"""
train_combined_akarin.py
train_combined.py の5クラス版。既存4戦略(Packed/Scatter/HPO/EPO)に加えて、
各(workload, threads)で最良だったAKARIN候補(alpha違いの複数cpu_mapのうち
sim_secondsが最小のもの、oracle)を5番目のクラス"AKARIN"として追加する。

AKARIN候補はget_cpu_map(strategy, workload)の静的テーブルには無い
(cpu_mapがalphaごとに動的にCP-SATで計算される)ため、各AKARIN実行ディレクトリの
affinity_config.txt に保存済みの cpu_map=[...] 行を直接パースして使う。

出力先: MachineLearning/{RF,SVM,XGBoost,TabPFN}/ALLTH_SW_AKARIN/

使い方:
    python train_combined_akarin.py
    python train_combined_akarin.py --label energy_j
    python train_combined_akarin.py --model rf
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
matplotlib.rcParams["font.family"] = "Noto Sans CJK JP"
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.inspection import permutation_importance as sk_perm_imp
from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import LeaveOneOut
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from MachineLearning.ml_utils import (
    FEATURE_COLS,
    THREAD_NUMS,
    _CLASS_ENC,
    parse_stats,
    save_fig,
)
from utility.cpu_affinity import get_cpu_map as _get_cpu_map

BASE_STRATEGIES = ["Packed", "Scatter", "HPO", "EPO"]
STRATEGIES = BASE_STRATEGIES + ["AKARIN"]

OUTPUTS_DIRS = [
    ROOT / "Outputs" / "sizeS",
    ROOT / "Outputs" / "sizeW",
]
ML_DIR = Path(__file__).parent
OUT_SUFFIX = "ALLTH_SW_AKARIN"


def _read_cpu_map_from_config(d: Path) -> list | None:
    cfg = d / "affinity_config.txt"
    if not cfg.exists():
        return None
    for line in cfg.read_text().splitlines():
        if line.startswith("cpu_map="):
            try:
                return ast.literal_eval(line[len("cpu_map="):])
            except (ValueError, SyntaxError):
                return None
    return None


def collect_dataset_akarin(
    outputs_dir: Path, num_threads: int, label_by: str,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    thread_dir = outputs_dir / f"{num_threads}TH"
    if not thread_dir.exists():
        return pd.DataFrame(), pd.Series(dtype=str), pd.DataFrame()

    # workload -> strategy -> (ts, Path)   (strategyは"Packed"等 or "AKARIN")
    wl_dirs: dict[str, dict[str, tuple[str, Path]]] = {}
    for d in sorted(thread_dir.iterdir()):
        if not d.is_dir():
            continue
        parts = d.name.split("_")
        if len(parts) < 6:
            continue
        workload = parts[0]
        strategy = parts[2]
        if strategy not in BASE_STRATEGIES and strategy != "AKARIN":
            continue
        ts = "_".join(parts[-2:])  # YYYYMMDD_HHMMSS (末尾2要素)
        key = strategy if strategy in BASE_STRATEGIES else f"AKARIN::{d.name}"
        prev = wl_dirs.setdefault(workload, {}).get(key)
        if prev is None or ts > prev[0]:
            wl_dirs[workload][key] = (ts, d)

    rows_X, rows_y, rows_perf, names = [], [], [], []

    for wl, sdirs_ts in sorted(wl_dirs.items()):
        if not all(s in sdirs_ts for s in BASE_STRATEGIES):
            continue
        akarin_keys = [k for k in sdirs_ts if k.startswith("AKARIN::")]
        if not akarin_keys:
            continue  # このworkload/threadsにAKARIN候補が無ければ5クラス化できないのでスキップ

        sdirs = {s: sdirs_ts[s][1] for s in BASE_STRATEGIES}

        packed_cpu_map = _get_cpu_map("Packed", wl)
        packed_stats = parse_stats(sdirs["Packed"], num_threads, packed_cpu_map)
        if not packed_stats or packed_stats.get("sim_seconds", 0) == 0:
            continue

        perf_row = {}
        for s in BASE_STRATEGIES:
            s_stats = parse_stats(sdirs[s], num_threads, _get_cpu_map(s, wl)) or {}
            perf_row[s] = s_stats.get(label_by, float("inf"))

        # AKARIN候補群のうちoracle(最良)を選ぶ
        best_akarin_val = float("inf")
        for k in akarin_keys:
            d = sdirs_ts[k][1]
            cpu_map = _read_cpu_map_from_config(d)
            stats = parse_stats(d, num_threads, cpu_map) or {}
            val = stats.get(label_by, float("inf"))
            if val < best_akarin_val:
                best_akarin_val = val
        perf_row["AKARIN"] = best_akarin_val

        finite_vals = {k: v for k, v in perf_row.items() if np.isfinite(v)}
        if not finite_vals:
            continue
        best_val = min(finite_vals.values())
        best = min(
            (s for s in STRATEGIES if perf_row.get(s, float("inf")) <= best_val * 1.02),
            key=STRATEGIES.index,
        )

        rows_X.append([packed_stats.get(c, 0.0) for c in FEATURE_COLS])
        rows_y.append(best)
        rows_perf.append(perf_row)
        names.append(wl)

    if not names:
        return pd.DataFrame(), pd.Series(dtype=str), pd.DataFrame()

    X = pd.DataFrame(rows_X, index=names, columns=FEATURE_COLS)
    y = pd.Series(rows_y, index=names, name="best_strategy")
    perf = pd.DataFrame(rows_perf, index=names, columns=STRATEGIES)
    return X, y, perf


def build_allth_data_akarin(label_by: str, outputs_dirs: list[Path]):
    rows_X, rows_y, names = [], [], []
    for outputs_dir in outputs_dirs:
        bench_class = outputs_dir.name.replace("size", "")
        for n in THREAD_NUMS + [16]:
            X_n, y_n, _ = collect_dataset_akarin(outputs_dir, n, label_by)
            if X_n.empty:
                continue
            X_aug = X_n.copy()
            X_aug.insert(0, "num_threads", n)
            X_aug.insert(1, "bench_class_enc", _CLASS_ENC.get(bench_class, 0))
            for i, idx in enumerate(X_aug.index):
                rows_X.append(X_aug.iloc[i].values)
                rows_y.append(y_n.iloc[i])
                names.append(f"{idx}_{n}TH_{bench_class}")
    if not names:
        return pd.DataFrame(), pd.Series(dtype=str)
    cols = ["num_threads", "bench_class_enc"] + FEATURE_COLS
    return pd.DataFrame(rows_X, index=names, columns=cols), pd.Series(rows_y, index=names, name="best_strategy")


def build_allth_perf_akarin(label_by: str, outputs_dirs: list[Path]) -> pd.DataFrame:
    rows_perf, names = [], []
    for outputs_dir in outputs_dirs:
        bench_class = outputs_dir.name.replace("size", "")
        for n in THREAD_NUMS + [16]:
            X_n, y_n, perf_n = collect_dataset_akarin(outputs_dir, n, label_by)
            if X_n.empty:
                continue
            for i, idx in enumerate(X_n.index):
                rows_perf.append(perf_n.iloc[i])
                names.append(f"{idx}_{n}TH_{bench_class}")
    if not names:
        return pd.DataFrame()
    return pd.DataFrame(rows_perf, index=names, columns=STRATEGIES)


def perf_ratio(y_true, y_pred, perf: pd.DataFrame) -> float:
    ratios = []
    for idx, pred in zip(y_true.index, y_pred):
        row = perf.loc[idx, STRATEGIES]
        oracle = row.min()
        ratios.append(perf.loc[idx, pred] / oracle)
    return float(np.mean(ratios))


def _plot_imp(imp: pd.Series, title: str, path: Path, color: str):
    fig, ax = plt.subplots(figsize=(10, 6))
    imp.sort_values().plot.barh(ax=ax, color=color)
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_xlabel("Importance")
    save_fig(fig, path)


def _plot_cm(cm: np.ndarray, title: str, path: Path, tag: str):
    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(cm, cmap="Blues")
    ax.set_xticks(range(len(STRATEGIES)))
    ax.set_yticks(range(len(STRATEGIES)))
    ax.set_xticklabels(STRATEGIES, rotation=45, ha="right")
    ax.set_yticklabels(STRATEGIES)
    ax.set_xlabel("Predicted")
    ax.set_ylabel("True")
    ax.set_title(title, fontsize=12, fontweight="bold")
    for i in range(len(STRATEGIES)):
        for j in range(len(STRATEGIES)):
            ax.text(j, i, cm[i, j], ha="center", va="center",
                    color="white" if cm[i, j] > cm.max() / 2 else "black")
    fig.colorbar(im, ax=ax)
    save_fig(fig, path)
    print(f"[{tag}] 混同行列: {path}")


def _plot_dist(y: pd.Series, title: str, path: Path, tag: str):
    fig, ax = plt.subplots(figsize=(6, 4))
    y.value_counts().reindex(STRATEGIES, fill_value=0).plot.bar(
        ax=ax, color="steelblue", rot=0)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlabel("Strategy")
    ax.set_ylabel("Count")
    save_fig(fig, path)
    print(f"[{tag}] 戦略分布: {path}")


def _save_results(res: dict, X: pd.DataFrame, y: pd.Series,
                  out_dir: Path, tag: str, label_by: str, color: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    _plot_imp(res["importances"], f"{tag} Feature Importance ({OUT_SUFFIX}, {label_by})",
              out_dir / f"feature_importance_{label_by}.png", color)
    _plot_cm(res["confusion"], f"{tag} Confusion Matrix ({OUT_SUFFIX}, {label_by})",
             out_dir / f"confusion_matrix_{label_by}.png", tag)
    _plot_dist(y, f"Best Strategy Distribution ({OUT_SUFFIX})",
               out_dir / f"strategy_dist_{label_by}.png", tag)
    imp_df = res["importances"].reset_index()
    imp_df.columns = ["feature", "importance"]
    imp_df.to_csv(out_dir / f"importances_{label_by}.csv", index=False)
    row = {"threads": "ALL", "sizes": "SW", "label": label_by,
           "accuracy": res["accuracy"], "n_samples": len(X)}
    if "perf_ratio" in res:
        row["perf_ratio"] = res["perf_ratio"]
    pd.DataFrame([row]).to_csv(out_dir / f"performance_{label_by}.csv", index=False)
    msg = f"[{tag}] LOO accuracy={res['accuracy']:.3f}  ({int(res['accuracy']*len(y))}/{len(y)})"
    if "perf_ratio" in res:
        msg += f"  perf_ratio={res['perf_ratio']:.4f} (1.0=oracle)"
    print(msg)


def run_rf(X, y, label_by):
    print(f"\n[RF-{OUT_SUFFIX}] n={len(X)}  features={X.shape[1]}")
    loo = LeaveOneOut()
    y_true, y_pred, importances = [], [], []
    for train_idx, test_idx in loo.split(X):
        y_train = y.iloc[train_idx]
        y_true.append(y.iloc[test_idx[0]])
        if y_train.nunique() < 2:
            y_pred.append(y_train.mode()[0])
            continue
        clf = RandomForestClassifier(n_estimators=300, class_weight="balanced", random_state=42)
        clf.fit(X.iloc[train_idx], y_train)
        y_pred.append(clf.predict(X.iloc[test_idx])[0])
        importances.append(clf.feature_importances_)
    avg_imp = np.mean(importances, axis=0) if importances else np.zeros(X.shape[1])
    res = {"accuracy": accuracy_score(y_true, y_pred),
           "confusion": confusion_matrix(y_true, y_pred, labels=STRATEGIES),
           "importances": pd.Series(avg_imp, index=X.columns.tolist())}
    _save_results(res, X, y, ML_DIR / "RandomForest" / OUT_SUFFIX, "RF", label_by, "steelblue")


def run_svm(X, y, label_by):
    print(f"\n[SVM-{OUT_SUFFIX}] n={len(X)}  features={X.shape[1]}")
    loo = LeaveOneOut()
    y_true, y_pred = [], []

    def make_pipe():
        return Pipeline([("scaler", StandardScaler()),
                          ("svm", SVC(kernel="rbf", C=10.0, gamma="scale",
                                      class_weight="balanced", random_state=42,
                                      decision_function_shape="ovr"))])

    for train_idx, test_idx in loo.split(X):
        y_train = y.iloc[train_idx]
        y_true.append(y.iloc[test_idx[0]])
        if y_train.nunique() < 2:
            y_pred.append(y_train.mode()[0])
            continue
        pipe = make_pipe()
        pipe.fit(X.iloc[train_idx], y_train)
        y_pred.append(pipe.predict(X.iloc[test_idx])[0])

    if y.nunique() < 2:
        imp = pd.Series(0.0, index=X.columns.tolist())
    else:
        pipe_full = make_pipe()
        pipe_full.fit(X, y)
        r = sk_perm_imp(pipe_full, X, y, n_repeats=10, random_state=42)
        imp = pd.Series(r.importances_mean, index=X.columns.tolist())

    res = {"accuracy": accuracy_score(y_true, y_pred),
           "confusion": confusion_matrix(y_true, y_pred, labels=STRATEGIES),
           "importances": imp}
    _save_results(res, X, y, ML_DIR / "SVM" / OUT_SUFFIX, "SVM", label_by, "darkorange")


def run_xgb(X, y, label_by):
    try:
        from xgboost import XGBClassifier
    except ImportError:
        print("[XGB] xgboost not installed")
        return
    from sklearn.preprocessing import LabelEncoder

    print(f"\n[XGB-{OUT_SUFFIX}] n={len(X)}  features={X.shape[1]}")
    le = LabelEncoder()
    le.fit(STRATEGIES)
    y_enc = le.transform(y)

    loo = LeaveOneOut()
    y_true_enc, y_pred_enc = [], []
    n_total = len(X)

    for fold_i, (train_idx, test_idx) in enumerate(loo.split(X)):
        wl_name = X.index[test_idx[0]]
        print(f"  [{fold_i+1:3d}/{n_total}] {wl_name}", flush=True)
        y_train = y_enc[train_idx]
        if len(np.unique(y_train)) < 2:
            y_pred_enc.append(y_train[0])
            y_true_enc.append(y_enc[test_idx[0]])
            continue
        clf = XGBClassifier(n_estimators=200, max_depth=4, learning_rate=0.1,
                            use_label_encoder=False, eval_metric="mlogloss",
                            random_state=42, verbosity=0)
        X_tr = X.iloc[train_idx].copy()
        y_tr = list(y_train)
        for cls in set(range(len(STRATEGIES))) - set(np.unique(y_train)):
            X_tr = pd.concat([X_tr, X_tr.mean().to_frame().T], ignore_index=True)
            y_tr.append(cls)
        clf.fit(X_tr, np.array(y_tr))
        y_pred_enc.append(clf.predict(X.iloc[test_idx])[0])
        y_true_enc.append(y_enc[test_idx[0]])

    y_true = le.inverse_transform(y_true_enc)
    y_pred = le.inverse_transform(y_pred_enc)

    clf_full = XGBClassifier(n_estimators=200, max_depth=4, learning_rate=0.1,
                             use_label_encoder=False, eval_metric="mlogloss",
                             random_state=42, verbosity=0)
    X_full = X.copy()
    y_full = list(y_enc)
    for cls in set(range(len(STRATEGIES))) - set(np.unique(y_enc)):
        X_full = pd.concat([X_full, X_full.mean().to_frame().T], ignore_index=True)
        y_full.append(cls)
    clf_full.fit(X_full, np.array(y_full))
    imp = pd.Series(clf_full.feature_importances_, index=X.columns.tolist())

    res = {"accuracy": accuracy_score(y_true, y_pred),
           "confusion": confusion_matrix(y_true, y_pred, labels=STRATEGIES),
           "importances": imp}
    _save_results(res, X, pd.Series(y, index=X.index), ML_DIR / "XGBoost" / OUT_SUFFIX, "XGB", label_by, "forestgreen")


def run_tabpfn(X, y, label_by, perf=None):
    try:
        from tabpfn_client import TabPFNClassifier, set_access_token
        import os
        token = os.environ.get("TABPFN_TOKEN",
                               "tabpfn_sk_lEBsJpNhQfIxGUMGBHFQZhmWNskPUJy2jUeS3omRGZw")
        set_access_token(token)
    except ImportError:
        print("[TabPFN] tabpfn-client not installed")
        return

    print(f"\n[TabPFN-{OUT_SUFFIX}] n={len(X)}  features={X.shape[1]}")
    loo = LeaveOneOut()
    y_true, y_pred = [], []
    n_total = len(X)

    for fold_i, (train_idx, test_idx) in enumerate(loo.split(X)):
        wl_name = X.index[test_idx[0]]
        print(f"  [{fold_i+1:3d}/{n_total}] {wl_name}", flush=True)
        y_train = y.iloc[train_idx]
        y_true.append(y.iloc[test_idx[0]])
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

    if y.nunique() < 2:
        imp = pd.Series(0.0, index=X.columns.tolist())
    else:
        clf_full = TabPFNClassifier()
        clf_full.fit(X, y)
        r = sk_perm_imp(clf_full, X, y, n_repeats=10, random_state=42)
        imp = pd.Series(r.importances_mean, index=X.columns.tolist())

    res = {"accuracy": accuracy_score(y_true, y_pred),
           "confusion": confusion_matrix(y_true, y_pred, labels=STRATEGIES),
           "importances": imp}
    if perf is not None and not perf.empty:
        res["perf_ratio"] = perf_ratio(y, y_pred, perf)
    _save_results(res, X, y, ML_DIR / "TabPFN" / OUT_SUFFIX, "TabPFN", label_by, "mediumpurple")


def main():
    p = argparse.ArgumentParser(description="sizeS+W 結合 ALLTH 再訓練 (AKARIN込み5クラス)")
    p.add_argument("--label", default="sim_seconds", choices=["sim_seconds", "energy_j"])
    p.add_argument("--model", default="all", choices=["all", "rf", "svm", "xgb", "tabpfn"])
    args = p.parse_args()

    print(f"=== 結合訓練(AKARIN込み): sizeS + sizeW  label={args.label} ===")
    X, y = build_allth_data_akarin(args.label, OUTPUTS_DIRS)
    if X.empty:
        print("データが取得できませんでした。AKARIN候補が存在するworkload/threadsを確認してください。")
        sys.exit(1)

    print(f"データロード完了: {len(X)} サンプル  特徴量={X.shape[1]}")
    print(f"戦略分布:\n{y.value_counts()}\n")

    if args.model in ("all", "rf"):
        run_rf(X.copy(), y.copy(), args.label)
    if args.model in ("all", "svm"):
        run_svm(X.copy(), y.copy(), args.label)
    if args.model in ("all", "xgb"):
        run_xgb(X.copy(), y.copy(), args.label)
    if args.model in ("all", "tabpfn"):
        perf = build_allth_perf_akarin(args.label, OUTPUTS_DIRS)
        run_tabpfn(X.copy(), y.copy(), args.label, perf)

    print(f"\n=== 完了 ===  出力: {ML_DIR}/*/{OUT_SUFFIX}/")


if __name__ == "__main__":
    main()
