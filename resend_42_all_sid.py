"""
2026-07-13: 今日のバッチ(sizeW_resend_20260712_013602.log)でタイムアウトした
42件(LU 24件@sid + dedup 18件@purple)の再送スクリプト。

元の自動リトライ(main()組み込み、旧v3イメージ)は18:05頃に無応答のまま停止
していた(LU/dedupのハングに巻き込まれたと推測、プロセス自体消滅済み)。

ユーザー指示により、42件全てをSID経由(backend="sid")で実行する。
sniper_sim_sid.CONTAINER_IMAGEは本スクリプト実行前にdetloc-firsttouch-
v12-dedupfix(LU修正v9+dedup修正v12統合)へ切り替え済み。これによりdedupも
Purpleのネイティブビルド(未修正)ではなくSIDの修正済みコンテナで実行される。

ClaudeXSniper本体には組み込まない使い捨てスクリプトだが、本番バッチへの
実際の投入なのでスクラッチパッドではなく直下に置く(実行後削除予定)。
"""
import sys
from datetime import datetime

sys.path.insert(0, "/home/hiragahama/ClaudeXSniper")

from ultra_orchestrator import build_jobs, schedule_and_run
from utility.capacity_model import SID_CAPACITY_DEFAULT
from utility.notify import notify

STRATEGIES = ["Packed", "Scatter", "HPO", "EPO", "MPO", "akarin"]

# LU: 24件 = 4threads(2,8,12,16) x 6strategies、全てbackend=sid
lu_jobs = build_jobs(["LU"], STRATEGIES, ["W"], [2, 8, 12, 16], backend="sid")

# dedup: 18件 = 3threads(8,12,16 -> resolve_valid_num_threadsで9,12,15) x 6strategies
# 元は@purpleだったが、今回は指示によりbackend=sidに強制
dedup_jobs = build_jobs(["dedup"], STRATEGIES, ["W"], [8, 12, 16], backend="sid")

jobs = lu_jobs + dedup_jobs

print(f"[RESEND] 再送ジョブ数={len(jobs)} (LU={len(lu_jobs)}, dedup={len(dedup_jobs)}) 全てbackend=sid")
for j in jobs:
    print(f"  {j}")

run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

notify(f"[resend_42_all_sid] 42件再送開始(全件sid、v12-dedupfixイメージ) run_id={run_id}")

timeout_failures = schedule_and_run(jobs, SID_CAPACITY_DEFAULT, run_id, use_exact=False,
                                    no_timeout=True, purple_capacity=0.0)

if timeout_failures:
    print(f"\n[RESULT] 再送後もタイムアウトした件数: {len(timeout_failures)}")
    for j in timeout_failures:
        print(f"  {j}")
else:
    print("\n[RESULT] 全件成功(タイムアウト無し)")

notify(f"[resend_42_all_sid] 42件再送完了 run_id={run_id} "
      f"残タイムアウト={len(timeout_failures)}")
