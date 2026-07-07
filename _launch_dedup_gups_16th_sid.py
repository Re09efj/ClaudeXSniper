"""
_launch_dedup_gups_16th_sid.py
dedup(16TH)・GUPS(12TH/16TH) を、既存5戦略+AKARIN候補(akarin_l)込みでSID上で実行する。
Purpleでの16TH実行(dedup/HPO以降・GUPS全体が未着手)を待たず、SIDの実測を先に取る。
出力先はOutputs/sizeWではなくOutputs/SID/size{cls}に分離する(本バッチの出力と混在させない)。
2026-07-07: GUPSはメモリ帯域律速でwidthモデルが同時実行数を絞りきれず、9並列で
自己渋滞したため、host_width_pct側にMEMORY_BOUND_WORKLOADS補正(2倍)を追加済み。
"""
import sys
from datetime import datetime

sys.path.insert(0, "/home/hiragahama/ClaudeXSniper")
import ultra_orchestrator as uo

# 出力先を分離 (run_job内のOUTPUT_BASE_TMPL参照を差し替え)
uo.OUTPUT_BASE_TMPL = "/home/hiragahama/ClaudeXSniper/Outputs/SID/size{cls}"

run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

STRATEGIES = ["Packed", "Scatter", "HPO", "EPO", "MPO", "akarin_l"]

jobs  = uo.build_jobs(["dedup"], STRATEGIES, ["W"], [16], backend="sid")
jobs += uo.build_jobs(["GUPS"],  STRATEGIES, ["W"], [12, 16], backend="sid")

print(f"[LAUNCH] dedup(16TH)/GUPS(12TH,16TH) SID ジョブ数: {len(jobs)}")
for j in jobs:
    print(" ", j)

uo.schedule_and_run(jobs, capacity=uo.SID_CAPACITY_DEFAULT, run_id=run_id, use_exact=False)
