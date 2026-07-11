"""
ultra_orchestrator.py
資源制約付きスケジューリング (Garey & Graham 1975, P|res 1|Cmax) で、
全ワークロード×全戦略×全ベンチクラス×全スレッド数のジョブを一括投入し、
ホストの実効コア容量に対して厳密/近似スケジューリングして実行する。

全ジョブを最初から1つのプールとしてスケジューリングし、空いた容量に次の
ジョブを即座に詰め込む(スレッド数ごとに完全逐次バッチ実行する設計だと、
BT/SPのような重いジョブが同じバッチ内の軽いジョブの完了まで後続バッチを
ブロックしてしまうため)。

コストモデル(ワークロードごとの資源消費・所要時間の見積もり、project_scheduling_model
メモリ参照)は`utility.capacity_model`、スケジューリングアルゴリズム(LPT/CP-SAT/
容量プール、Sniperの中身を一切知らない汎用ロジック)は`utility.scheduling`に
切り出してある(2026-07-09)。本ファイルには「どのワークロードにどの戦略を
割り当て、どう実行するか」という指揮そのものだけを残す。

スケジューリング戦略:
  - 既定は List Scheduling + LPT (長い順に、空いた瞬間に詰める)。
    最適解に対し (2 - 1/C) 以内の近似保証があり、ジョブ数が多くても高速。
  - --exact 指定時、ジョブ数が少ない場合 (既定80件未満) のみ OR-Tools CP-SAT の
    cumulative 制約で厳密解を求め、その順序をLPTの代わりに使う。
    ジョブ数が多い場合は解けないため自動的にLPTにフォールバックする。

使い方:
  python3 ultra_orchestrator.py --threads 2 8 12 16 --bench-class S W
  python3 ultra_orchestrator.py --threads 8 --bench-class A --strategies Packed HPO
"""

import argparse
import os
import sys
import threading
import time
from datetime import datetime

from config.generate_config import generate_config, get_config_path
from utility.sniper_sim_sid   import run_sniper as run_sniper_sid
from utility.sniper_sim_purple import run_sniper as run_sniper_purple
from utility.sniper_sim_purple import REMOTE_CFG_ROOT
from utility.cpu_affinity    import (resolve_cpu_map, binary_path, get_binary_args,
                                      save_affinity_config, needs_stdin, write_stdin_file,
                                      resolve_valid_num_threads)
from utility.csv_exporter    import export_csv
from utility.notify          import notify
from utility.power_model     import estimate as estimate_power
from utility.run_profile     import get_reference, update_from_run
from utility.stats_reader    import parse_node_stats
from utility.capacity_model  import (host_width_pct, live_sid_load_cores, live_purple_load_cores,
                                      live_sid_loadavg, live_purple_loadavg,
                                      job_duration_sec, SID_CAPACITY_DEFAULT, PURPLE_CAPACITY_DEFAULT,
                                      SID_HARD_LIMIT_CORES, PURPLE_HARD_LIMIT_CORES,
                                      SID_LOADAVG_HARD_LIMIT, PURPLE_LOADAVG_HARD_LIMIT, HEAVY_WORKLOADS)
from utility.scheduling      import lpt_order, cpsat_order, _CapacityPool, estimate_makespan

# ============================================================
# 実験設定（CLI 引数で上書き可能。orchestrator.py と同じ流儀）
# ============================================================
# 除外したワークロード(GAPBS系BFS/PR/TC/BC/CC/SSSP、lavaMD、fluidanimate、
# water_nsquared、x264、bodytrack)の除外理由はDocuments/2026年7月6日.md・
# 2026年7月7日.md・2026年7月10日.md参照(いずれもSniper本体のfutexデッドロックや
# Pin計装との非互換)。CG/LUの追加経緯はDocuments/2026年7月8日.md参照。
WORKLOADS = ["BT", "FT", "IS", "MG", "CG", "LU",
             "canneal", "dedup", "GUPS"]

