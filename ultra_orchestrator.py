"""
ultra_orchestrator.py
資源制約付きスケジューリング (Garey & Graham 1975, P|res 1|Cmax) で、
全ワークロード×全戦略×全ベンチクラス×全スレッド数のジョブを一括投入し、
ホストの実効コア容量に対して厳密/近似スケジューリングして実行する。

orchestrator.py の設計上の欠陥（スレッド数ごとに完全逐次バッチ実行するため、
BT/SPのような重いジョブが同じバッチ内の軽いジョブの完了まで後続バッチを
ブロックする）を解消するのが目的。全ジョブを最初から1つのプールとして
スケジューリングし、空いた容量に次のジョブを即座に詰め込む。

コストモデル (project_scheduling_model メモリ参照):
  - duration (壁時計時間): utility.run_profile の実測値/推定値
  - width (実消費ホストコア数%): ワークロード種別ごとの2THベースライン ×
    スレッド数スケーリング (cost(threads) ≈ baseline × (threads/2)^0.413)
    ワークロードの実行時間そのもの(研究上の性能比較)とは無関係な、
    Sniper自体のホストCPU消費効率の話である点に注意。

スケジューリング戦略:
  - 既定は List Scheduling + LPT (長い順に、空いた瞬間に詰める)。
    最適解に対し (2 - 1/C) 以内の近似保証があり、ジョブ数が多くても高速。
  - --exact 指定時、ジョブ数が少ない場合 (既定80件未満) のみ OR-Tools CP-SAT の
    cumulative 制約で厳密解を求め、その順序をLPTの代わりに使う。
    ジョブ数が多い場合は解けないため自動的にLPTにフォールバックする。

使い方:
  python3 ultra_orchestrator.py --sizes S W --threads 2 8 12 16 \\
      --strategies Packed Scatter HPO EPO MPO --capacity 21
"""

import argparse
import os
import shutil
import threading
import time
from datetime import datetime

from config.generate_config import generate_config, get_config_path
from orchestrator            import _resolve_cpu_map, WORKLOADS as _ALL_WORKLOADS
from sniper_sim               import run_sniper
from utility.cpu_affinity    import binary_path, get_binary_args, save_affinity_config
from utility.csv_exporter    import export_csv
from utility.power_model     import estimate as estimate_power
from utility.run_profile     import get_reference, update_from_run, estimate_walltime
from utility.stats_reader    import parse_node_stats

# 重複("BC"が2回)を除いた正式な13ワークロード
WORKLOADS = list(dict.fromkeys(_ALL_WORKLOADS))

OUTPUT_BASE_TMPL = "/home/hiragahama/ClaudeXSniper/Outputs/size{cls}"

# ============================================================
# コストモデル
# ============================================================

# ワークロード種別ごとの実消費ホストコア(%) @ 2TH実測 (project_scheduling_model参照)
_WIDTH_BASELINE_2TH = {
    "BT": 133, "FT": 117, "SP": 116, "IS": 102, "MG": 100, "CG": 89,
    "BFS": 57, "PR": 57, "BC": 57, "CC": 57, "SSSP": 57, "TC": 57,
    "lavaMD": 13,
}
_WIDTH_EXPONENT = 0.413  # BTの実測4点フィット cost(threads)≈99.6×threads^0.413 の指数部を流用


def host_width_pct(workload: str, num_threads: int) -> float:
    """このワークロード・スレッド数がホストの実コアを何%消費するかの推定値。"""
    baseline = _WIDTH_BASELINE_2TH.get(workload, 100)
    scale = (num_threads / 2) ** _WIDTH_EXPONENT
    return baseline * scale


def job_duration_sec(workload: str, bench_class: str, num_threads: int) -> float:
    """壁時計時間の推定 (実測があれば実測、無ければ utility.run_profile の推定式)。"""
    ref = get_reference(workload, bench_class, num_threads)
    if ref:
        return ref["wallTime"]
    est = estimate_walltime(workload, bench_class, num_threads)
    return est if est is not None else 3600.0  # 完全に未知なら1時間と仮定


# ============================================================
# ジョブ定義とスケジューリング
# ============================================================

