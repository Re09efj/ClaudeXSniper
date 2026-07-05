"""
_launch_full_sw.py
SizeS+SizeW 全ワークロード×全戦略の本実行ランチャー。

MPOの16THはPurpleでのcomm.csv収集完了待ちのため、暫定的に2,8,12THのみで実行する
(Packed/Scatter/HPO/EPOは2,8,12,16THの全部)。16TH分のcomm.csvが揃い次第、
別途 _launch_mpo_16th_followup.py 相当の追加ジョブを投入する。
"""
import sys
from datetime import datetime

sys.path.insert(0, "/home/hiragahama/ClaudeXSniper")
from ultra_orchestrator import build_jobs, schedule_and_run, WORKLOADS

run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

jobs_main = build_jobs(WORKLOADS, ["Packed", "Scatter", "HPO", "EPO", "MPO"], ["S", "W"], [2, 8, 12])
jobs_16th_no_mpo = build_jobs(WORKLOADS, ["Packed", "Scatter", "HPO", "EPO"], ["S", "W"], [16])
jobs = jobs_main + jobs_16th_no_mpo

print(f"[LAUNCH] 合計ジョブ数: {len(jobs)} "
      f"(2,8,12TH全戦略={len(jobs_main)} + 16TH非MPO={len(jobs_16th_no_mpo)})")

schedule_and_run(jobs, capacity=21.0, run_id=run_id, use_exact=False)