STRATEGIES_TO_RUN = ["Packed", "Scatter", "HPO", "EPO", "MPO"]
THREAD_COUNTS     = [2, 8, 12, 16]
BENCH_CLASSES     = ["W"]

# 実行先マシン。WORKLOADSと同じ長さの配列で位置対応させる(2026-07-09、戦略軸から
# ワークロード軸に変更。実際に重い/軽いはワークロードで決まるため
# 、utility.capacity_model.HEAVY_WORKLOADSと同じ軸で揃えた)。
#   --machine省略時: HEAVY_WORKLOADSに基づき自動振り分け(重量級→sid、それ以外→purple)
#   --machine sid: 全ワークロードをsidで実行
#   --machine sid sid purple purple: WORKLOADSと同数で位置対応
AKARIN_STRATEGY_TOKENS = {"akarin"}

# SID_CAPACITY_DEFAULT/PURPLE_CAPACITY_DEFAULT/SID_HARD_LIMIT_CORES/PURPLE_HARD_LIMIT_CORES
# はutility.capacity_modelに定義(2026-07-09切り出し、importで参照)。

CLAUDEXSNIPER_DIR = "/home/hiragahama/ClaudeXSniper"
OUTPUT_BASE_TMPL  = "/home/hiragahama/ClaudeXSniper/Outputs/size{cls}"
VALID_THREAD_COUNTS = {2, 4, 6, 8, 12, 16, 32}
VALID_BENCH_CLASSES = {"S", "W", "A", "B", "C", "D"}
VALID_MACHINES = {"sid", "purple"}  # sid = hiragahama(このホストの実際のhostname)

# マシンごとの run_sniper 実装 (マシン切替機構)
RUN_SNIPER_BACKENDS = {
    "sid":    run_sniper_sid,
    "purple": run_sniper_purple,
}

TIMEOUT_LOG = os.path.join(CLAUDEXSNIPER_DIR, "logs", "timeoutwl.log")


def _log_timeout(workload: str, strategy: str, bench_class: str, num_threads: int,
                 elapsed: float, timeout_sec: float) -> None:
    line = (
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  "
        f"{workload:<8} {strategy:<8} Class={bench_class}  {num_threads}TH  "
        f"elapsed={elapsed:.0f}s  timeout={timeout_sec:.0f}s  FAILED  [ultra]\n"
    )
    os.makedirs(os.path.dirname(TIMEOUT_LOG), exist_ok=True)
    with open(TIMEOUT_LOG, "a") as f:
        f.write(line)


def _validate(thread_list: list[int], class_list: list[str]) -> None:
    bad_threads = [t for t in thread_list if t not in VALID_THREAD_COUNTS]
    bad_classes = [c for c in class_list  if c not in VALID_BENCH_CLASSES]
    errors = []
    if bad_threads:
        errors.append(f"不正なスレッド数: {bad_threads}  有効値: {sorted(VALID_THREAD_COUNTS)}")
    if bad_classes:
        errors.append(f"不正なベンチクラス: {bad_classes}  有効値: {sorted(VALID_BENCH_CLASSES)}")
    if errors:
        for msg in errors:
            print(f"[ERROR] {msg}", file=sys.stderr)
        sys.exit(1)


# ============================================================
# ジョブ定義とスケジューリング
# ============================================================
# コストモデル(host_width_pct/live_*_load_cores/job_duration_sec)は
# utility.capacity_model に切り出し済み(2026-07-09、import参照)。

