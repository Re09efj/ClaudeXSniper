"""
run_tonight.py
2026-07-06夜間の全ワークロード一括実行ドライバ。

ultra_orchestrator.py本体のmain()はSTRATEGIES_TO_RUNとMACHINEを位置対応させる
(戦略ごとにマシンを切り替える)設計だが、今回はワークロードの重さでマシンを
振り分けたい。当初「NPB系→SID、PARSEC/GUPS系→Purple」というスイート単位の
区分を検討したが、canneal(10184s)がBT(10036s)よりも壁時計時間で重く、逆に
MG/FT/ISはcanneal/x264より軽いことが実測(Data/run_profile.json)で判明し、
「スイートで重さを決めるのは実態と合わない」と2026-07-06に結論。
全8ワークロードをW級の実測壁時計時間(canneal/dedup/x264/GUPSはbench_classを
見ないため S=W。 NPBはSID実測、canneal/dedup/x264/GUPSはPurple実測で、
異なる実行環境の値を混在比較している点に注意)で降順に並べ、重い方から3つを
SID、残り5つをPurpleに機械的に割り当てる:

  canneal(10184s) > BT(10036s) > x264(7765s) > dedup(3506s) > MG(1715s)
  > GUPS(1534s) > FT(829s) > IS(401s)

  → SID:    canneal, BT, x264
  → Purple: dedup, MG, GUPS, FT, IS

注意: MG/FT/ISをPurpleで実行するのはこれが初めてで、sniper_sim_purple.py
経由の実行実績がない(参照壁時計時間もPurple版は存在しない)。canneal/dedup/
x264/GUPSはbench_classを見ないためW一本のみ実行(S/W重複回避)。NPB系
(BT/MG/FT/IS)は本物のS/Wクラスなので両方実行する。

対象: 全ワークロード×BENCH_CLASSES(NPBはS,W / PARSEC系はWのみ)×
      THREAD_COUNTS(2,8,12,16)×戦略(Packed/Scatter/HPO/EPO/MPO/akarin_h/akarin_l)
"""

import sys
from datetime import datetime

import ultra_orchestrator as uo

ALL_STRATEGIES = ["Packed", "Scatter", "HPO", "EPO", "MPO", "akarin_h", "akarin_l"]

# 注意: akarin.generate_candidates.HEAVY_WORKLOADS(AKARIN候補alpha点数の粗密判定、
# 実測壁時計時間ベース={"canneal","BT","x264"})とは別物。こちらは「本物のNPB
# クラス(S/W)を持つか」の判定で、常にNPBの4種({"BT","FT","IS","MG"})を指す。
# 同じ名前の定数を使い回すと、AKARIN側を実測重量ベースに変更した際にNPB判定まで
# 壊れる(canneal/x264がS+W重複実行、MG/FT/ISがW単一化)ため、2026-07-06に分離した。
_NPB_WORKLOADS = {"BT", "FT", "IS", "MG"}

# 全ワークロードをW級実測壁時計時間の重い順に並べる(2026-07-06実測、上のdocstring参照)
_WEIGHT_ORDER = ["canneal", "BT", "x264", "dedup", "MG", "GUPS", "FT", "IS"]
all_sorted = sorted(uo.WORKLOADS, key=lambda w: _WEIGHT_ORDER.index(w))
sid_workloads = all_sorted[:3]
purple_workloads = all_sorted[3:]

# bench_class: NPB(本物のS/Wクラス)は両方、それ以外(bench_classを見ない)はWのみ
def _classes_for(wl: str) -> list[str]:
    return uo.BENCH_CLASSES if wl in _NPB_WORKLOADS else ["W"]

print(f"[run_tonight] 重い順(W級実測): {all_sorted}")
print(f"[run_tonight] →SID    = {sid_workloads}")
print(f"[run_tonight] →Purple = {purple_workloads}")
print(f"[run_tonight] threads={uo.THREAD_COUNTS}")

jobs: list[uo.Job] = []
for wl in sid_workloads:
    jobs += uo.build_jobs([wl], ALL_STRATEGIES, _classes_for(wl), uo.THREAD_COUNTS, backend="sid")
for wl in purple_workloads:
    jobs += uo.build_jobs([wl], ALL_STRATEGIES, _classes_for(wl), uo.THREAD_COUNTS, backend="purple")

print(f"[run_tonight] 総ジョブ数={len(jobs)}")

run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

uo.notify(
    f"[run_tonight] 実行開始 sid={sid_workloads} purple={purple_workloads} "
    f"threads={uo.THREAD_COUNTS} jobs={len(jobs)}"
)

uo.schedule_and_run(
    jobs,
    capacity=uo.SID_CAPACITY_DEFAULT,
    run_id=run_id,
    use_exact=False,
    no_timeout=False,
    purple_capacity=uo.PURPLE_CAPACITY_DEFAULT,
)

uo.notify(f"[run_tonight] 完了 sid={sid_workloads} purple={purple_workloads} threads={uo.THREAD_COUNTS}")
print("[run_tonight] 完了")
