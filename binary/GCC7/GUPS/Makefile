CC      = gcc
# -static は外す: SIDのSniperコンテナにlibc.a等の静的ライブラリが無く
# リンクできないため。両ホスト(SID/Purple)ともdynamic + -lrtでビルドする。
# Purpleのgccはデフォルトc89相当のため-std=gnu99が必要。
CFLAGS  = -O3 -std=gnu99 -fopenmp
LDLIBS  = -lrt
TARGET  = gups

# Default table size: 2^22 = 4M entries = 32 MB (シミュレート対象L3=4MBを
# 大きく超えるサイズ。ホスト実機のL3ではなくarrow_lake.cfg側のL3設定が基準)
# Override with: make LOG2_TABLESIZE=24
LOG2_TABLESIZE ?= 22

all: $(TARGET)

$(TARGET): gups.c
	$(CC) $(CFLAGS) -DLOG2_TABLESIZE=$(LOG2_TABLESIZE) -o $@ $< $(LDLIBS)

clean:
	rm -f $(TARGET)

.PHONY: all clean