class Job:
    __slots__ = ("workload", "strategy", "bench_class", "num_threads", "width", "duration",
                 "backend", "cpu_map", "provenance")

    def __init__(self, workload, strategy, bench_class, num_threads,
                backend="sid", cpu_map=None, provenance=None):
        self.workload    = workload
        self.strategy    = strategy
        self.bench_class = bench_class
        self.num_threads = num_threads
        self.backend     = backend
        # cpu_map を明示指定した場合はそれを使う (AKARIN候補ジョブ)。
        # None なら run_job 側で strategy 名から resolve_cpu_map する (既存5戦略ジョブ)。
        self.cpu_map     = cpu_map
        # 元となった候補ラベル一覧 (AKARINジョブの由来記録。affinity_config.txtに残す)
        self.provenance  = provenance
        self.duration    = job_duration_sec(workload, bench_class, num_threads, backend)

        if backend == "purple":
            # Purpleは実消費コア%の重み付けモデルを持たないため、生スレッド数を
            # そのままcapacity単位として使う (実使用スレッド数=num_threadsは不変)。
            self.width = float(num_threads)
        else:
            # width はコア等価数 (capacity と同じ単位)。host_width_pct は%を返すので /100 する。
            self.width = host_width_pct(workload, num_threads) / 100.0

    def __repr__(self):
        return (f"Job({self.workload}/{self.strategy}/{self.bench_class}/"
                f"{self.num_threads}TH@{self.backend}, w={self.width:.2f}, d={self.duration:.0f}s)")


def _build_akarin_jobs_for_workload(workload: str, bench_class: str, num_threads: int,
                                    backend: str) -> list[Job]:
    """
    1つのワークロードについて、akarin.generate_candidates で計算したAKARIN候補
    cpu_mapが既存5戦略(Packed/Scatter/HPO/EPO/MPO)のいずれとも一致しない場合に
    限りJob化する。完全に既存戦略と同一の候補は既存パイプラインでカバー済み
    なので、ここでは重複実行しない。

    2026-07-11: AKARIN数式からalphaを廃した(akarin/cpsat_mapper.py、ルーフライン
    モデルへ刷新)ことで、CP-SATの解は決定的に1点になった。以前は候補ラベルが
    "alpha=0.35"のようなalpha点だったが、今は単一の"AKARIN"ラベルになる。
    """
    from akarin.generate_candidates import generate_candidates

    jobs = []
    candidates = generate_candidates(workload, bench_class, num_threads)
    for entry in candidates.values():
        labels = entry["labels"]
        if "AKARIN" not in labels:
            continue  # 既存5戦略と完全一致 → スキップ(重複実行防止)
        jobs.append(Job(workload, "AKARIN", bench_class, num_threads, backend=backend,
                        cpu_map=entry["cpu_map"], provenance=labels))
    return jobs


def build_jobs(workloads, strategies, bench_classes, thread_counts, backend="sid") -> list[Job]:
    """
    strategies には既存5戦略(Packed/Scatter/HPO/EPO/MPO)に加え、AKARIN候補生成を
    意味する特別な戦略トークン akarin を混在指定できる(全ワークロード対象)。

    2026-07-11: 以前は akarin_h(重量級のみ、粗いalphaグリッド)/ akarin_l(軽量級
    のみ、密なalphaグリッド)の2トークンに分かれていたが、AKARIN数式からalphaを
    廃した(ルーフラインモデルへ刷新)ことでグリッド密度という区別が消滅し、
    残っていたのは「重量級/軽量級でワークロードを絞る」というフィルタだけ
    だった。h+lは常にセットで指定され重量級/軽量級は互いに排他的な分割のため、
    分ける実益がなく単一トークンに統合した(ユーザー判断)。
    """
    jobs = []
    for cls in bench_classes:
        # ワークロードごとに、要求されたthread_counts(標準2/8/12/16等)を実現
        # 可能な値に変換する(dedupの3n+3制約など、resolve_valid_num_threads
        # 参照)。複数のnominal値が同じ実測値に丸められた場合は重複排除する
        # (2026-07-10、ユーザー提案: スキップではなく変換方式にすることで、
        # dedupのようなワークロードでも標準リストから4点相当のカバレッジを
        # 別バッチを組まずに得られるようにした)。
        resolved_threads = {
            wl: sorted(set(resolve_valid_num_threads(wl, th) for th in thread_counts))
            for wl in workloads
        }
        for st in strategies:
            if st == "akarin":
                for wl in workloads:
                    for th in resolved_threads[wl]:
                        jobs.extend(_build_akarin_jobs_for_workload(wl, cls, th, backend))
            else:
                for wl in workloads:
                    for th in resolved_threads[wl]:
                        jobs.append(Job(wl, st, cls, th, backend=backend))
    return jobs


