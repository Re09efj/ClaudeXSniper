#!/bin/bash
# rebuild_sift_o2.sh
# snipersim/snipersim:latest 内の sift_recorder を -O2 で再ビルドし
# イメージを更新する
#
# 使い方: bash rebuild_sift_o2.sh   (sudo 不要)
# 注意: 実行中の Sniper/Podman プロセスが完了してから実行すること

set -e

IMAGE="snipersim/snipersim:latest"
TMP_CONTAINER="sniper-rebuild-$$"

echo "=== sift_recorder -O2 再ビルド (Podman) ==="
echo "イメージ: $IMAGE"

# クリーンアップトラップ
cleanup() {
    podman rm -f "$TMP_CONTAINER" 2>/dev/null || true
}
trap cleanup EXIT

# ─── 一時コンテナを起動してビルド ────────────────────────────────
echo ""
echo "[1] 一時コンテナ起動..."
podman run --name "$TMP_CONTAINER" "$IMAGE" bash -c '
set -e
SNIPER=/root/sniper
RECORDER=$SNIPER/sift/recorder
PIN_ROOT=$SNIPER/pin_kit
CONFIG_DIR=$PIN_ROOT/source/tools/Config

echo "  sift/recorder: $RECORDER"

# -O3 を探して -O2 に置換
echo "[2] -O3 を検索・置換..."
FILES=$(grep -rl "\-O3" "$CONFIG_DIR" "$RECORDER" 2>/dev/null || true)
if [ -n "$FILES" ]; then
    echo "$FILES" | while read -r f; do
        sed -i "s/-O3/-O2/g" "$f"
        echo "  変更: $f"
    done
else
    echo "  -O3 が見つかりません。CXXOPT=-O2 で上書きビルドします"
fi

echo "[3] クリーンビルド..."
cd "$RECORDER"
if [ -n "$FILES" ]; then
    make PIN_ROOT="$PIN_ROOT" clean 2>&1 | tail -3
    make PIN_ROOT="$PIN_ROOT" 2>&1 | tail -15
else
    make PIN_ROOT="$PIN_ROOT" clean 2>&1 | tail -3
    make PIN_ROOT="$PIN_ROOT" CXXOPT="-O2" 2>&1 | tail -15
fi

echo "[4] フラグ確認..."
strings obj-intel64/sift_recorder.o 2>/dev/null | grep -oE "\-O[0-9]" | sort -u || true
echo "  ビルド完了"
'

# ─── イメージにコミット ───────────────────────────────────────────
echo ""
echo "[5] イメージ更新中..."
podman commit "$TMP_CONTAINER" "$IMAGE"

echo ""
echo "=== 完了 ==="
echo "次回の podman run から -O2 済み sift_recorder が使われます"
podman images "$IMAGE"
