"""
orchestrator.py
複数のワークロード × アフィニティ戦略 × スレッド数を統括して Sniper で実行する。

使い方:
  python3 orchestrator.py --threads 4 --bench-class S
  python3 orchestrator.py --threads 8 --bench-class A --strategies Packed HPO
"""

import argparse
import os
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from utility.cpu_affinity   import (get_cpu_map, binary_path, get_binary_args,
                                     save_affinity_config, resolve_mpo_equivalent)
from utility.run_profile    import get_reference, update_from_run, estimate_walltime
from utility.stats_reader   import parse_node_stats
from utility.csv_exporter   import export_csv
from utility.power_model    import estimate as estimate_power
from utility.notify         import notify
from config.generate_config import generate_config, get_config_path

# ============================================================
# 実験設定（CLI 引数で上書き可能）
# ============================================================
WORKLOADS = ["BT", "CG", "FT", "IS", "MG", "SP","lavaMD", "BFS", "PR", "BC", "CC", "SSSP", "TC","BC"]
#     
# ]#BT,SPが最重量、MGは中間くらい　新しくベンチ入れたらMPOAffintyに注意
STRATEGIES_TO_RUN  = ["Packed","HPO","Scatter","EPO"]#
THREAD_COUNTS      = [2]#,6,8,12
BENCH_CLASSES      = ["A"]



_HOST_CORES      = 32
_SNIPER_OVERHEAD = 0