# lpt_order/cpsat_order/_CapacityPool は utility.scheduling に切り出し済み
# (2026-07-09、import参照)。

# ============================================================
# ジョブ実行 (タイムアウト処理含む)
# ============================================================

def run_job(job: Job, run_id: str, no_timeout: bool = False,
           timeout_multiplier: float = 2.0) -> tuple[str | None, str | None]:
    """
    戻り値: (out_dir, failure_reason)。成功時は(out_dir, None)、失敗時は
    (None, "timeout"/"error"/"deadlock")。failure_reasonは2026-07-11に
    「本バッチ完走後にタイムアウト由来の失敗だけを1回だけ自動リトライする」
    機構(schedule_and_run参照)のために追加した。crash/デッドロックは同じ
    理由で再送しても再現するだけなので、リトライ対象からは意図的に除外する。

    timeout_multiplier: 見積もりに掛ける倍率。初回実行は既定の2.0のまま
    (2026-07-11、ユーザー指示: 初回のマージンは変更しない)。本バッチ完走後の
    自動リトライ(main()参照)だけ4.0を渡す — 見積もりが外れて完走間近で
    タイムアウトする事故が繰り返し起きたため、再送時だけ余裕を広げる。
    """
    output_base = OUTPUT_BASE_TMPL.format(cls=job.bench_class)
    # job.num_threadsは常に「実スレッド数」の意味(全ワークロードで統一)。
    # canneal/dedupの-t引数への逆算はget_binary_args/resolve_cpu_map内部で行う。
    cpu_map  = job.cpu_map if job.cpu_map is not None else resolve_cpu_map(
        job.strategy, job.workload, job.bench_class, job.num_threads)
    bin_path = binary_path(job.workload, job.bench_class)
    bin_args = get_binary_args(job.workload, job.bench_class, job.num_threads)
    run_sniper = RUN_SNIPER_BACKENDS[job.backend]

    out_dir = os.path.join(
        output_base, f"{job.num_threads}TH",
        f"{job.workload}_{job.bench_class}_{job.strategy}_{job.num_threads}TH_{run_id}",
    )

    ref          = get_reference(job.workload, job.bench_class, job.num_threads, job.backend)
    expected_sec = ref["wallTime"] if ref else job.duration
    timeout_sec  = float("inf") if no_timeout else max(expected_sec * timeout_multiplier, 600)

    os.makedirs(out_dir, exist_ok=True)

    cfg_path = get_config_path(out_dir, job.strategy, job.num_threads)
    # map_fileはcfgと同じディレクトリに書き出される。SIDはそのディレクトリが
    # コンテナ内に/cfgとしてマウントされる。Purpleは呼び出し先(sniper_sim_purple.
    # run_sniper)がcfgと同様にscpで転送するため、転送後の絶対パス(REMOTE_CFG_ROOT
    # 配下)を渡す。
    map_basename = f"{os.path.splitext(os.path.basename(cfg_path))[0]}.map"
    map_container_path = (
        f"{REMOTE_CFG_ROOT}/{map_basename}" if job.backend == "purple"
        else f"/cfg/{map_basename}"
    )
    generate_config(job.strategy, job.num_threads, cpu_map, cfg_path, map_container_path)
    save_affinity_config(out_dir, job.strategy, job.workload, job.bench_class,
                         cpu_map, job.num_threads)
    stdin_path = None
    if needs_stdin(job.workload):
        stdin_path = write_stdin_file(job.workload, job.bench_class, job.num_threads, out_dir)
    if job.provenance:
        with open(os.path.join(out_dir, "affinity_config.txt"), "a") as f:
            f.write(f"\n# AKARIN候補由来: {', '.join(job.provenance)}\n")
            f.write(f"# backend={job.backend}\n")

    log_path = os.path.join(out_dir, "sniper.log")
    log_file = open(log_path, "w")

    start       = time.time()
    done_flag   = [False]
    ret_code    = [None]
    proc_holder = []
    timed_out   = [False]

    def _do_run():
        ret_code[0] = run_sniper(
            binary_path=bin_path, binary_args=bin_args,
            num_threads=job.num_threads, cpu_map=cpu_map,
            strategy=job.strategy, output_dir=out_dir,
            config_path=cfg_path, log_file=log_file,
            workload=job.workload, proc_holder=proc_holder,
            stdin_path=stdin_path,
        )
        done_flag[0] = True

    t = threading.Thread(target=_do_run, daemon=True)
    t.start()
    while not done_flag[0]:
        elapsed = time.time() - start
        if elapsed > timeout_sec and proc_holder:
            timed_out[0] = True
            proc_holder[0].kill()
            break
        time.sleep(1)
    t.join()
    log_file.close()
    elapsed = time.time() - start

    if timed_out[0]:
        _log_timeout(job.workload, job.strategy, job.bench_class, job.num_threads,
                    elapsed, timeout_sec)
        print(f"[TIMEOUT] {job} ({elapsed:.0f}s > {timeout_sec:.0f}s)", flush=True)
        return None, "timeout"

    if ret_code[0] != 0:
        # ret=255はsniper_sim_purple.pyのSSH/scp起動失敗を意味することが多い。
        # Purple向けジョブを大量に同時起動するとsshdのMaxStartups(既定10:30:100)
        # に引っかかり接続がランダムに拒否されることがあると2026-07-06に判明
        # (起動直後のバーストでのみ発生、数秒後には自然に収まる一過性の事象)。
        # 2026-07-07: リトライは実効性が確認できなかったため廃止(タイムアウトのみ残す)。
        print(f"[ERROR] {job} 失敗 ret={ret_code[0]}", flush=True)
        return None, "error"

    # 2026-07-10: Sniper本体がbarrier_sync_server.ccで内部デッドロックを検出した場合、
    # ret=0のまま(正常終了扱いで)早期終了することが判明(bodytrackで6並列中5/6再現、
    # project_sniper_futex_deadlockメモリ参照)。ret_codeだけでは検知できないため、
    # ログの当該エラー文字列を明示的にチェックする。
    with open(log_path) as f:
        if "Application has deadlocked" in f.read():
            print(f"[DEADLOCK] {job} Sniperが内部デッドロックを検出(ret=0だが実質失敗)", flush=True)
            return None, "deadlock"

    power = estimate_power(out_dir, cpu_map, job.num_threads)
    update_from_run(job.workload, job.bench_class, job.num_threads, out_dir, elapsed, job.backend)
    export_csv(out_dir, job.workload, job.strategy, job.bench_class, job.num_threads, cpu_map, power)
    return out_dir, None


