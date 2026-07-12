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
    parse_cpi_breakdown,
    _db,
    P_CORES,
    E_CORES,
    NODE0_CPUS,
)
from utility.power_model import estimate as estimate_power

# ── 定数 ────────────────────────────────────────────────────────
STRATEGIES   = ["Packed", "Scatter", "HPO", "EPO"]
THREAD_NUMS  = [2, 6, 8, 12]
OUTPUTS_DIR  = Path(__file__).parent.parent / "Outputs" / "sizeS"

FEATURE_COLS = [
    # ── IPC 系 ──────────────────────────
    "avg_ipc",
    "ipc_cv",
    "pe_ratio",
    # ── NUMA アクセス ────────────────────
    "local_ratio",
    "remote_ratio",
    # ── キャッシュ階層強度 ────────────────
    "l2_intensity",
    "l3_intensity",
    "mem_intensity",
    "read_ratio",
    # ── CPI 内訳比率（メモリ待ち） ─────────
    "cpi_l3_ratio",
    "cpi_dram_local_ratio",
    "cpi_dram_remote_ratio",
    "cpi_branch_ratio",
    "cpi_sync_ratio",
    # ── μop 命令ミックス ──────────────────
    "uop_load_ratio",
    "uop_store_ratio",
    "uop_fp_ratio",
    # ── TLB・ブランチ ────────────────────
    "dtlb_miss_rate",
    "branch_mispredict_rate",
    # ── DRAM レイテンシ ───────────────────
    "avg_dram_latency",
    "dram_queue_ratio",
    # ── L1D ヒット率 ─────────────────────
    "l1d_hit_ratio",
    # ── 同期頻度 ─────────────────────────
    "futex_wake_per_minst",
    # ── メモリ帯域・Roofline ──────────────
    "mem_bw_mbps",
    "operational_intensity",
    # ── TLB（命令・2段目） ────────────────
    "itlb_miss_rate",
    "stlb_miss_rate",
    # ── ロード供給源 ─────────────────────
    "load_l1_ratio",
]
FEATURE_COLS_ALLTH       = ["num_threads"] + FEATURE_COLS
FEATURE_COLS_ALLTH_MULTI = ["num_threads", "bench_class_enc"] + FEATURE_COLS

# 2% 以内の差は同率とみなす許容幅
TIE_TOLERANCE = 0.02

