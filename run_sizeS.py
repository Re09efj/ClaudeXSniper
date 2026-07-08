"""
run_sizeS.py
SizeS(Sクラス)を7ワークロードで仕上げるための実行ドライバ。

背景: BT/FT/IS/MGは既に(旧orchestrator.py経由の)Sクラス実測がほぼ揃っているが、
BT/MGの2THだけEPOが1件ずつ欠けている(roofline_dataset.csvで確認、2026-07-08)。
GUPS/canneal/dedupはSクラスのSniper実測が1件も無い(通信行列は2026-07-08に
1〜16刻みで取得済み、Data/comm_matrices/にも配置済み)。既存5戦略に加え、
AKARIN候補(akarin_h/akarin_l)も含める。SizeSはakarin/generate_candidates.py
のalpha_grid_for()により、重量級(canneal/BT/dedup)でも軽量級と同じ21点グリッド
を使う(2026-07-08、SizeWの3点粗グリッドとは異なる特別扱い)。

canneal/dedupは2026-07-08にJob.num_threadsの意味を「常に実スレッド数」に統一した
(utility/cpu_affinity.pyのarg_threads_for参照)。dedupは3N+3の制約上、標準の
THREAD_COUNTS=[2,8,12,16]を実現できない(実現可能なのは6,9,12,15)ため、
dedupだけ専用のスレッド数リストを使う。

SID/Purple振り分けはHEAVY_WORKLOADS({"canneal","BT","dedup"}、
akarin/generate_candidates.py参照)と同じ基準: 重量級→SID、軽量級→Purple。

このバッチはSクラスのみを対象とする(Wクラスは2026-07-06のrun_tonight.pyで
既に完了済みのため、重複実行を避ける)。
"""
from datetime import datetime

import ultra_orchestrator as uo

ALL_STRATEGIES = ["Packed", "Scatter", "HPO", "EPO", "MPO", "akarin_h", "akarin_l"]

# GUPS/canneal/dedupはSクラス実測が丸ごと無い。BT/MGは2THのEPOだけ欠けている。
FULL_S_WORKLOADS = ["GUPS", "canneal", "dedup"]
DEDUP_THREAD_COUNTS = [6, 9, 12, 15]  # dedupは3N+3の制約でuo.THREAD_COUNTSを実現できない

HEAVY_WORKLOADS = {"canneal", "BT", "dedup"}  # akarin/generate_candidates.HEAVY_WORKLOADSと同じ基準


def backend_for(workload: str) -> str:
    return "sid" if workload in HEAVY_WORKLOADS else "purple"


jobs: list[uo.Job] = []

for wl in FULL_S_WORKLOADS:
    thread_counts = DEDUP_THREAD_COUNTS if wl == "dedup" else uo.THREAD_COUNTS
    jobs += uo.build_jobs([wl], ALL_STRATEGIES, ["S"], thread_counts, backend=backend_for(wl))

# BT/MGの2THだけ欠けているEPOを個別に補完
for wl in ["BT", "MG"]:
    jobs.append(uo.Job(wl, "EPO", "S", 2, backend=backend_for(wl)))

print(f"[run_sizeS] 総ジョブ数={len(jobs)}")
for j in jobs:
    print(f"  {j}")

run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

uo.notify(f"[run_sizeS] 実行開始 jobs={len(jobs)}")

uo.schedule_and_run(
    jobs,
    capacity=uo.SID_CAPACITY_DEFAULT,
    run_id=run_id,
    use_exact=False,
    no_timeout=False,
    purple_capacity=uo.PURPLE_CAPACITY_DEFAULT,
)

uo.notify(f"[run_sizeS] 完了 jobs={len(jobs)}")
print("[run_sizeS] 完了")