def _run_and_release(job: Job, pool: _CapacityPool, run_id: str, no_timeout: bool,
                     counters: dict, lock: threading.Lock,
                     timeout_failures: list[Job] | None = None,
                     timeout_multiplier: float = 2.0):
    try:
        out_dir, fail_reason = run_job(job, run_id, no_timeout=no_timeout,
                                       timeout_multiplier=timeout_multiplier)
        with lock:
            counters["done"] += 1
            status = "OK" if out_dir else "FAILED"
            print(f"[{counters['done']}/{counters['total']}] {job} -> {status}", flush=True)
            if fail_reason == "timeout" and timeout_failures is not None:
                timeout_failures.append(job)
    finally:
        pool.release(job.width)


def _order_jobs(jobs: list[Job], capacity: float, use_exact: bool, exact_threshold: int) -> list[Job]:
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
    return ordered


def _run_pool(jobs: list[Job], capacity: float, run_id: str, use_exact: bool,
             no_timeout: bool, exact_threshold: int, counters: dict, lock: threading.Lock,
             hard_limit: float | None = None, live_load_fn=None,
             loadavg_fn=None, loadavg_hard_limit: float | None = None,
             timeout_failures: list[Job] | None = None,
             timeout_multiplier: float = 2.0) -> None:
    if not jobs:
        return
    ordered = _order_jobs(jobs, capacity, use_exact, exact_threshold)

    # 2026-07-11: スケジューリング(順序付け)が終わった時点で、実行前に完了までの
    # 推定時間をログへ出す(ユーザー要望)。_CapacityPool.acquire()と同じ貪欲規則の
    # シミュレーション(utility.scheduling.estimate_makespan)なので、実測ゲート
    # (live_load_fn/loadavg_fn)による遅延は含まない静的な見積もり。
    makespan_sec = estimate_makespan(ordered, capacity)
    print(f"[SCHED] {jobs[0].backend}: 見積完了時間 = {makespan_sec:.0f}s "
          f"({makespan_sec/3600:.1f}時間、{len(ordered)}件、実測ゲートの遅延は含まず)")

    pool = _CapacityPool(capacity, hard_limit=hard_limit, live_load_fn=live_load_fn,
                         loadavg_fn=loadavg_fn, loadavg_hard_limit=loadavg_hard_limit)
    threads = []
    for job in ordered:
        pool.acquire(job.width)
        t = threading.Thread(target=_run_and_release,
                             args=(job, pool, run_id, no_timeout, counters, lock,
                                   timeout_failures, timeout_multiplier))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()