class Job:
    __slots__ = ("workload", "strategy", "bench_class", "num_threads", "width", "duration")

    def __init__(self, workload, strategy, bench_class, num_threads):
        self.workload    = workload
        self.strategy    = strategy
        self.bench_class = bench_class
        self.num_threads = num_threads
        # width はコア等価数 (capacity と同じ単位)。host_width_pct は%を返すので /100 する。
        self.width       = host_width_pct(workload, num_threads) / 100.0
        self.duration    = job_duration_sec(workload, bench_class, num_threads)

    def __repr__(self):
        return (f"Job({self.workload}/{self.strategy}/{self.bench_class}/"
                f"{self.num_threads}TH, w={self.width:.2f}core, d={self.duration:.0f}s)")


def build_jobs(workloads, strategies, bench_classes, thread_counts) -> list[Job]:
    jobs = []
    for cls in bench_classes:
        for th in thread_counts:
            for st in strategies:
                for wl in workloads:
                    jobs.append(Job(wl, st, cls, th))
    return jobs


def lpt_order(jobs: list[Job]) -> list[Job]:
    """List Scheduling + LPT: durationの長い順。"""
    return sorted(jobs, key=lambda j: j.duration, reverse=True)


def cpsat_order(jobs: list[Job], capacity: float, time_limit_sec: float = 30.0) -> list[Job] | None:
    """
    OR-Tools CP-SAT の cumulative 制約で P|res 1|Cmax の厳密/近似解を求め、
    各ジョブの開始時刻順を返す。求解に失敗/タイムアウトした場合は None。
    """
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        return None

    model = cp_model.CpModel()
    horizon = int(sum(j.duration for j in jobs)) + 1
    starts, ends, intervals = [], [], []
    for j in jobs:
        dur = max(int(j.duration), 1)
        start = model.NewIntVar(0, horizon, "start")
        end   = model.NewIntVar(0, horizon, "end")
        interval = model.NewIntervalVar(start, dur, end, "interval")
        starts.append(start)
        ends.append(end)
        intervals.append(interval)

    # width はコア単位の小数 (例: 1.33) なので、CP-SAT の整数制約用に100倍して丸める
    _SCALE = 100
    demands = [max(int(round(j.width * _SCALE)), 1) for j in jobs]
    model.AddCumulative(intervals, demands, int(capacity * _SCALE))

    makespan = model.NewIntVar(0, horizon, "makespan")
    model.AddMaxEquality(makespan, ends)
    model.Minimize(makespan)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_sec
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    order = sorted(range(len(jobs)), key=lambda i: solver.Value(starts[i]))
    return [jobs[i] for i in order]


class _CapacityPool:
    """容量(実効コア数)を超えないよう、ジョブのwidthをゲートする貪欲リストスケジューラの資源プール。"""

    def __init__(self, capacity: float):
        self.capacity = capacity
        self.used = 0.0
        self._cond = threading.Condition()

    def acquire(self, width: float) -> None:
        with self._cond:
            while self.used > 0 and self.used + width > self.capacity:
                self._cond.wait()
            self.used += width

    def release(self, width: float) -> None:
        with self._cond:
            self.used -= width
            self._cond.notify_all()


# ============================================================
# ジョブ実行 (orchestrator.py の run_one 相当)
# ============================================================

