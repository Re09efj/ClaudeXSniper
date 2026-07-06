"""
_launch_akarin_purple.py
AKARIN候補(akarin_h/akarin_l)を全ワークロード×全スレッド数(2,8,12,16)、
Purpleバックエンドで実行するランチャー。
"""
import sys
from datetime import datetime

sys.path.insert(0, "/home/hiragahama/ClaudeXSniper")
from ultra_orchestrator import build_jobs, schedule_and_run, WORKLOADS, BENCH_CLASSES

run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

jobs = build_jobs(WORKLOADS, ["akarin_h", "akarin_l"], BENCH_CLASSES, [2, 8, 12, 16], backend="purple")

print(f"[LAUNCH] AKARIN候補ジョブ数: {len(jobs)}  (backend=purple, class={BENCH_CLASSES}, threads=[2,8,12,16])")

schedule_and_run(jobs, capacity=21.0, run_id=run_id, use_exact=False, purple_capacity=45.0)
