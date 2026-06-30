"""
run.py
ClaudeXSniper の単発実験エントリーポイント。
orchestrator を使わずに 1 ワークロード × 1 戦略を直接実行する。

  python3 run.py
  python3 run.py --workload IS --strategy Packed --threads 4 --class S
"""

import argparse
import os
import time
from datetime import datetime

from config.generate_config  import generate_config, get_config_path
from utility.cpu_affinity    import get_cpu_map, binary_path, get_binary_args, save_affinity_config
from utility.csv_exporter    import export_csv
from utility.grapher         import generate_numa_graph
from utility.power_model     import estimate as estimate_power
from utility.run_profile     import update_from_run
from utility.stats_reader    import parse_node_stats, parse_sim_time, print_summary
from sniper_sim              import run_sniper

# ============================================================
# 実験設定（CLI 引数で上書き可能）
# ============================================================
WORKLOAD    = "IS"
STRATEGY    = "Packed"
BENCH_CLASS = "S"
NUM_THREADS = 4

NUM_NODES = 2
BIG_CPN   = 4
SML_CPN   = 4

OUTPUT_BASE = "/home/hiragahama/ClaudeXSniper/Outputs"
# ============================================================


def _parse_args():
    p = argparse.ArgumentParser(description="ClaudeXSniper 単発実験")
    p.add_argument("--workload",  default=WORKLOAD,    help=f"ワークロード名 (default: {WORKLOAD})")
    p.add_argument("--strategy",  default=STRATEGY,    help=f"戦略 (default: {STRATEGY})")
    p.add_argument("--threads",   type=int, default=NUM_THREADS)
    p.add_argument("--class",     dest="bench_class",  default=BENCH_CLASS)
    return p.parse_args()


def main():
    args = _parse_args()
    workload    = args.workload
    strategy    = args.strategy
    num_threads = args.threads
    bench_class = args.bench_class

    run_id  = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(
        OUTPUT_BASE,
        f"size{bench_class}",
        f"{num_threads}TH",
        f"{workload}_{bench_class}_{strategy}_{num_threads}TH_{run_id}",
    )
    os.makedirs(out_dir, exist_ok=True)

    cpu_map  = get_cpu_map(strategy, workload)
    bin_path = binary_path(workload, bench_class)
    bin_args = get_binary_args(workload, bench_class, num_threads)

    print(f"\n{'='*60}")
    print(f"  ClaudeXSniper — 単発実験")
    print(f"  ワークロード : {workload}")
    print(f"  戦略         : {strategy}")
    print(f"  スレッド数   : {num_threads}")
    print(f"  クラス       : {bench_class}")
    print(f"  cpu_map      : {cpu_map[:num_threads]}")
    print(f"  出力先       : {out_dir}")
    print(f"{'='*60}\n")

    cfg_path = get_config_path(out_dir, strategy, num_threads)
    generate_config(strategy, num_threads, cpu_map, cfg_path)
    print(f"[設定] {cfg_path}")

    save_affinity_config(
        out_dir, strategy, workload, bench_class,
        cpu_map, num_threads, NUM_NODES, BIG_CPN, SML_CPN,
    )

    log_path = os.path.join(out_dir, "sniper.log")
    print(f"[実行] {bin_path} → ログ: {log_path}")

    start = time.time()
    with open(log_path, "w") as log_file:
        ret = run_sniper(
            binary_path   = bin_path,
            binary_args   = bin_args,
            num_threads   = num_threads,
            cpu_map       = cpu_map,
            strategy      = strategy,
            output_dir    = out_dir,
            config_path   = cfg_path,
            log_file      = log_file,
            workload      = workload,
        )
    elapsed = time.time() - start

    if ret != 0:
        print(f"\n[ERROR] Sniper が非ゼロ終了: ret={ret}  ({elapsed:.1f}s)")
        print(f"        ログ確認: {log_path}")
        return

    print(f"\n[完了] 実行時間: {elapsed:.1f}s")

    # ── 統計収集 ──
    sim_seconds = parse_sim_time(out_dir)
    node_stats  = parse_node_stats(out_dir)
    power       = estimate_power(out_dir, cpu_map, num_threads)

    print_summary(node_stats, cpu_map, num_threads, out_dir)

    if sim_seconds:
        print(f"\n  シミュレーション時間: {sim_seconds:.6f} s")
    if power:
        print(f"  推定電力: {power['total_W']:.3f} W  "
              f"エネルギー: {power['energy_J']:.4f} J")

    update_from_run(workload, bench_class, num_threads, out_dir, elapsed)
    export_csv(out_dir, workload, strategy, bench_class, num_threads, cpu_map, power)

    generate_numa_graph(out_dir, node_stats, strategy, cpu_map,
                        num_threads, workload, bench_class)

    print(f"\n[出力先] {out_dir}")


if __name__ == "__main__":
    main()
