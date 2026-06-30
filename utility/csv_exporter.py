"""
csv_exporter.py
Sniper 実験結果から ML 用 CSV を生成する。

出力ファイル:
  output_dir/metrics.csv      — 転置形式 (metric, value) の 2 列 ×多行。人間が読みやすい。
  Documents/Data/all_metrics.csv — 横持ち形式。1 実験 = 1 行。ML/pandas 向け。

列グループ:
  [識別]      timestamp, workload, strategy, bench_class, num_threads, output_dir
  [コア構成]  cpu_map, p_core_count, e_core_count, node0_threads, node1_threads
  [性能]      sim_time_ms, total_instructions, avg_ipc, avg_ipc_p, avg_ipc_e
  [電力]      total_power_W, dynamic_W, leakage_W, dram_W, energy_J
  [NUMA]      node0_reads, node1_reads, node0_pct, dram_local, dram_remote, dram_remote_pct
  [キャッシュ] l1d_miss_pct, l2_miss_pct, l3_miss_pct, l1d_mpki, l2_mpki, l3_mpki, l3_miss_count
  [CPI内訳]   cpi_base_frac, cpi_branch_frac, cpi_l2_frac, cpi_l3_frac,
              cpi_dram_local_frac, cpi_dram_remote_frac
  [生指標]    dump_all_stats() から得られる全 SQLite3 指標 (obj.metric[.cN/.total/.node0/.node1])
"""

import csv
import json
import os
import threading
from datetime import datetime

from utility.stats_reader import (
    parse_sim_time,
    parse_ipc,
    parse_instructions,
    parse_node_stats,
    parse_numa_access,
    parse_l1d_where,
    parse_cpi_breakdown,
    dump_all_stats,
    P_CORES,
    E_CORES,
    NODE0_CPUS,
    NODE1_CPUS,
)

_lock = threading.Lock()

GLOBAL_CSV = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "Documents", "Data", "all_metrics.csv")
)

# 派生指標の列順（raw 指標はこの後にソート順で追加される）
DERIVED_COLUMNS = [
    # 識別
    "timestamp", "workload", "strategy", "bench_class", "num_threads", "output_dir",
    # コア構成
    "cpu_map", "p_core_count", "e_core_count", "node0_threads", "node1_threads",
    # 性能
    "sim_time_ms", "total_instructions", "avg_ipc", "avg_ipc_p", "avg_ipc_e",
    # 電力
    "total_power_W", "dynamic_W", "leakage_W", "dram_W", "energy_J",
    # NUMA
    "node0_reads", "node1_reads", "node0_pct",
    "dram_local", "dram_remote", "dram_remote_pct",
    # キャッシュ
    "l1d_miss_pct", "l2_miss_pct", "l3_miss_pct",
    "l1d_mpki", "l2_mpki", "l3_mpki", "l3_miss_count",
    # CPI内訳フラクション
    "cpi_base_frac", "cpi_branch_frac", "cpi_l2_frac", "cpi_l3_frac",
    "cpi_dram_local_frac", "cpi_dram_remote_frac",
]


# ─── ヘルパ ─────────────────────────────────────────────────────

def _pct(num, den) -> float:
    return _r(num / den * 100) if den > 0 else 0.0


def _r(v, n=4) -> float:
    return round(float(v), n)


def _core_config(cpu_map: list, num_threads: int) -> dict:
    active = cpu_map[:num_threads]
    return {
        "cpu_map":       json.dumps(active),
        "p_core_count":  sum(1 for c in active if c in P_CORES),
        "e_core_count":  sum(1 for c in active if c in E_CORES),
        "node0_threads": sum(1 for c in active if c in NODE0_CPUS),
        "node1_threads": sum(1 for c in active if c in NODE1_CPUS),
    }


def _cache_stats(output_dir: str, num_threads: int) -> dict:
    """loads-where-* からキャッシュミス率・MPKI を集計する。"""
    per_core = parse_l1d_where(output_dir)
    inst_map = parse_instructions(output_dir)
    total_insts = sum(inst_map.values()) if inst_map else 0

    agg = {k: 0 for k in ["l1", "l1s", "l2", "l3", "l3s", "dram_local", "dram_remote"]}
    for d in per_core.values():
        for k in agg:
            agg[k] += d.get(k, 0)

    total_l1d = sum(agg.values())
    l1d_hits  = agg["l1"] + agg["l1s"]
    miss_l1   = total_l1d - l1d_hits
    l3_access = agg["l3"] + agg["l3s"] + agg["dram_local"] + agg["dram_remote"]
    miss_l2   = l3_access
    miss_l3   = agg["dram_local"] + agg["dram_remote"]

    kilo = total_insts / 1000 if total_insts > 0 else 1

    return {
        "l1d_miss_pct":  _pct(miss_l1, total_l1d),
        "l2_miss_pct":   _pct(miss_l2, miss_l1),
        "l3_miss_pct":   _pct(miss_l3, l3_access),
        "l1d_mpki":      _r(miss_l1 / kilo),
        "l2_mpki":       _r(miss_l2 / kilo),
        "l3_mpki":       _r(miss_l3 / kilo),
        "l3_miss_count": miss_l3,
    }