def schedule_and_run(jobs: list[Job], capacity: float, run_id: str, use_exact: bool,
                     no_timeout: bool = False, exact_threshold: int = 80,
                     purple_capacity: float = PURPLE_CAPACITY_DEFAULT,
                     timeout_multiplier: float = 2.0) -> list[Job]:
    """
    backend("sid"/"purple")ごとに独立した資源プールでスケジューリングする
    (マシン切替機構)。hiragahama(sid)とPurpleは別々の物理資源なので、互いを
    待たせず完全並行に実行する(2マシン並列スケジューリングは、backendごとに
    独立した_CapacityPoolを別スレッドで走らせるだけで実現できる)。

    戻り値: タイムアウトで失敗したJobのリスト(crash/デッドロック失敗は含まない、
    2026-07-11のリトライ機構向け)。呼び出し元(main)がこれを使って本バッチ完走後に
    1回だけ自動リトライするかどうかを判断する。

    timeout_multiplier: 既定2.0(初回実行はこのまま変更しない、ユーザー指示)。
    main()の自動リトライパスだけ4.0を渡す。
    """
    sid_jobs    = [j for j in jobs if j.backend == "sid"]
    purple_jobs = [j for j in jobs if j.backend == "purple"]

    counters = {"done": 0, "total": len(jobs)}
    lock = threading.Lock()
    timeout_failures: list[Job] = []

    print(f"\n{'='*64}")
    print(f"  ULTRA ORCHESTRATOR — 資源制約付きスケジューリング実行")
    print(f"  ジョブ数={len(jobs)}  (sid={len(sid_jobs)}, purple={len(purple_jobs)})")
    print(f"  実効容量: sid={capacity}コア  purple={purple_capacity}スレッド")
    print(f"{'='*64}\n")

    group_threads = [
        threading.Thread(target=_run_pool, args=(
            sid_jobs, capacity, run_id, use_exact, no_timeout, exact_threshold, counters, lock,
            SID_HARD_LIMIT_CORES, live_sid_load_cores,
            live_sid_loadavg, SID_LOADAVG_HARD_LIMIT, timeout_failures, timeout_multiplier)),
        threading.Thread(target=_run_pool, args=(
            purple_jobs, purple_capacity, run_id, use_exact, no_timeout, exact_threshold, counters, lock,
            PURPLE_HARD_LIMIT_CORES, live_purple_load_cores,
            live_purple_loadavg, PURPLE_LOADAVG_HARD_LIMIT, timeout_failures, timeout_multiplier)),
    ]
    for t in group_threads:
        t.start()
    for t in group_threads:
        t.join()

    print(f"\n[完了] {counters['done']}/{counters['total']} 件処理")
    return timeout_failures


