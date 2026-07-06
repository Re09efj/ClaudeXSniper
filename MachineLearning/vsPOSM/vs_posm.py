"""
vs_posm.py
POSM(先行研究, Jin) と TabPFN の公平な比較。

POSMの本来の出力空間は MPO/HPO の2択（Documents/jinyifan_master_thesis.pdf 参照）。
4クラス(Packed/Scatter/HPO/EPO)の正解に対してPOSMを評価するのは構造的に不公平なため、
真の正解を「MPO/HPOのうちsim_secondsが小さい方」に限定した2クラスで評価する。

性能比(perf_ratio)は実運用上の実質コストを見るため、Packed/Scatter/HPO/EPO/MPOの
全5戦略中の最速(oracle)に対する比率で計算する。

出力: MachineLearning/vsPOSM/{N}TH_{S,W}/, ALLTH_S/, ALLTH_W/, ALLTH_SW/
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["font.family"] = "Noto Sans CJK JP"
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score
from sklearn.model_selection import LeaveOneOut

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from utility.cpu_affinity import get_cpu_map, resolve_cpu_map
from MachineLearning.ml_utils import parse_stats, FEATURE_COLS, THREAD_NUMS, save_fig

try:
    import os
    from tabpfn_client import TabPFNClassifier, set_access_token
    _token = os.environ.get("TABPFN_TOKEN", "tabpfn_sk_lEBsJpNhQfIxGUMGBHFQZhmWNskPUJy2jUeS3omRGZw")
    set_access_token(_token)
    HAS_TABPFN = True
except ImportError:
    HAS_TABPFN = False
    print("[vsPOSM] tabpfn-client が見つかりません: pip install tabpfn-client")

ALL_STRATEGIES = ["Packed", "Scatter", "HPO", "EPO", "MPO"]
TWO_CLASS      = ["MPO", "HPO"]
TIE_TOLERANCE  = 0.02

OUTPUTS_DIRS = {"S": ROOT / "Outputs" / "sizeS", "W": ROOT / "Outputs" / "sizeW"}
MODEL_DIR    = Path(__file__).parent


# ── データ収集(2クラス版) ────────────────────────────────────────
def collect_dataset_2class(outputs_dir: Path, num_threads: int,
                           label_by: str = "sim_seconds"):
    bench_class = outputs_dir.name.replace("size", "")
    thread_dir = outputs_dir / f"{num_threads}TH"
    if not thread_dir.exists():
        return pd.DataFrame(), pd.Series(dtype=str), pd.DataFrame()

    wl_dirs: dict[str, dict[str, tuple[str, Path]]] = {}
    for d in sorted(thread_dir.iterdir()):
        if not d.is_dir():
            continue
        parts = d.name.split("_")
        if len(parts) < 6:
            continue
        workload, strategy = parts[0], parts[2]
        ts = "_".join(parts[4:6])
        if strategy not in ALL_STRATEGIES:
            continue
        prev = wl_dirs.setdefault(workload, {}).get(strategy)
        if prev is None or ts > prev[0]:
            wl_dirs[workload][strategy] = (ts, d)

    rows_X, rows_y, rows_perf, names = [], [], [], []
    for wl, sdirs_ts in sorted(wl_dirs.items()):
        if not all(s in sdirs_ts for s in ALL_STRATEGIES):
            continue
        sdirs = {s: v[1] for s, v in sdirs_ts.items()}

        packed_stats = parse_stats(sdirs["Packed"], num_threads, get_cpu_map("Packed", wl))
        if not packed_stats or packed_stats.get("sim_seconds", 0) == 0:
            continue

        perf_row_full = {}
        for s in ALL_STRATEGIES:
            s_stats = parse_stats(sdirs[s], num_threads, resolve_cpu_map(s, wl, bench_class, num_threads)) or {}
            perf_row_full[s] = s_stats.get(label_by, float("inf"))

        two_vals = {s: perf_row_full[s] for s in TWO_CLASS}
        best_val = min(two_vals.values())
        best = min((s for s in TWO_CLASS if two_vals[s] <= best_val * (1 + TIE_TOLERANCE)),
                   key=TWO_CLASS.index)

        rows_X.append([packed_stats.get(c, 0.0) for c in FEATURE_COLS])
        rows_y.append(best)
        rows_perf.append(perf_row_full)
        names.append(wl)

    if not names:
        return pd.DataFrame(), pd.Series(dtype=str), pd.DataFrame()

    X = pd.DataFrame(rows_X, index=names, columns=FEATURE_COLS)
    y = pd.Series(rows_y, index=names, name="best_2class")
    perf = pd.DataFrame(rows_perf, index=names, columns=ALL_STRATEGIES)
    return X, y, perf


def build_allth(cls_filter: list[str], label_by: str = "sim_seconds"):
    rows_X, rows_y, rows_perf, names = [], [], [], []
    for cls in cls_filter:
        outputs_dir = OUTPUTS_DIRS[cls]
        for n in THREAD_NUMS:
            X_n, y_n, perf_n = collect_dataset_2class(outputs_dir, n, label_by)
            if X_n.empty:
                continue
            for i, idx in enumerate(X_n.index):
                rows_X.append(X_n.iloc[i].values)
                rows_y.append(y_n.iloc[i])
                rows_perf.append(perf_n.iloc[i])
                names.append(f"{idx}_{n}TH_{cls}")
    if not names:
        return pd.DataFrame(), pd.Series(dtype=str), pd.DataFrame()
    X = pd.DataFrame(rows_X, index=names, columns=FEATURE_COLS)
    y = pd.Series(rows_y, index=names, name="best_2class")
    perf = pd.DataFrame(rows_perf, index=names, columns=ALL_STRATEGIES)
    return X, y, perf


# ── POSM (ルールベース、LOOでMinMax正規化) ────────────────────────
def posm_predict_loo(X: pd.DataFrame) -> list[str]:
    n = len(X)
    y_pred = []
    for i in range(n):
        train_idx = [j for j in range(n) if j != i]
        train_X   = X.iloc[train_idx]
        test      = X.iloc[i]

        mem_min, mem_max = train_X["mem_intensity"].min(), train_X["mem_intensity"].max()
        ipc_min, ipc_max = train_X["ipc_cv"].min(), train_X["ipc_cv"].max()
        norm_mem = (test["mem_intensity"] - mem_min) / (mem_max - mem_min + 1e-12)
        norm_ipc = (test["ipc_cv"] - ipc_min) / (ipc_max - ipc_min + 1e-12)
        y_pred.append("MPO" if norm_mem > norm_ipc else "HPO")
    return y_pred


# ── TabPFN (LOO-CV) ─────────────────────────────────────────────
def tabpfn_predict_loo(X: pd.DataFrame, y: pd.Series) -> list[str] | None:
    if not HAS_TABPFN:
        return None
    loo = LeaveOneOut()
    y_pred = []
    n_total = len(X)
    for fold_i, (train_idx, test_idx) in enumerate(loo.split(X)):
        y_train = y.iloc[train_idx]
        if y_train.nunique() < 2:
            y_pred.append(y_train.mode()[0])
            continue
        for attempt in range(5):
            try:
                clf = TabPFNClassifier()
                clf.fit(X.iloc[train_idx], y_train)
                y_pred.append(clf.predict(X.iloc[test_idx])[0])
                break
            except Exception as e:
                if attempt < 4:
                    print(f"  [retry {attempt+1}/4] {e.__class__.__name__}", flush=True)
                    time.sleep(10 * (attempt + 1))
                else:
                    raise
        print(f"  [TabPFN {fold_i+1:3d}/{n_total}] {X.index[test_idx[0]]}", flush=True)
    return y_pred


# ── 性能比 ────────────────────────────────────────────────────────
def perf_ratio(index, y_pred: list[str], perf: pd.DataFrame) -> float:
    ratios = []
    for idx, pred in zip(index, y_pred):
        row    = perf.loc[idx, ALL_STRATEGIES]
        oracle = row.min()
        ratios.append(perf.loc[idx, pred] / oracle)
    return float(np.mean(ratios))


# ── 評価 + 保存 ────────────────────────────────────────────────────
def evaluate_and_save(spec_label: str, out_dir: Path, X, y, perf) -> dict:
    n = len(X)
    if n == 0:
        print(f"  {spec_label}: データなし")
        return {}
    dist = y.value_counts().to_dict()

    posm_pred = posm_predict_loo(X)
    acc_posm  = accuracy_score(y, posm_pred)
    r_posm    = perf_ratio(y.index, posm_pred, perf)

    tabpfn_pred = tabpfn_predict_loo(X, y)
    if tabpfn_pred is not None:
        acc_tabpfn = accuracy_score(y, tabpfn_pred)
        r_tabpfn   = perf_ratio(y.index, tabpfn_pred, perf)
    else:
        acc_tabpfn = r_tabpfn = None

    print(f"\n{'─'*70}")
    print(f"  {spec_label}  n={n}  分布={dist}")
    print(f"  {'手法':<10}{'精度':>8}  {'性能比(oracle比)':>16}")
    print(f"  {'POSM':<10}{acc_posm:>7.1%}  {r_posm:>16.4f}")
    if acc_tabpfn is not None:
        print(f"  {'TabPFN':<10}{acc_tabpfn:>7.1%}  {r_tabpfn:>16.4f}")

    out_dir.mkdir(parents=True, exist_ok=True)
    X.join(perf).to_csv(out_dir / "dataset.csv")
    row = {"spec": spec_label, "n_samples": n,
           "acc_posm": acc_posm, "r_posm": r_posm,
           "acc_tabpfn": acc_tabpfn, "r_tabpfn": r_tabpfn}
    pd.DataFrame([row]).to_csv(out_dir / "comparison.csv", index=False)

    return row


def plot_summary(records: list[dict], out_path: Path):
    labels = [r["spec"] for r in records]
    x = np.arange(len(labels))
    w = 0.35

    fig, axes = plt.subplots(1, 2, figsize=(max(10, len(labels) * 1.3), 5))

    ax = axes[0]
    ax.bar(x - w/2, [r["acc_posm"] for r in records], w, label="POSM", color="#e74c3c")
    ax.bar(x + w/2, [r.get("acc_tabpfn") or 0 for r in records], w, label="TabPFN", color="#9b59b6")
    ax.set_xticks(x); ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_ylim(0, 1.15); ax.set_ylabel("LOO-CV Accuracy (2-class: MPO/HPO)")
    ax.set_title("精度比較")
    ax.axhline(0.5, color="gray", ls="--", alpha=0.5)
    ax.legend(fontsize=9)

    ax = axes[1]
    ax.bar(x - w/2, [r["r_posm"] for r in records], w, label="POSM", color="#e74c3c")
    ax.bar(x + w/2, [r.get("r_tabpfn") or 0 for r in records], w, label="TabPFN", color="#9b59b6")
    ax.set_xticks(x); ax.set_xticklabels(labels, rotation=30, ha="right")
    ax.set_ylabel("平均性能比 (sim_sec / oracle, ↓が良)")
    ax.set_title("性能比較 (全5戦略中のoracleとの比)")
    ax.axhline(1.0, color="gray", ls="--", alpha=0.5)
    ax.legend(fontsize=9)

    fig.tight_layout()
    save_fig(fig, out_path)
    print(f"\n[SAVE] {out_path}")


def main():
    records = []

    for cls in ("S", "W"):
        for n in THREAD_NUMS:
            X, y, perf = collect_dataset_2class(OUTPUTS_DIRS[cls], n, "sim_seconds")
            spec = f"{n}TH-{cls}"
            row = evaluate_and_save(spec, MODEL_DIR / f"{n}TH_{cls}", X, y, perf)
            if row:
                records.append(row)

        X_allth, y_allth, perf_allth = build_allth([cls], "sim_seconds")
        row = evaluate_and_save(f"ALLTH-{cls}", MODEL_DIR / f"ALLTH_{cls}", X_allth, y_allth, perf_allth)
        if row:
            records.append(row)

    X_sw, y_sw, perf_sw = build_allth(["S", "W"], "sim_seconds")
    row = evaluate_and_save("ALLTH_SW", MODEL_DIR / "ALLTH_SW", X_sw, y_sw, perf_sw)
    if row:
        records.append(row)

    if records:
        pd.DataFrame(records).to_csv(MODEL_DIR / "comparison_all.csv", index=False)
        plot_summary(records, MODEL_DIR / "posm_vs_tabpfn_comparison.png")


if __name__ == "__main__":
    main()
