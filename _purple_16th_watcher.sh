#!/bin/bash
# _purple_16th_watcher.sh
# Purpleの16TH収集バッチ(batch_16th.log)完了を待ち、完了したら
# Data/comm_matrices/に同期し、MPO戦略の16TH分(S+W, 全13WL=26ジョブ)を
# 控えめな容量(2コア)で追加投入する。既存の本実行(容量21コア)とは別プロセス
# なので、競合を避けるため意図的に小さい容量にしている。
set -u
cd /home/hiragahama/ClaudeXSniper

echo "[$(date)] Purple 16THバッチの完了待ち開始"
while true; do
  if ssh yuri@172.20.2.220 'grep -q "完了" ~/deloc_test/batch_16th.log 2>/dev/null'; then
    echo "[$(date)] Purple 16THバッチ完了を検知"
    break
  fi
  sleep 120
done

echo "[$(date)] Data/comm_matrices/ へ同期開始"
rsync -avz yuri@172.20.2.220:~/deloc_test/comm_matrices/ Data/comm_matrices/ 2>&1
rsync -avz yuri@172.20.2.220:~/deloc_test/comm_matrices_sizes/ Data/comm_matrices/ 2>&1
echo "[$(date)] 同期完了"

echo "[$(date)] MPO-16TH追加ジョブ(S+W, 26件)を容量2コアで投入"
python3 -c "
import sys
from datetime import datetime
sys.path.insert(0, '/home/hiragahama/ClaudeXSniper')
from ultra_orchestrator import build_jobs, schedule_and_run, WORKLOADS
run_id = datetime.now().strftime('%Y%m%d_%H%M%S')
jobs = build_jobs(WORKLOADS, ['MPO'], ['S', 'W'], [16])
print(f'[MPO-16TH追加] {len(jobs)}件のジョブ')
schedule_and_run(jobs, capacity=2.0, run_id=run_id, use_exact=False)
"
echo "[$(date)] MPO-16TH追加ジョブ完了。watcher終了"