def _calc_concurrent(num_threads: int, user_override: int | None = None) -> int:
    """Sniperはnum_threads本のホストスレッドを使うため、同時実験数を自動算出する。"""
    if user_override:
        return user_override
    return max(1, _HOST_CORES // (num_threads + _SNIPER_OVERHEAD))


CLAUDEXSNIPER_DIR = "/home/hiragahama/ClaudeXSniper"
OUTPUT_BASE_TMPL  = "/home/hiragahama/ClaudeXSniper/Outputs/size{cls}"

BINARY_BASE  = "/home/hiragahama/ClaudeXSniper/binary"
NPB_BIN_DIR  = f"{BINARY_BASE}/NPB3.3-OMP/bin"
GAPBS_DIR    = f"{BINARY_BASE}/GAPBS"
LAVAMD_DIR   = f"{BINARY_BASE}/Rodinia/openmp/lavaMD"
VALID_THREAD_COUNTS = {2, 4, 6, 8, 12, 16, 32}
VALID_BENCH_CLASSES = {"S", "W", "A", "B", "C", "D"}

TIMEOUT_LOG = os.path.join(CLAUDEXSNIPER_DIR, "logs", "timeoutwl.log")

def _log_timeout(workload: str, strategy: str, bench_class: str, num_threads: int,
                 elapsed: float, timeout_sec: float, attempt: int, max_retries: int) -> None:
    outcome = f"retry({attempt + 2}/{max_retries + 1})" if attempt < max_retries else "FAILED"
    line = (
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  "
        f"{workload:<8} {strategy:<8} Class={bench_class}  {num_threads}TH  "
        f"elapsed={elapsed:.0f}s  timeout={timeout_sec:.0f}s  {outcome}\n"
    )
    os.makedirs(os.path.dirname(TIMEOUT_LOG), exist_ok=True)
    with open(TIMEOUT_LOG, "a") as f:
        f.write(line)
    print(f"[timeout log] {TIMEOUT_LOG} に追記しました", flush=True)


# ============================================================
# 進捗表示（TTY でのみ ANSI 上書き）
# ============================================================

def _fmt(s: float) -> str:
    m, sec = divmod(int(max(s, 0)), 60)
    return f"{m:02d}:{sec:02d}"


def _bar(pct: float, w: int = 22) -> str:
    f = int(w * pct / 100)
    return "█" * f + "░" * (w - f)


class ProgressDisplay:
    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._bars: dict[str, dict] = {}
        self._order: list[str] = []
        self._drawn = 0
        self._tty   = sys.stdout.isatty()

    def add(self, key: str) -> None:
        with self._lock:
            self._bars[key] = {"pct": 0.0, "elapsed": 0.0, "remain": None, "known": False}
            self._order.append(key)
            if self._tty:
                self._redraw()
            else:
                print(f"  [{key}] 開始", flush=True)

    def update(self, key: str, pct: float, elapsed: float,
               remain: float | None, known: bool) -> None:
        with self._lock:
            if key not in self._bars:
                return
            self._bars[key] = {"pct": pct, "elapsed": elapsed, "remain": remain, "known": known}
            if self._tty:
                self._redraw()

    def complete(self, key: str, success: bool, done: int, total: int) -> None:
        with self._lock:
            info    = self._bars.pop(key, {})
            elapsed = info.get("elapsed", 0.0)
            status  = "完了" if success else "失敗"
            if self._tty:
                self._clear()
                print(f"  [{key}] {status}  経過 {_fmt(elapsed)}  [{done}/{total}]")
                self._order = [k for k in self._order if k != key]
                self._redraw()
            else:
                self._order = [k for k in self._order if k != key]
                print(f"  [{key}] {status}  経過 {_fmt(elapsed)}  [{done}/{total}]", flush=True)

    def _clear(self) -> None:
        for _ in range(self._drawn):
            sys.stdout.write("\033[A\033[2K")
        self._drawn = 0
        sys.stdout.flush()

    def _redraw(self) -> None:
        self._clear()
        for key in self._order:
            info = self._bars[key]
            pct, known, elapsed, remain = (
                info["pct"], info["known"], info["elapsed"], info["remain"]
            )
            pct_str    = f"{pct:5.1f}%" if known else "  --.-% "
            remain_str = _fmt(remain) if (known and remain is not None) else "--:--"
            print(f"  [{key:<18}]  {pct_str}  |{_bar(pct)}|"
                  f"  経過 {_fmt(elapsed)}  残り {remain_str}")
            self._drawn += 1
        sys.stdout.flush()


# ============================================================
# 結果サマリー表示
# ============================================================

NODE0_CPUS = set(range(0, 8))

def _print_summary(results: dict, workloads: list,
                   bench_class: str, num_threads: int,
                   output_base: str, run_id: str) -> None:
    print(f"\n{'='*76}")
    print(f"  全実験結果サマリー  (Class {bench_class}, {num_threads} threads)")
    print(f"{'='*76}")
    print(f"  {'WL':<4} {'Strategy':<10}  {'Node0':>10}  {'Node1':>10}  {'Node0%':>7}  使用コア")
    print(f"  {'-'*72}")
    for wl in workloads:
        for r in results[wl]:
            ns    = r["node_stats"]
            t0    = ns.get(0, {}).get("reads", 0) + ns.get(0, {}).get("writes", 0)
            t1    = ns.get(1, {}).get("reads", 0) + ns.get(1, {}).get("writes", 0)
            grand = t0 + t1
            ratio = t0 / grand * 100 if grand > 0 else 0
            cmap  = r["cpu_map"]
            cores = " ".join(
                f"CPU{cmap[t]}({'N0' if cmap[t] in NODE0_CPUS else 'N1'}"
                f"{'P' if (cmap[t] % 8) < 4 else 'E'})"
                for t in range(min(num_threads, 4))
            )
            if num_threads > 4:
                cores += " ..."
            print(f"  {wl:<4} {r['strategy']:<10}  {t0:>10,}  {t1:>10,}  {ratio:>6.1f}%  {cores}")
        if wl != workloads[-1]:
            print(f"  {'-'*72}")
    print(f"{'='*76}")
    print(f"\n  各実験出力: {output_base}/{{WL}}_{bench_class}_*_{num_threads}TH_{run_id}/")


# ============================================================

def _parse_args():
    p = argparse.ArgumentParser(description="ClaudeXSniper orchestrator")
    p.add_argument("--threads",     type=int, nargs="+", default=None,
                   help="スレッド数 (複数指定可: --threads 2 6 8 12)")
    p.add_argument("--bench-class", default=None,
                   choices=sorted(VALID_BENCH_CLASSES),
                   help="ベンチクラス (省略時: BENCH_CLASSES リストを逐次実行)")
    p.add_argument("--strategies",  nargs="+", default=None)
    p.add_argument("--concurrent",  type=int,  default=None,
                   help="同時実験数 (省略時: host_cores // (threads + 2))")
    p.add_argument("--workloads",   nargs="+", default=None)
    p.add_argument("--no-timeout",  action="store_true",
                   help="タイムアウトを無効化 (sizeA など長時間実験用)")
    return p.parse_args()


def _find_existing_output(output_base: str, bench_class: str, strategy: str,
                          workload: str, num_threads: int) -> str | None:
    """既存の実行結果ディレクトリを検索する(直近のタイムスタンプを採用)。"""
    import glob
    pattern = os.path.join(
        output_base, f"{num_threads}TH",
        f"{workload}_{bench_class}_{strategy}_{num_threads}TH_*",
    )
    matches = sorted(glob.glob(pattern))
    return matches[-1] if matches else None


def _materialize_mpo_copy(src_dir: str, workload: str, bench_class: str,
                          num_threads: int, source_strategy: str,
                          output_base: str, run_id: str) -> str:
    """
    MPO の cpu_map が source_strategy と完全一致する場合に、実シミュレーションを
    省略して source_strategy の結果を複製・リネームする(Sniper は決定論的な
    シミュレータなので、同一 cpu_map なら結果も同一になる)。
    """
    new_dir = os.path.join(
        output_base, f"{num_threads}TH",
        f"{workload}_{bench_class}_MPO_{num_threads}TH_{run_id}",
    )
    if os.path.exists(new_dir):
        shutil.rmtree(new_dir)
    shutil.copytree(src_dir, new_dir)

    threads_label = f"{num_threads}TH"

    old_cfg = os.path.join(new_dir, f"arrow_lake_{source_strategy}_{threads_label}.cfg")
    new_cfg = os.path.join(new_dir, f"arrow_lake_MPO_{threads_label}.cfg")
    if os.path.exists(old_cfg):
        os.rename(old_cfg, new_cfg)

    ac_path = os.path.join(new_dir, "affinity_config.txt")
    if os.path.exists(ac_path):
        with open(ac_path) as f:
            content = f.read()
        content = content.replace(f"PRESET={source_strategy}", "PRESET=MPO")
        content += (f"\n# NOTE: {source_strategy}とcpu_mapが完全一致するため複製生成"
                     f"（実機でのMPO専用実験は未実施）\n")
        with open(ac_path, "w") as f:
            f.write(content)

    mc_path = os.path.join(new_dir, "metrics.csv")
    if os.path.exists(mc_path):
        with open(mc_path) as f:
            lines = f.readlines()
        new_lines = []
        for line in lines:
            if line.startswith("strategy,"):
                line = "strategy,MPO\n"
            elif line.startswith("output_dir,"):
                line = line.replace(f"_{source_strategy}_", "_MPO_")
            new_lines.append(line)
        with open(mc_path, "w") as f:
            f.writelines(new_lines)

    si_path = os.path.join(new_dir, "sim.info")
    if os.path.exists(si_path):
        with open(si_path) as f:
            content = f.read()
        content = content.replace(f"arrow_lake_{source_strategy}_{threads_label}.cfg",
                                   f"arrow_lake_MPO_{threads_label}.cfg")
        with open(si_path, "w") as f:
            f.write(content)

    print(f"  [MPO] {workload}: {source_strategy}と完全一致のため複製 (実シミュレーションはスキップ)",
          flush=True)
    return new_dir


def _run_one_thread_count(
    num_threads: int,
    bench_class: str,
    strategies: list,
    workers: int,
    workloads: list,
    run_id: str,
    no_timeout: bool = False,
) -> None:
    output_base = OUTPUT_BASE_TMPL.format(cls=bench_class)

    # MPO は cpu_map が他戦略と完全一致するワークロード/スレッド数では
    # 実シミュレーションを省略し、一致先の結果を複製して使う。
    real_jobs: list[tuple[str, str]] = []
    mpo_copy_jobs: list[tuple[str, str]] = []
    for st in strategies:
        for wl in workloads:
            if st == "MPO":
                equiv = resolve_mpo_equivalent(wl, num_threads)
                if equiv is not None:
                    mpo_copy_jobs.append((wl, equiv))
                    continue
            real_jobs.append((wl, st))

    total = len(real_jobs)

    print(f"\n{'='*64}")
    print(f"  ClaudeXSniper — NUMA アフィニティ実験")
    print(f"  ワークロード: {workloads}")
    print(f"  クラス={bench_class}  スレッド={num_threads}  戦略={strategies}")
    print(f"  実験数={total} (実実行)  +{len(mpo_copy_jobs)} (MPO複製)  同時実験数={workers}  (ホスト{_HOST_CORES}コア)")
    print(f"{'='*64}\n")

    display      = ProgressDisplay()
    done_counter = [0]
    done_lock    = threading.Lock()
    output_dirs: dict[tuple[str, str], str] = {}

    def run_one(workload: str, strategy: str) -> str:
        key      = f"{workload}/{strategy}"
        cpu_map  = get_cpu_map(strategy, workload)
        bin_path = binary_path(workload, bench_class)
        bin_args = get_binary_args(workload, bench_class, num_threads)

        out_dir = os.path.join(
            output_base, f"{num_threads}TH",
            f"{workload}_{bench_class}_{strategy}_{num_threads}TH_{run_id}",
        )

        ref          = get_reference(workload, bench_class, num_threads)
        expected_sec = ref["wallTime"] if ref else estimate_walltime(workload, bench_class, num_threads)
        timeout_sec  = float("inf") if no_timeout else (max(expected_sec * 3, 600) if expected_sec else 7200)

        from sniper_sim import run_sniper

        MAX_RETRIES = 1
        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                shutil.rmtree(out_dir, ignore_errors=True)
            os.makedirs(out_dir, exist_ok=True)

            cfg_path = get_config_path(out_dir, strategy, num_threads)
            generate_config(strategy, num_threads, cpu_map, cfg_path)
            save_affinity_config(out_dir, strategy, workload, bench_class,
                                 cpu_map, num_threads)

            display.add(key)
            start    = time.time()
            log_file = open(os.path.join(out_dir, "sniper.log"), "w")

            done_flag   = [False]
            ret_code    = [None]
            proc_holder = []
            timed_out   = [False]

            def _do_run():
                ret_code[0] = run_sniper(
                    binary_path=bin_path, binary_args=bin_args,
                    num_threads=num_threads, cpu_map=cpu_map,
                    strategy=strategy, output_dir=out_dir,
                    config_path=cfg_path, log_file=log_file,
                    workload=workload, proc_holder=proc_holder,
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
                if expected_sec:
                    display.update(key, min(elapsed / expected_sec * 100, 99.0),
                                   elapsed, max(expected_sec - elapsed, 0), known=True)
                else:
                    display.update(key, 0.0, elapsed, None, known=False)
                time.sleep(1)
            t.join()
            log_file.close()

            elapsed = time.time() - start

            if timed_out[0]:
                _log_timeout(workload, strategy, bench_class, num_threads,
                             elapsed, timeout_sec, attempt, MAX_RETRIES)
                print(f"\n[TIMEOUT] {key} ({elapsed:.0f}s > {timeout_sec:.0f}s)", flush=True)
                if attempt < MAX_RETRIES:
                    print(f"  → 再実行 (attempt {attempt + 2}/{MAX_RETRIES + 1})", flush=True)
                    display.complete(key, False, done_counter[0], total)
                    continue
                with done_lock:
                    done_counter[0] += 1
                display.complete(key, False, done_counter[0], total)
                raise RuntimeError(f"タイムアウト ({workload}/{strategy})")

            success = (ret_code[0] == 0)
            with done_lock:
                done_counter[0] += 1
            display.complete(key, success, done_counter[0], total)

            if not success:
                raise RuntimeError(f"Sniper 失敗 ({workload}/{strategy}) ret={ret_code[0]}")
            break

        power = estimate_power(out_dir, cpu_map, num_threads)
        update_from_run(workload, bench_class, num_threads, out_dir, elapsed)
        export_csv(out_dir, workload, strategy, bench_class, num_threads, cpu_map, power)
        return out_dir

    print(f"[STEP] Sniper 並列実行 ({total} 実験, 同時実験数={workers})\n")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(run_one, wl, st): (wl, st)
            for wl, st in real_jobs
        }
        for future in as_completed(futures):
            wl, st = futures[future]
            try:
                output_dirs[(wl, st)] = future.result()
            except Exception as e:
                print(f"[ERROR:{wl}/{st}] {e}")

    if mpo_copy_jobs:
        print(f"\n[STEP] MPO複製 ({len(mpo_copy_jobs)}件)")
        for wl, equiv in mpo_copy_jobs:
            src_dir = output_dirs.get((wl, equiv))
            if not src_dir:
                src_dir = _find_existing_output(output_base, bench_class, equiv, wl, num_threads)
            if not src_dir:
                print(f"  [MPO] {wl}: {equiv}相当の既存結果が見つからないため実シミュレーションを実行します",
                      flush=True)
                try:
                    output_dirs[(wl, "MPO")] = run_one(wl, "MPO")
                except Exception as e:
                    print(f"[ERROR:{wl}/MPO] {e}")
                continue
            output_dirs[(wl, "MPO")] = _materialize_mpo_copy(
                src_dir, wl, bench_class, num_threads, equiv, output_base, run_id
            )

    print(f"\n[STEP] 結果収集")
    results: dict[str, list] = {wl: [] for wl in workloads}
    for wl in workloads:
        for st in strategies:
            out_dir = output_dirs.get((wl, st))
            if not out_dir:
                continue
            results[wl].append({
                "strategy":   st,
                "output_dir": out_dir,
                "node_stats": parse_node_stats(out_dir),
                "cpu_map":    get_cpu_map(st, wl),
            })

    _print_summary(results, workloads, bench_class, num_threads, output_base, run_id)


def _validate(thread_list: list[int], class_list: list[str]) -> None:
    bad_threads = [t for t in thread_list if t not in VALID_THREAD_COUNTS]
    bad_classes = [c for c in class_list  if c not in VALID_BENCH_CLASSES]
    errors = []
    if bad_threads:
        errors.append(
            f"不正なスレッド数: {bad_threads}  有効値: {sorted(VALID_THREAD_COUNTS)}"
        )
    if bad_classes:
        errors.append(
            f"不正なベンチクラス: {bad_classes}  有効値: {sorted(VALID_BENCH_CLASSES)}"
        )
    if errors:
        for msg in errors:
            print(f"[ERROR] {msg}", file=sys.stderr)
        sys.exit(1)


def main():
    args         = _parse_args()
    thread_list  = args.threads        if args.threads     is not None else THREAD_COUNTS
    class_list   = [args.bench_class] if args.bench_class is not None else BENCH_CLASSES
    strategies   = args.strategies or STRATEGIES_TO_RUN
    workloads    = args.workloads  or WORKLOADS

    _validate(thread_list, class_list)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    if len(class_list) > 1 or len(thread_list) > 1:
        print(f"\n  クラスシーケンス : {class_list}")
        print(f"  スレッドシーケンス: {thread_list}  (逐次実行)")

    notify(
        f"[ClaudeXSniper] 実行開始  class={class_list}  threads={thread_list}  "
        f"workloads={workloads}  strategies={strategies}  no_timeout={args.no_timeout}"
    )

    for bench_class in class_list:
        for num_threads in thread_list:
            workers = _calc_concurrent(num_threads, args.concurrent)
            _run_one_thread_count(
                num_threads, bench_class, strategies, workers, workloads, run_id,
                no_timeout=args.no_timeout,
            )
            notify(f"[ClaudeXSniper] {num_threads}TH (class={bench_class}) 完了")

    notify(f"[ClaudeXSniper] 全実行完了  class={class_list}  threads={thread_list}")


if __name__ == "__main__":
    main()