_CLASS_ENC = {"S": 0, "W": 1, "A": 2, "B": 3, "C": 4, "D": 5}


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
    node_stats  = parse_node_stats(out_str, cpu_map=cpu_map)
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

    # ── CPI 内訳比率 ──────────────────────────────────────────────
    cpi_bd = parse_cpi_breakdown(out_str)  # {core: {base, branch, l2, l3, dram_local, dram_remote}}
    cpi_sums = {"base": 0, "branch": 0, "l2": 0, "l3": 0, "dram_local": 0, "dram_remote": 0}
    for core_d in cpi_bd.values():
        for k in cpi_sums:
            cpi_sums[k] += core_d.get(k, 0)
    cpi_total = sum(cpi_sums.values()) or 1
    cpi_l3_ratio         = cpi_sums["l3"]         / cpi_total
    cpi_dram_local_ratio = cpi_sums["dram_local"]  / cpi_total
    cpi_dram_remote_ratio= cpi_sums["dram_remote"] / cpi_total
    cpi_branch_ratio     = cpi_sums["branch"]      / cpi_total

    # sync stall (performance_model)
    conn = _db(out_str)
    cpi_sync = 0.0
    if conn:
        from utility.stats_reader import _query
        sync_vals = _query(conn, "stop", "performance_model", "cpiSyncFutex") + \
                    _query(conn, "stop", "performance_model", "cpiSyncSyscall")
        cpi_sync = sum(v for _, v in sync_vals)
        conn.close()
    cpi_sync_ratio = cpi_sync / (cpi_total + cpi_sync) if (cpi_total + cpi_sync) > 0 else 0.0

    # ── μop 命令ミックス ───────────────────────────────────────────
    conn2 = _db(out_str)
    uop_load = uop_store = uop_fp = uops_total = 0
    if conn2:
        from utility.stats_reader import _query
        def _sum_metric(obj, met):
            return sum(v for _, v in _query(conn2, "stop", obj, met))
        uop_load   = _sum_metric("interval_timer", "uop_load")
        uop_store  = _sum_metric("interval_timer", "uop_store")
        uop_fp     = _sum_metric("interval_timer", "uop_fp_muldiv")
        uops_total = _sum_metric("interval_timer", "uops_total") or 1
        conn2.close()
    uop_load_ratio  = uop_load  / uops_total
    uop_store_ratio = uop_store / uops_total
    uop_fp_ratio    = uop_fp    / uops_total

    # ── TLB ミス率 ────────────────────────────────────────────────
    conn3 = _db(out_str)
    dtlb_miss_rate = 0.0
    if conn3:
        from utility.stats_reader import _query
        dtlb_access = sum(v for _, v in _query(conn3, "stop", "dtlb", "access")) or 1
        dtlb_miss   = sum(v for _, v in _query(conn3, "stop", "dtlb", "miss"))
        dtlb_miss_rate = dtlb_miss / dtlb_access
        conn3.close()

    # ── ブランチ予測失敗率 ─────────────────────────────────────────
    conn4 = _db(out_str)
    branch_mispredict_rate = 0.0
    if conn4:
        from utility.stats_reader import _query
        bp_correct   = sum(v for _, v in _query(conn4, "stop", "branch_predictor", "num-correct"))
        bp_incorrect = sum(v for _, v in _query(conn4, "stop", "branch_predictor", "num-incorrect"))
        bp_total = bp_correct + bp_incorrect
        branch_mispredict_rate = bp_incorrect / bp_total if bp_total > 0 else 0.0
        conn4.close()

    # ── DRAM レイテンシ ───────────────────────────────────────────
    conn5 = _db(out_str)
    avg_dram_latency = dram_queue_ratio = 0.0
    if conn5:
        from utility.stats_reader import _query
        dram_reads_n   = sum(v for _, v in _query(conn5, "stop", "dram", "reads")) or 1
        dram_lat_total = sum(v for _, v in _query(conn5, "stop", "dram", "total-access-latency"))
        dq_requests    = sum(v for _, v in _query(conn5, "stop", "dram-queue", "num-requests")) or 1
        dq_delay       = sum(v for _, v in _query(conn5, "stop", "dram-queue", "total-queue-delay"))
        dq_used        = sum(v for _, v in _query(conn5, "stop", "dram-queue", "total-time-used")) or 1
        avg_dram_latency = dram_lat_total / dram_reads_n
        dram_queue_ratio = dq_delay / dq_used
        conn5.close()

    # ── L1D ヒット率 ──────────────────────────────────────────────
    l1d_total = l1d_hit = 0
    for core_d in l1d_where.values():
        hits    = core_d.get("l1", 0) + core_d.get("l1s", 0)
        total_c = sum(core_d.values())
        l1d_hit   += hits
        l1d_total += total_c
    l1d_hit_ratio = l1d_hit / l1d_total if l1d_total > 0 else 0.0

    # ── 同期頻度 ──────────────────────────────────────────────────
    from utility.stats_reader import parse_sync_stats
    sync_s = parse_sync_stats(out_str)
    futex_wake_per_minst = sync_s.get("futex_wake_per_minst", 0.0)

    # ── メモリ帯域・Roofline ───────────────────────────────────────
    conn_bw = _db(out_str)
    mem_bw_mbps = operational_intensity = itlb_miss_rate = stlb_miss_rate = load_l1_ratio = 0.0
    if conn_bw:
        from utility.stats_reader import _query
        def _s(obj, met):
            return sum(v for _, v in _query(conn_bw, "stop", obj, met))
        elapsed_fs  = max((v for _, v in _query(conn_bw, "stop", "performance_model", "elapsed_time")), default=1)
        elapsed_s   = elapsed_fs / 1e15
        dram_reads  = _s("dram", "reads")
        dram_writes = _s("dram", "writes")
        dram_bytes  = (dram_reads + dram_writes) * 64
        mem_bw_mbps = dram_bytes / 1e6 / elapsed_s if elapsed_s > 0 else 0.0

        uop_fp_add = _s("interval_timer", "uop_fp_addsub")
        uop_fp_mul = _s("interval_timer", "uop_fp_muldiv")
        operational_intensity = (uop_fp_add + uop_fp_mul) / dram_bytes if dram_bytes > 0 else 0.0

        itlb_acc       = _s("itlb", "access") or 1
        itlb_miss_rate = _s("itlb", "miss") / itlb_acc

        stlb_acc       = _s("stlb", "access") or 1
        stlb_miss_rate = _s("stlb", "miss") / stlb_acc

        lc_l1    = _s("interval_timer", "cpContr_load_l1")
        lc_l2    = _s("interval_timer", "cpContr_load_l2")
        lc_l3    = _s("interval_timer", "cpContr_load_l3")
        lc_ot    = _s("interval_timer", "cpContr_load_other")
        lc_total = lc_l1 + lc_l2 + lc_l3 + lc_ot or 1
        load_l1_ratio = lc_l1 / lc_total
        conn_bw.close()

    return {
        "sim_seconds":  sim_seconds,
        "energy_j":     energy_j,
        # IPC 系
        "avg_ipc":      avg_ipc,
        "ipc_cv":       ipc_cv,
        "pe_ratio":     pe_ratio,
        # NUMA アクセス
        "local_ratio":  local_ratio,
        "remote_ratio": remote_ratio,
        # キャッシュ階層強度
        "l2_intensity": l2_intensity,
        "l3_intensity": l3_intensity,
        "mem_intensity":mem_intensity,
        "read_ratio":   read_ratio,
        # CPI 内訳
        "cpi_l3_ratio":          cpi_l3_ratio,
        "cpi_dram_local_ratio":  cpi_dram_local_ratio,
        "cpi_dram_remote_ratio": cpi_dram_remote_ratio,
        "cpi_branch_ratio":      cpi_branch_ratio,
        "cpi_sync_ratio":        cpi_sync_ratio,
        # μop 命令ミックス
        "uop_load_ratio":  uop_load_ratio,
        "uop_store_ratio": uop_store_ratio,
        "uop_fp_ratio":    uop_fp_ratio,
        # TLB・ブランチ
        "dtlb_miss_rate":        dtlb_miss_rate,
        "branch_mispredict_rate":branch_mispredict_rate,
        # DRAM レイテンシ
        "avg_dram_latency": avg_dram_latency,
        "dram_queue_ratio": dram_queue_ratio,
        # L1D ヒット率
        "l1d_hit_ratio": l1d_hit_ratio,
        # 同期頻度
        "futex_wake_per_minst":    futex_wake_per_minst,
        # メモリ帯域・Roofline
        "mem_bw_mbps":             mem_bw_mbps,
        "operational_intensity":   operational_intensity,
        # TLB
        "itlb_miss_rate":          itlb_miss_rate,
        "stlb_miss_rate":          stlb_miss_rate,
        # ロード供給源
        "load_l1_ratio":           load_l1_ratio,
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


def collect_dataset_multi(
    outputs_dirs: list[Path],
    num_threads: int,
    label_by: str,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    複数の outputs_dir を結合してスレッド数固定の学習データを返す。
    bench_class_enc を追加特徴量として付与する。
    Returns: (X, y)  ─ FEATURE_COLS_MULTI 列
    """
    rows_X, rows_y, names = [], [], []
    for outputs_dir in outputs_dirs:
        bench_class = outputs_dir.name.replace("size", "")
        X_n, y_n, _ = collect_dataset(outputs_dir, num_threads, label_by)
        if X_n.empty:
            continue
        for i, idx in enumerate(X_n.index):
            feat = [_CLASS_ENC.get(bench_class, 0)] + list(X_n.iloc[i].values)
            rows_X.append(feat)
            rows_y.append(y_n.iloc[i])
            names.append(f"{idx}_{bench_class}")
    if not names:
        return pd.DataFrame(), pd.Series(dtype=str)
    cols = ["bench_class_enc"] + FEATURE_COLS
    X = pd.DataFrame(rows_X, index=names, columns=cols)
    y = pd.Series(rows_y, index=names, name="best_strategy")
    return X, y


def build_allth_data(
    label_by: str,
    outputs_dirs: list[Path] | Path = OUTPUTS_DIR,
    thread_nums: list[int] | None = None,
) -> tuple[pd.DataFrame, pd.Series]:
    if isinstance(outputs_dirs, Path):
        outputs_dirs = [outputs_dirs]
    thread_nums = thread_nums if thread_nums is not None else THREAD_NUMS
    rows_X, rows_y, names = [], [], []
    for outputs_dir in outputs_dirs:
        bench_class = outputs_dir.name.replace("size", "")  # sizeS → S, sizeW → W
        for n in thread_nums:
            X_n, y_n, _ = collect_dataset(outputs_dir, n, label_by)
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
    X_all = pd.DataFrame(rows_X, index=names, columns=cols)
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
