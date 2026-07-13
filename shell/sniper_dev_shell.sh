#!/bin/bash
# sniper_dev_shell.sh
# SID上でSniper C++本体をいじって試行錯誤するときの高速イテレーション用シェル。
#
# 通常のワークフロー(scratchpadにコピー→podman execで書き込み→make→podman commit)は
# イテレーション1回ごとにイメージを1枚焼くコストがあるため、コード修正→再ビルド→
# 動作確認を何度も繰り返すデバッグ中は非効率(2026-07-13、bind mountに切り替える案を
# ユーザーと合意)。
#
# 代わりに .bind/(gitignore済み、フルソースツリー+ビルド成果物入り)を
# ホスト側で直接編集し、コンテナには /root/sniper としてbind mountする。
# コンテナ内でmakeすれば増分ビルドで済み、イメージを焼き直す必要がない。
#
# 使い方:
#   bash shell/sniper_dev_shell.sh                 # 対話シェルを開く(デフォルト: v12-dedupfixベース)
#   bash shell/sniper_dev_shell.sh <ベースイメージ>  # 別のベースイメージから開始したい場合
#
# 満足いく修正ができたら:
#   1. .bind/内の直したファイルを .SniperChange/ 配下の対応パスにコピー
#      (元のSniperソースツリー相対パスは共通なので単純cpでよい)
#   2. 別のスクリプト(通常のscratchpadビルド手順)で正式にイメージへ焼き込み、
#      新タグをultra_orchestrator.py側に反映する
#   3. Documents/SniperBugFix.md に変更内容を記録する
#
# .bind/ 自体は使い捨てではない(gitignore対象の作業コピーとして常駐)。
# 中身を壊した/汚したと思ったら、以下で作り直せる:
#   rm -rf .bind
#   CID=$(podman create localhost/snipersim/snipersim:detloc-firsttouch-v12-dedupfix true)
#   podman cp "$CID:/root/sniper" .bind
#   podman rm "$CID"

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$REPO_ROOT/.bind"
BASE_IMAGE="${1:-localhost/snipersim/snipersim:detloc-firsttouch-v12-dedupfix}"

if [ ! -d "$WORKDIR" ]; then
    echo "エラー: $WORKDIR が無い。README.mdの手順で作り直すこと。"
    exit 1
fi

echo "ベースイメージ: $BASE_IMAGE"
echo "bind mount元 : $WORKDIR"
echo ""
echo "コンテナ内での典型的な流れ:"
echo "  cd /root/sniper"
echo "  make -j\$(nproc)                          # 増分ビルド"
echo "  ./run-sniper -n 16 -d /tmp/out -c <cfg> -- <binary> <args>  # 動作確認"
echo ""

podman run --rm -it \
    -v "$WORKDIR:/root/sniper" \
    "$BASE_IMAGE" \
    bash