def resolve_workload_machine_pairs(workload_tokens: list[str],
                                    machine_tokens: list[str] | None) -> list[tuple[str, str]]:
    """
    ワークロードトークンとマシントークンを位置対応でペアにする。実際に重い/軽いは
    戦略ではなくワークロードで決まる(utility.capacity_model.HEAVY_WORKLOADS参照)
    ため、2026-07-09に戦略軸からワークロード軸に変更した。

    machine_tokens が None(--machine省略)の場合はHEAVY_WORKLOADSに基づき自動振り分け
    する(重量級→sid、それ以外→purple)。
      例: resolve_workload_machine_pairs(["BT","FT","IS","MG"], None)
          → BT(HEAVY_WORKLOADS所属)はsid、FT/IS/MGはpurple
      machine_tokens が1個だけなら全workloadトークンに一律適用。
      複数指定時はworkloadトークン数と個数が一致している必要がある(位置対応)。

    注意: 「AKARIN候補生成(akarin)だけ別マシンに逃がす」のような戦略軸の
    振り分けが必要な場合は、この関数の対象外(1回のCLI呼び出しでは表現できない)。
    そのワークロード群に対してだけ--strategiesを絞った上で、シェルスクリプトで
    複数回`ultra_orchestrator.py`を呼び分けること。
    """
    if machine_tokens is None:
        return [(wl, "sid" if wl in HEAVY_WORKLOADS else "purple") for wl in workload_tokens]
    if len(machine_tokens) == 1:
        machine_tokens = machine_tokens * len(workload_tokens)
    if len(machine_tokens) != len(workload_tokens):
        raise ValueError(
            f"machine の個数({len(machine_tokens)})が workloads の個数"
            f"({len(workload_tokens)})と一致しません(1個 or 同数を指定してください)")
    return list(zip(workload_tokens, machine_tokens))


