"""
ml_utils.py
Sniper NUMA ML 共通ユーティリティ。
RandomForest / SVM / XGBoost などが共有するデータロード・特徴量抽出・プロット関数。
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utility.cpu_affinity import get_cpu_map as _get_cpu_map
from utility.stats_reader import (
    parse_sim_time,
    parse_ipc,
    parse_instructions,
    parse_cycles,
    parse_node_stats,
    parse_numa_access,
    parse_l1d_where,
    P_CORES,
    E_CORES,
    NODE0_CPUS,
)
from utility.power_model import estimate as estimate_power

# ── 定数 ────────────────────────────────────────────────────────
STRATEGIES   = ["Packed", "Scatter", "HPO", "EPO"]
THREAD_NUMS  = [2, 4, 8, 16]
OUTPUTS_DIR  = Path(__file__).parent.parent / "Outputs" / "sizeS"

FEATURE_COLS = [
    "avg_ipc",
    "ipc_cv",
    "pe_ratio",
    "local_ratio",
    "remote_ratio",
    "l2_intensity",
    "l3_intensity",
    "mem_intensity",
    "read_ratio",
]
FEATURE_COLS_ALLTH = ["num_threads"] + FEATURE_COLS

# 2% 以内の差は同率とみなす許容幅
TIE_TOLERANCE = 0.02


# ── 特徴量抽出 ──────────────────────────────────────────────────
def parse_stats(output_dir: Path, num_threads: int,
                cpu_map: list | None = None) -> dict:
    """
    Sniper sim.stats.sqlite3 から特徴量・性能指標を返す。
    Returns dict with FEATURE_COLS keys + 'sim_seconds', 'energy_j'.
    Returns {} if data unavailable.
    """
    if not (output_dir / "sim.stats.sqlite3").exists():
        return {}

    out_str     = str(output_dir)
    sim_seconds = parse_sim_time(out_str) or 0.0
    ipc_map     = parse_ipc(out_str, cpu_map)
    inst_map    = parse_instructions(out_str)
    cycle_map   = parse_cycles(out_str, cpu_map)
    numa_acc    = parse_numa_access(out_str)
    node_stats  = parse_node_stats(out_str)
    l1d_where   = parse_l1d_where(out_str)

    if not ipc_map or sim_seconds == 0:
        return {}

    power_result = estimate_power(out_str, cpu_map, num_threads)
    energy_j     = power_result.get("energy_j", 0.0)

    # コア別 IPC
    ipc_list, p_ipc_list, e_ipc_list = [], [], []
    for sim_core in range(num_threads):
        cpu_id = cpu_map[sim_core] if cpu_map else sim_core
        ipc = ipc_map.get(sim_core, 0.0)
        if ipc > 0:
            ipc_list.append(ipc)
            (p_ipc_list if cpu_id in P_CORES else e_ipc_list).append(ipc)

    avg_ipc  = float(np.mean(ipc_list)) if ipc_list else 0.0
    ipc_cv   = (float(np.std(ipc_list) / avg_ipc)
                if avg_ipc > 0 and len(ipc_list) > 1 else 0.0)
    pe_ratio = (float(np.mean(p_ipc_list)) / float(np.mean(e_ipc_list))
                if p_ipc_list and e_ipc_list and float(np.mean(e_ipc_list)) > 0
                else 1.0)

    # NUMA アクセス
    total_local  = numa_acc.get("local", 0)
    total_remote = numa_acc.get("remote", 0)
    total_dram   = total_local + total_remote
    local_ratio  = total_local  / total_dram if total_dram > 0 else 0.0
    remote_ratio = total_remote / total_dram if total_dram > 0 else 0.0

    # メモリ階層強度
    total_insts = sum(inst_map.get(c, 0) for c in range(num_threads))
    l2_hits = l3_hits = 0
    for core_d in l1d_where.values():
        l2_hits += core_d.get("l2", 0)
        l3_hits += core_d.get("l3", 0)
    l2_intensity  = l2_hits    / total_insts if total_insts > 0 else 0.0
    l3_intensity  = l3_hits    / total_insts if total_insts > 0 else 0.0
    mem_intensity = total_dram / total_insts if total_insts > 0 else 0.0

    # DRAM 読み書き比
    total_reads  = sum(v["reads"]  for v in node_stats.values())
    total_writes = sum(v["writes"] for v in node_stats.values())
    dram_total   = total_reads + total_writes
    read_ratio   = total_reads / dram_total if dram_total > 0 else 0.5

    return {
        "sim_seconds":  sim_seconds,
        "energy_j":     energy_j,
        "avg_ipc":      avg_ipc,
        "ipc_cv":       ipc_cv,
        "pe_ratio":     pe_ratio,
        "local_ratio":  local_ratio,
        "remote_ratio": remote_ratio,
        "l2_intensity": l2_intensity,
        "l3_intensity": l3_intensity,
        "mem_intensity":mem_intensity,
        "read_ratio":   read_ratio,
    }


# ── データセット収集 ─────────────────────────────────────────────
def collect_dataset(
    outputs_dir: Path,
    num_threads: int,
    label_by: str,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """
    outputs_dir/{N}TH/ から学習データを収集。
    同一ワークロード×戦略が複数ランあれば最新を採用。

    Returns: (X, y, perf_df)
    """
    thread_dir = outputs_dir / f"{num_threads}TH"
    if not thread_dir.exists():
        return pd.DataFrame(), pd.Series(dtype=str), pd.DataFrame()

    # workload -> strategy -> (ts, Path) の最新ディレクトリを収集
    wl_dirs: dict[str, dict[str, tuple[str, Path]]] = {}
    for d in sorted(thread_dir.iterdir()):
        if not d.is_dir():
            continue
        parts = d.name.split("_")
        if len(parts) < 6:
            continue
        workload = parts[0]
        strategy = parts[2]
        ts       = "_".join(parts[4:6])  # YYYYMMDD_HHMMSS
        if strategy not in STRATEGIES:
            continue
        prev = wl_dirs.setdefault(workload, {}).get(strategy)
        if prev is None or ts > prev[0]:
            wl_dirs[workload][strategy] = (ts, d)

    rows_X, rows_y, rows_perf, names = [], [], [], []

    for wl, sdirs_ts in sorted(wl_dirs.items()):
        if not all(s in sdirs_ts for s in STRATEGIES):
            continue
        sdirs = {s: v[1] for s, v in sdirs_ts.items()}

        packed_cpu_map = _get_cpu_map("Packed", wl)
        packed_stats   = parse_stats(sdirs["Packed"], num_threads, packed_cpu_map)
        if not packed_stats or packed_stats.get("sim_seconds", 0) == 0:
            continue

        perf_row = {}
        for s in STRATEGIES:
            s_stats     = parse_stats(sdirs[s], num_threads, _get_cpu_map(s, wl)) or {}
            perf_row[s] = s_stats.get(label_by, float("inf"))

        best_val = min(perf_row.values())
        best = min(
            (s for s in STRATEGIES if perf_row[s] <= best_val * (1 + TIE_TOLERANCE)),
            key=STRATEGIES.index,
        )

        rows_X.append([packed_stats.get(c, 0.0) for c in FEATURE_COLS])
        rows_y.append(best)
        rows_perf.append(perf_row)
        names.append(wl)

    if not names:
        return pd.DataFrame(), pd.Series(dtype=str), pd.DataFrame()

    X    = pd.DataFrame(rows_X, index=names, columns=FEATURE_COLS)
    y    = pd.Series(rows_y,    index=names, name="best_strategy")
    perf = pd.DataFrame(rows_perf, index=names, columns=STRATEGIES)
    return X, y, perf


def build_allth_data(
    label_by: str,
    outputs_dir: Path = OUTPUTS_DIR,
) -> tuple[pd.DataFrame, pd.Series]:
    rows_X, rows_y, names = [], [], []
    for n in THREAD_NUMS:
        X_n, y_n, _ = collect_dataset(outputs_dir, n, label_by)
        if X_n.empty:
            continue
        X_aug = X_n.copy()
        X_aug.insert(0, "num_threads", n)
        for i, idx in enumerate(X_aug.index):
            rows_X.append(X_aug.iloc[i].values)
            rows_y.append(y_n.iloc[i])
            names.append(f"{idx}_{n}TH")
    if not names:
        return pd.DataFrame(), pd.Series(dtype=str)
    X_all = pd.DataFrame(rows_X, index=names, columns=FEATURE_COLS_ALLTH)
    y_all = pd.Series(rows_y, index=names, name="best_strategy")
    return X_all, y_all


# ── 共通プロット ─────────────────────────────────────────────────
def save_fig(fig: plt.Figure, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def plot_feature_importance(imp: pd.Series, title: str, path: Path):
    fig, ax = plt.subplots(figsize=(10, 5))
    imp.sort_values().plot.barh(ax=ax, color="steelblue")
    ax.set_title(title, fontsize=13, fontweight="bold")
    ax.set_xlabel("Importance")
    save_fig(fig, path)


def plot_confusion_matrix(cm: np.ndarray, classes: list, title: str, path: Path):
    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(cm, cmap="Blues")
    ax.set_xticks(range(len(classes)))
    ax.set_yticks(range(len(classes)))
    ax.set_xticklabels(classes, rotation=45, ha="right")
    ax.set_yticklabels(classes)
    ax.set_xlabel("Predicted")
    ax.set_ylabel("True")
    ax.set_title(title, fontsize=12, fontweight="bold")
    for i in range(len(classes)):
        for j in range(len(classes)):
            ax.text(j, i, cm[i, j], ha="center", va="center",
                    color="white" if cm[i, j] > cm.max() / 2 else "black")
    fig.colorbar(im, ax=ax)
    save_fig(fig, path)


def plot_strategy_dist(y: pd.Series, title: str, path: Path):
    fig, ax = plt.subplots(figsize=(6, 4))
    y.value_counts().reindex(STRATEGIES, fill_value=0).plot.bar(
        ax=ax, color="steelblue", rot=0)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlabel("Strategy")
    ax.set_ylabel("Count")
    save_fig(fig, path)


def print_per_sample(names, y_true, y_pred, header="Workload"):
    acc = sum(t == p for t, p in zip(y_true, y_pred)) / len(y_true)
    n_ok = int(acc * len(y_true))
    print(f"  LOO accuracy = {acc:.3f}  ({n_ok}/{len(y_true)})")
    print(f"\n  {header:<18} {'True':<10} {'Pred':<10} {'OK?'}")
    print(f"  {'-'*50}")
    for name, t, p in zip(names, y_true, y_pred):
        print(f"  {str(name):<18} {t:<10} {p:<10} {'✓' if t == p else '✗'}")
