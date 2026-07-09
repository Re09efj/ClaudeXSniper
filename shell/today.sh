#!/bin/bash
# CG/LU の SizeS・SizeW 本番実行(通信行列(comm.csv)取得完了後)。
# 今回は特別にCG/LUとも --machine sid で実行(通常はHEAVY_WORKLOADS外なのでpurple自動振り分け)。
# nohupを忘れないで
set -eu
cd /home/hiragahama/ClaudeXSniper

mkdir -p logs
LOGFILE="logs/today_$(date +%Y%m%d_%H%M%S).log"

nohup python3 ultra_orchestrator.py \
  --workloads CG LU \
  --bench-class S W \
  --machine sid sid \
  --strategies Packed Scatter HPO EPO MPO akarin_l \
  --no-timeout \
  > "$LOGFILE" 2>&1 &

echo "PID=$! LOG=$LOGFILE"