def run_job(job: Job, run_id: str, no_timeout: bool = True) -> str | None:
    output_base = OUTPUT_BASE_TMPL.format(cls=job.bench_class)
    cpu_map  = _resolve_cpu_map(job.strategy, job.workload, job.bench_class, job.num_threads)
    bin_path = binary_path(job.workload, job.bench_class)
    bin_args = get_binary_args(job.workload, job.bench_class, job.num_threads)

    out_dir = os.path.join(
        output_base, f"{job.num_threads}TH",
        f"{job.workload}_{job.bench_class}_{job.strategy}_{job.num_threads}TH_{run_id}",
    )
    os.makedirs(out_dir, exist_ok=True)

    cfg_path = get_config_path(out_dir, job.strategy, job.num_threads)
    generate_config(job.strategy, job.num_threads, cpu_map, cfg_path)
    save_affinity_config(out_dir, job.strategy, job.workload, job.bench_class,
                         cpu_map, job.num_threads)

    log_path = os.path.join(out_dir, "sniper.log")
    start = time.time()
    with open(log_path, "w") as log_file:
        ret = run_sniper(
            binary_path=bin_path, binary_args=bin_args, num_threads=job.num_threads,
            cpu_map=cpu_map, strategy=job.strategy, output_dir=out_dir,
            config_path=cfg_path, log_file=log_file, workload=job.workload,
        )
    elapsed = time.time() - start

    if ret != 0:
        print(f"[ERROR] {job} 失敗 ret={ret}", flush=True)
        return None

    power = estimate_power(out_dir, cpu_map, job.num_threads)
    update_from_run(job.workload, job.bench_class, job.num_threads, out_dir, elapsed)
    export_csv(out_dir, job.workload, job.strategy, job.bench_class, job.num_threads, cpu_map, power)
    return out_dir


def _run_and_release(job: Job, pool: _CapacityPool, run_id: str, counters: dict, lock: threading.Lock):
    try:
        out_dir = run_job(job, run_id)
        with lock:
            counters["done"] += 1
            status = "OK" if out_dir else "FAILED"
            print(f"[{counters['done']}/{counters['total']}] {job} -> {status}", flush=True)
    finally:
        pool.release(job.width)


def schedule_and_run(jobs: list[Job], capacity: float, run_id: str, use_exact: bool,
                     exact_threshold: int = 80) -> None:
    if use_exact and len(jobs) < exact_threshold:
        print(f"[SCHED] CP-SAT厳密スケジューリングを試行 ({len(jobs)}件)...")
        ordered = cpsat_order(jobs, capacity)
        if ordered is None:
            print("[SCHED] CP-SATが時間内に解けず、LPTにフォールバック")
            ordered = lpt_order(jobs)
        else:
            print("[SCHED] CP-SAT厳密解を採用")
    else:
        if use_exact:
            print(f"[SCHED] ジョブ数{len(jobs)}件はCP-SATには多すぎるため、LPTを使用")
        ordered = lpt_order(jobs)

    pool = _CapacityPool(capacity)
    counters = {"done": 0, "total": len(jobs)}
    lock = threading.Lock()

    print(f"\n{'='*64}")
    print(f"  ULTRA ORCHESTRATOR — 資源制約付きスケジューリング実行")
    print(f"  ジョブ数={len(jobs)}  実効容量={capacity}コア")
    print(f"{'='*64}\n")

    threads = []
    for job in ordered:
        pool.acquire(job.width)
        t = threading.Thread(target=_run_and_release, args=(job, pool, run_id, counters, lock))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    print(f"\n[完了] {counters['done']}/{counters['total']} 件処理")


# ============================================================
# CLI
# ============================================================

def _parse_args():
    p = argparse.ArgumentParser(description="資源制約付きスケジューリングで全ジョブを一括実行する")
    p.add_argument("--sizes",       nargs="+", default=["S"], help="ベンチクラス (例: S W)")
    p.add_argument("--threads",     nargs="+", type=int, default=[2, 8, 12, 16])
    p.add_argument("--strategies",  nargs="+", default=["Packed", "Scatter", "HPO", "EPO", "MPO"])
    p.add_argument("--workloads",   nargs="+", default=WORKLOADS)
    p.add_argument("--capacity",    type=float, default=21.0, help="ホスト実効コア数上限")
    p.add_argument("--exact",       action="store_true", help="ジョブ数が少なければCP-SAT厳密解を試す")
    return p.parse_args()


def main():
    args = _parse_args()
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    jobs = build_jobs(args.workloads, args.strategies, args.sizes, args.threads)
    print(f"[JOBS] {len(jobs)}件のジョブを生成 "
          f"(workloads={len(args.workloads)}, strategies={len(args.strategies)}, "
          f"sizes={args.sizes}, threads={args.threads})")

    schedule_and_run(jobs, args.capacity, run_id, use_exact=args.exact)


if __name__ == "__main__":
    main()