def _cpi_fracs(output_dir: str) -> dict:
    """interval_timer CPI 内訳を正規化フラクションに変換する。"""
    per_core = parse_cpi_breakdown(output_dir)
    keys = ["base", "branch", "l2", "l3", "dram_local", "dram_remote"]

    sums = {k: 0 for k in keys}
    for d in per_core.values():
        d_merged = dict(d)
        d_merged["l3"] = d.get("l3", 0) + d.get("l3s", 0)
        for k in keys:
            sums[k] += d_merged.get(k, 0)

    total = sum(sums.values())
    if total == 0:
        return {f"cpi_{k}_frac": 0.0 for k in keys}
    return {f"cpi_{k}_frac": _r(sums[k] / total) for k in keys}


def _ipc_by_type(output_dir: str, cpu_map: list, num_threads: int) -> dict:
    ipc_map = parse_ipc(output_dir, cpu_map)
    if not ipc_map:
        return {"avg_ipc": 0.0, "avg_ipc_p": 0.0, "avg_ipc_e": 0.0}

    all_vals, p_vals, e_vals = [], [], []
    for sim_core, ipc in ipc_map.items():
        cpu_id = cpu_map[sim_core] if sim_core < len(cpu_map) else sim_core
        all_vals.append(ipc)
        (p_vals if cpu_id in P_CORES else e_vals).append(ipc)

    return {
        "avg_ipc":   _r(sum(all_vals) / len(all_vals)) if all_vals else 0.0,
        "avg_ipc_p": _r(sum(p_vals)   / len(p_vals))   if p_vals   else 0.0,
        "avg_ipc_e": _r(sum(e_vals)   / len(e_vals))   if e_vals   else 0.0,
    }


# ─── メイン エクスポート ─────────────────────────────────────────

def export_csv(
    output_dir: str,
    workload: str,
    strategy: str,
    bench_class: str,
    num_threads: int,
    cpu_map: list,
    power: dict | None = None,
) -> str:
    """
    metrics.csv (転置・2列) を output_dir に書き出し、
    all_metrics.csv (横持ち) に追記する。

    Returns: 書き出した metrics.csv のパス
    """
    power = power or {}

    sim_time   = parse_sim_time(output_dir) or 0.0
    node_stats = parse_node_stats(output_dir)
    numa       = parse_numa_access(output_dir)
    inst_map   = parse_instructions(output_dir)
    total_insts = sum(inst_map.values()) if inst_map else 0

    node0_reads = node_stats.get(0, {}).get("reads", 0)
    node1_reads = node_stats.get(1, {}).get("reads", 0)
    total_reads = node0_reads + node1_reads
    dram_local  = numa.get("local", 0)
    dram_remote = numa.get("remote", 0)
    dram_total  = dram_local + dram_remote

    derived: dict = {
        # 識別
        "timestamp":      datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "workload":       workload,
        "strategy":       strategy,
        "bench_class":    bench_class,
        "num_threads":    num_threads,
        "output_dir":     os.path.relpath(
                              output_dir,
                              start=os.path.join(os.path.dirname(__file__), "..")),
        # コア構成
        **_core_config(cpu_map, num_threads),
        # 性能
        "sim_time_ms":        _r(sim_time * 1000, 6),
        "total_instructions": total_insts,
        **_ipc_by_type(output_dir, cpu_map, num_threads),
        # 電力
        "total_power_W": power.get("total_W", 0.0),
        "dynamic_W":     power.get("dynamic_W", 0.0),
        "leakage_W":     power.get("leakage_W", 0.0),
        "dram_W":        power.get("dram_W", 0.0),
        "energy_J":      power.get("energy_J", 0.0),
        # NUMA
        "node0_reads":     node0_reads,
        "node1_reads":     node1_reads,
        "node0_pct":       _pct(node0_reads, total_reads),
        "dram_local":      dram_local,
        "dram_remote":     dram_remote,
        "dram_remote_pct": _pct(dram_remote, dram_total),
        # キャッシュ
        **_cache_stats(output_dir, num_threads),
        # CPI 内訳
        **_cpi_fracs(output_dir),
    }

    # 生指標 (SQLite3 全列)
    raw = dump_all_stats(output_dir, cpu_map)
    raw_keys = sorted(raw.keys())

    # ── metrics.csv: 転置形式 (metric, value) ──────────────────
    local_csv = os.path.join(output_dir, "metrics.csv")
    with open(local_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        # 派生指標
        for k in DERIVED_COLUMNS:
            w.writerow([k, derived.get(k, "")])
        # 生指標
        for k in raw_keys:
            w.writerow([k, raw[k]])

    # ── all_metrics.csv: 横持ち形式 (ML 用) ────────────────────
    all_columns = DERIVED_COLUMNS + raw_keys
    flat_row = {k: derived.get(k, "") for k in DERIVED_COLUMNS}
    flat_row.update(raw)

    _append_global(flat_row, all_columns)

    print(f"[csv] metrics.csv 保存: {local_csv}")
    return local_csv


def _append_global(row: dict, columns: list) -> None:
    os.makedirs(os.path.dirname(GLOBAL_CSV), exist_ok=True)
    with _lock:
        write_header = not os.path.exists(GLOBAL_CSV) or os.path.getsize(GLOBAL_CSV) == 0
        with open(GLOBAL_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            if write_header:
                w.writeheader()
            w.writerow(row)
