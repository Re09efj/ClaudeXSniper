"""
run_tonight.py
2026-07-06夜間の全ワークロード一括実行ドライバ。

ultra_orchestrator.py本体のmain()はSTRATEGIES_TO_RUNとMACHINEを位置対応させる
(戦略ごとにマシンを切り替える)設計だが、今回はワークロードの重さでマシンを
振り分けたい(重量級NPB系→SID、軽量級PARSEC/SPLASH2/GUPS系→Purple)。この軸は
既存main()のCLIでは表現できないため、build_jobs()を重量級/軽量級で2回呼び分けて
schedule_and_runに渡す専用ドライバとして用意した。

対象: BENCH_CLASSES(S,W) x THREAD_COUNTS(2,8,12,16) x
      戦略(Packed/Scatter/HPO/EPO/MPO/akarin_h/akarin_l)
"""

import sys
from datetime import datetime

import ultra_orchestrator as uo
from akarin.generate_candidates import HEAVY_WORKLOADS

ALL_STRATEGIES = ["Packed", "Scatter", "HPO", "EPO", "MPO", "akarin_h", "akarin_l"]

heavy_workloads = [w for w in uo.WORKLOADS if w in HEAVY_WORKLOADS]
light_workloads = [w for w in uo.WORKLOADS if w not in HEAVY_WORKLOADS]

print(f"[run_tonight] heavy(→sid)  = {heavy_workloads}")
print(f"[run_tonight] light(→purple) = {light_workloads}")
print(f"[run_tonight] bench_class={uo.BENCH_CLASSES} threads={uo.THREAD_COUNTS}")

jobs: list[uo.Job] = []
jobs += uo.build_jobs(heavy_workloads, ALL_STRATEGIES, uo.BENCH_CLASSES, uo.THREAD_COUNTS, backend="sid")
jobs += uo.build_jobs(light_workloads, ALL_STRATEGIES, uo.BENCH_CLASSES, uo.THREAD_COUNTS, backend="purple")

print(f"[run_tonight] 総ジョブ数={len(jobs)}")

run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

uo.notify(
    f"[run_tonight] 実行開始 heavy(sid)={heavy_workloads} light(purple)={light_workloads} "
    f"class={uo.BENCH_CLASSES} threads={uo.THREAD_COUNTS} jobs={len(jobs)}"
)

uo.schedule_and_run(
    jobs,
    capacity=uo.SID_CAPACITY_DEFAULT,
    run_id=run_id,
    use_exact=False,
    no_timeout=False,
    purple_capacity=uo.PURPLE_CAPACITY_DEFAULT,
)

uo.notify(f"[run_tonight] 完了 class={uo.BENCH_CLASSES} threads={uo.THREAD_COUNTS}")
print("[run_tonight] 完了")
