CC      = gcc
# GCC15検証ビルド: ホストのGCC15.2.1で-static静的リンク
# (通常版Makefileは.bind/pin_kitのPin 3.11がlibc.a未整備のため-staticを外していたが、
#  こちらはGCC比較実験用にPin 3.22/3.31での動作を確認済みの静的リンク版)
CFLAGS  = -O3 -std=gnu99 -fopenmp -static
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
