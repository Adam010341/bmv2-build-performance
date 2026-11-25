# bmbench tools. Plain C: bmgen, bmrecv and bmnic need only libc/libm; bmnull also needs libpcap.
CC ?= gcc
CFLAGS ?= -O2 -g -Wall -Wextra -std=gnu11
BIN := bin

all: $(BIN)/bmgen $(BIN)/bmrecv $(BIN)/bmnull $(BIN)/bmnic

$(BIN)/%: src/%.c src/bmpkt.h
	@mkdir -p $(BIN)
	$(CC) $(CFLAGS) -o $@ $< -lm

# bmnull is the only tool that links libpcap (it mimics how bmv2 opens its ports); it does not use bmpkt.h.
$(BIN)/bmnull: src/bmnull.c
	@mkdir -p $(BIN)
	$(CC) $(CFLAGS) -o $@ $< -lpcap

test: all
	bash tests/test_offline.sh

clean:
	rm -rf $(BIN)

.PHONY: all test clean
