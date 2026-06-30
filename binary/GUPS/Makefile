CC      = gcc
CFLAGS  = -O3 -fopenmp -static
TARGET  = gups

# Default table size: 2^22 = 4M entries = 32 MB
# Override with: make LOG2_TABLESIZE=24
LOG2_TABLESIZE ?= 22

all: $(TARGET)

$(TARGET): gups.c
	$(CC) $(CFLAGS) -DLOG2_TABLESIZE=$(LOG2_TABLESIZE) -o $@ $<

clean:
	rm -f $(TARGET)

.PHONY: all clean
