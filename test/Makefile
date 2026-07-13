# Build and (manually) exercise the deadlock.py validation programs.
# `make test` builds them and prints how to drive gdb against a live deadlock.

CC ?= cc
CFLAGS ?= -g -O0 -pthread

PROGS = deadlock_ab deadlock_self

.PHONY: test clean

test: $(PROGS)
	@echo
	@echo "Built: $(PROGS)"
	@echo "Drive the analyzer against a live deadlock, e.g.:"
	@echo "  ./deadlock_ab & sleep 2 && \\"
	@echo "    sudo gdb -q -batch -ex 'source ../deadlock.py' -ex 'deadlock scan' -p \$$!"

deadlock_ab: deadlock_ab.c
	$(CC) $(CFLAGS) $< -o $@

deadlock_self: deadlock_self.c
	$(CC) $(CFLAGS) $< -o $@

clean:
	rm -f $(PROGS)