def group_pairs_by_machine(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    by_machine: dict[str, list[str]] = {}
    for token, machine in pairs:
        by_machine.setdefault(machine, []).append(token)
    return by_machine


# ============================================================
# CLI (orchestrator.py と同じ引数名・流儀)
# ============================================================

def _parse_args():
    p = argparse.ArgumentParser(description="ClaudeXSniper ultra orchestrator (厳密/近似スケジューリング版)")
    p.add_argument("--threads",     type=int, nargs="+", default=None,
                   help="スレッド数 (複数指定可: --threads 2 8 12 16)")
    p.add_argument("--bench-class", nargs="+", default=None,
                   choices=sorted(VALID_BENCH_CLASSES),
                   help="ベンチクラス (複数指定可: --bench-class S W。省略時: BENCH_CLASSES)")
    p.add_argument("--strategies",  nargs="+", default=None,
                   help="戦略名(Packed/Scatter/HPO/EPO/MPO)、またはAKARIN候補生成トークンakarin"
                        "(全ワークロード対象、alpha廃止によりakarin_h/lの区別は2026-07-11に統合)。"
                        "複数指定可: --strategies Packed MPO akarin")
    p.add_argument("--workloads",   nargs="+", default=None)
    p.add_argument("--machine",     nargs="+", default=None, choices=sorted(VALID_MACHINES),
                   help="実行先マシン(マシン切替機構)。1個なら全--workloadsに一律適用、"
                        "複数なら--workloadsと同数で位置対応 (例: --workloads BT FT "
                        "--machine sid purple)。省略時: HEAVY_WORKLOADSに基づき自動振り分け"
                        "(重量級→sid、それ以外→purple)")
    p.add_argument("--capacity",    type=float, default=None,
                   help=f"sid(hiragahama)実効コア数上限 (省略時: {SID_CAPACITY_DEFAULT})")
    p.add_argument("--purple-capacity", type=float, default=None,
                   help=f"Purple実効スレッド数上限 (省略時: {PURPLE_CAPACITY_DEFAULT})")
    p.add_argument("--exact",       action="store_true",
                   help="ジョブ数が少なければCP-SAT厳密解を試す (既定はLPT)")
    p.add_argument("--no-timeout",  action="store_true",
                   help="タイムアウトを無効化 (sizeA など長時間実験用)")
    return p.parse_args()


def main():
    args = _parse_args()

    thread_list = args.threads     if args.threads     is not None else THREAD_COUNTS
    class_list  = args.bench_class if args.bench_class is not None else BENCH_CLASSES
    workloads   = args.workloads   or WORKLOADS
    strategy_tokens = args.strategies or STRATEGIES_TO_RUN
    capacity    = args.capacity    if args.capacity    is not None else SID_CAPACITY_DEFAULT
    purple_capacity = args.purple_capacity if args.purple_capacity is not None else PURPLE_CAPACITY_DEFAULT

    _validate(thread_list, class_list)

    try:
        pairs = resolve_workload_machine_pairs(workloads, args.machine)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    by_machine = group_pairs_by_machine(pairs)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    jobs: list[Job] = []
    for machine, wl_list in by_machine.items():
        machine_jobs = build_jobs(wl_list, strategy_tokens, class_list, thread_list, backend=machine)
        jobs.extend(machine_jobs)
        print(f"[JOBS] {len(machine_jobs)}件 (machine={machine}, workloads={wl_list}, "
              f"strategies={strategy_tokens}, bench_class={class_list}, threads={thread_list})")

    notify(
        f"[ultra_orchestrator] 実行開始  class={class_list}  threads={thread_list}  "
        f"workloads/machine={pairs}  strategies={strategy_tokens}  capacity={capacity}  "
        f"purple_capacity={purple_capacity}  no_timeout={args.no_timeout}"
    )

    timeout_failures = schedule_and_run(jobs, capacity, run_id, use_exact=args.exact,
                                        no_timeout=args.no_timeout, purple_capacity=purple_capacity)

    if timeout_failures:
        # 2026-07-11: タイムアウト由来の失敗だけを、本バッチ完走後に1回だけ自動
        # リトライする。crash/デッドロック失敗(run_job()のfailure_reason参照)は
        # 対象外(同じ理由で再送しても再現するだけ)。マシン振り分けは元のpairs
        # (workload->machine)をそのまま再利用する(resolve_workload_machine_pairsを
        # 再度呼ぶと、リトライ対象がworkloadsの部分集合になった場合に
        # --machineの位置対応個数チェックで誤ってValueErrorになりうるため)。
        # タイムアウト閾値は本バッチ完走でrun_profile.jsonが更新された後の
        # 「更新後の見積もり×2」をrun_job()が自動で再計算する(ユーザー判断:
        # 2026-07-06の事故以来、余裕を持たせるより実測に追従する方針で一貫)。
        backend_by_workload = dict(pairs)
        retry_jobs = [
            Job(j.workload, j.strategy, j.bench_class, j.num_threads,
                backend=backend_by_workload[j.workload], cpu_map=j.cpu_map, provenance=j.provenance)
            for j in timeout_failures
        ]
        print(f"\n[RETRY] タイムアウト由来の失敗{len(retry_jobs)}件を1回だけ再送します"
              f"(更新後の見積もり×4でタイムアウトを再計算、初回の×2より余裕を広げる)")
        notify(f"[ultra_orchestrator] タイムアウト失敗{len(retry_jobs)}件を自動リトライ中")
        retry_run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        schedule_and_run(retry_jobs, capacity, retry_run_id, use_exact=args.exact,
                         no_timeout=args.no_timeout, purple_capacity=purple_capacity,
                         timeout_multiplier=4.0)

    notify(f"[ultra_orchestrator] 完了  class={class_list}  threads={thread_list}")


if __name__ == "__main__":
    main()
