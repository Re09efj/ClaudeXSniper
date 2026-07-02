#!/bin/bash
# rebuild_sift_o2.sh
# snipersim/snipersim:latest 内の sift_recorder を指定最適化レベルで再ビルドし
# イメージを更新する
#
# 使い方: bash rebuild_sift_o2.sh [OPT_LEVEL]
#   例: bash rebuild_sift_o2.sh       → -O1 (デフォルト)
#       bash rebuild_sift_o2.sh O2    → -O2
# 注意: 実行中の Sniper/Podman プロセスが完了してから実行すること

set -e

OPT_LEVEL="${1:-O1}"
IMAGE="snipersim/snipersim:latest"
TMP_CONTAINER="sniper-rebuild-$$"

echo "=== sift_recorder -${OPT_LEVEL} 再ビルド (Podman) ==="
echo "イメージ: $IMAGE"

# クリーンアップトラップ
cleanup() {
    podman rm -f "$TMP_CONTAINER" 2>/dev/null || true
}
trap cleanup EXIT

# ─── 一時コンテナを起動してビルド ────────────────────────────────
echo ""
echo "[1] 一時コンテナ起動..."
podman run --name "$TMP_CONTAINER" "$IMAGE" bash -c "
set -e
SNIPER=/root/sniper
RECORDER=\$SNIPER/sift/recorder
PIN_ROOT=\$SNIPER/pin_kit
CONFIG_DIR=\$PIN_ROOT/source/tools/Config
OPT=-${OPT_LEVEL}

echo \"  sift/recorder: \$RECORDER\"
echo \"  最適化レベル : \$OPT\"

# 既存の -O? フラグをすべて目的レベルに置換
echo \"[2] 最適化フラグを \$OPT に統一...\"
FILES=\$(grep -rl '\-O[0-9]' \"\$CONFIG_DIR\" \"\$RECORDER\" 2>/dev/null || true)
if [ -n \"\$FILES\" ]; then
    echo \"\$FILES\" | while read -r f; do
        sed -i \"s/-O[0-9]/\$OPT/g\" \"\$f\"
        echo \"  変更: \$f\"
    done
else
    echo \"  既存の -O フラグが見つかりません\"
fi

echo \"[3] クリーンビルド...\"
cd \"\$RECORDER\"
make PIN_ROOT=\"\$PIN_ROOT\" clean 2>&1 | tail -3
make PIN_ROOT=\"\$PIN_ROOT\" CXXOPT=\"\$OPT\" 2>&1 | tail -15

echo \"[4] フラグ確認...\"
grep -r '\-O[0-9]' \"\$CONFIG_DIR/makefile.unix.config\" 2>/dev/null | head -3 || true
echo \"  ビルド完了\"
"

# ─── イメージにコミット ───────────────────────────────────────────
echo ""
echo "[5] イメージ更新中..."
podman commit "$TMP_CONTAINER" "$IMAGE"

echo ""
echo "=== 完了 ==="
echo "次回の podman run から -${OPT_LEVEL} 済み sift_recorder が使われます"
podman images "$IMAGE"
