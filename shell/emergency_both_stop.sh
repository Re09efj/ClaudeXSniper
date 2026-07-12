#!/bin/bash
# emergency_stop.sh
# LoadAverage異常(SID/Purpleいずれか、または両方)を検知したときに使う緊急停止スクリプト。
#
# 【重要】これは素のシェル(bash + coreutils + ssh + podman)のみで書かれている。
# LoadAverageが50〜60を超えるような極限状況ではPythonの起動(fork+import)すら
# まともにスケジューリングされない/固まるリスクがあるため、依存を最小限に抑えている。
# Claude Code(VSCode Remote-SSH経由)との通信が固まって使えない場合でも、
# 別のターミナル/生のSSHセッションから直接このスクリプトを叩けば止まる設計。
#
# 使い方: bash shell/emergency_stop.sh
#   (ClaudeXSniperのどこからでも動くよう絶対パス/固定ホスト名で書いてある)
#
# やること:
#   1. SID(ローカル)側: ultra_orchestrator.py を止める
#   2. SID(ローカル)側: sniper_* という名前のPodmanコンテナを全部止める
#   3. Purple(リモート)側: sniper-detloc / pin_kit / run-sniper 関連プロセスをkill。
#      「送信して終わり」ではなく、残存プロセス数が0になったとSSH越しに確認できるまで
#      2秒間隔・最大30回、pkillの再送信を繰り返す
#   4. 各段階でload averageを表示し、下がったことを目視確認できるようにする
#
# 中途半端に終わったOutputs/ディレクトリの掃除(整合性チェック)はここではやらない。
# それは別スクリプト(cleanup_incomplete_outputs.py等)で、負荷が落ち着いてから行うこと。

echo "=========================================="
echo " EMERGENCY STOP: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

echo ""
echo "--- [1/4] SID: hiragahama所有のpython3プロセスを全部停止 ---"
# 2026-07-13: 以前は 'ultra_orchestrator\.py' というファイル名の完全一致で
# しか狙えなかったため、Claude Codeが作る使い捨てリトライスクリプト
# (resend_*.py等、毎回名前が変わる)がこのpkillに引っかからず、コンテナだけ
# [2/4]で止まった後もジョブ投入ループが生き残って新規コンテナを起動し続ける
# 事故が発生した。ファイル名を当てに行くのをやめ、「hiragahama所有の
# python3プロセスは(名前に関わらず)全部止める」方式に変更(ユーザー指示)。
if pkill -u hiragahama -TERM -x python3 2>/dev/null; then
    echo "SIGTERM送信。3秒待機..."
    sleep 3
    pkill -u hiragahama -KILL -x python3 2>/dev/null && echo "残っていたのでSIGKILL送信"
else
    echo "hiragahama所有のpython3プロセスは動いていなかった"
fi

echo ""
echo "--- [2/4] SID: sniper_* Podmanコンテナを停止 ---"
CONTAINERS=$(podman ps --format '{{.Names}}' 2>/dev/null | grep '^sniper_')
if [ -n "$CONTAINERS" ]; then
    echo "$CONTAINERS"
    echo "$CONTAINERS" | xargs -r -n1 podman stop -t 5
else
    echo "停止対象のコンテナなし"
fi

echo ""
echo "--- SID load average (直後) ---"
uptime

echo ""
echo "--- [3/4] Purple(yuri@172.20.2.220): sniper関連プロセスをkill(確認できるまでリトライ) ---"
SSH_HOST="yuri@172.20.2.220"
SSH_OPTS="-o ConnectTimeout=15 -o BatchMode=yes -o ServerAliveInterval=10 -o ServerAliveCountMax=3"

# Purple自体が過負荷でSSH接続が一発で失敗する(ret255)ことがある(2026-07-11実績)。
# 「送信して終わり」にせず、残存プロセス数が0になったことをSSH越しに確認できるまで
# 2秒間隔・最大30回、pkillの再送信+確認を1セットにして繰り返す。
#
# 【注意・既知の落とし穴】単純に `pkill -9 -f "run-sniper"` のようなワンライナーを
# ssh経由で送ると、そのパターン文字列(例: "run-sniper")がリモートで実行される
# シェル自身のコマンドライン(スクリプト全文がそのままargvに載る)にも含まれるため、
# pkillが「自分自身を実行しているシェル」まで巻き込んでkillしてしまい、確認用echoの
# 出力前にセッションごと死ぬ(exit-signalでrc=255)という自爆事故が実際に発生した
# (2026-07-11)。対処として pgrep で対象PIDを列挙し、自分自身のPID($$)を明示的に
# 除外してから kill する方式に変更している。
PURPLE_MAX_ATTEMPTS=30
PURPLE_INTERVAL=2
purple_confirmed=0

for i in $(seq 1 "$PURPLE_MAX_ATTEMPTS"); do
    result=$(timeout 15 ssh $SSH_OPTS "$SSH_HOST" '
        mypid=$$
        for pat in "claudex_akarin/sniper-detloc" "claudex_akarin/pin_kit"; do
            for p in $(pgrep -f "$pat" 2>/dev/null); do
                [ "$p" = "$mypid" ] && continue
                kill -9 "$p" 2>/dev/null
            done
        done
        ps -eo pid,cmd | grep -E "claudex_akarin/sniper-detloc|claudex_akarin/pin_kit" | grep -v grep | grep -Ev "^[[:space:]]*${mypid}[[:space:]]" | wc -l
    ' 2>/dev/null)

    if [ -n "$result" ] && [ "$result" -eq 0 ] 2>/dev/null; then
        echo "[$i/$PURPLE_MAX_ATTEMPTS] 確認OK: 残存プロセス0件"
        purple_confirmed=1
        break
    elif [ -z "$result" ]; then
        echo "[$i/$PURPLE_MAX_ATTEMPTS] SSH接続失敗。${PURPLE_INTERVAL}秒後リトライ..."
        sleep "$PURPLE_INTERVAL"
    else
        echo "[$i/$PURPLE_MAX_ATTEMPTS] まだ残存プロセスあり(${result}件)。再killして${PURPLE_INTERVAL}秒後リトライ..."
        sleep "$PURPLE_INTERVAL"
    fi
done

echo ""
echo "--- [4/4] Purple load average (最終確認) ---"
timeout 15 ssh $SSH_OPTS "$SSH_HOST" 'uptime'

if [ "$purple_confirmed" -ne 1 ]; then
    echo ""
    echo "!!! 警告: ${PURPLE_MAX_ATTEMPTS}回試行してもPurple側の残存プロセス0件を確認できなかった。"
    echo "!!! 手動でPurpleにログインして状態を確認すること。"
fi

echo ""
echo "=========================================="
echo " 完了。load averageは徐々に下がる(1分値→5分値→15分値の順)。"
echo " 再開する場合は今回の停止対象ジョブを手動で再投入すること。"
echo "=========================================="
