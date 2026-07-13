NAME = gdb
SPECNAME = smartmet-gdb
rpmsourcedir = /tmp/$(shell whoami)/rpmbuild

# Where the python modules live when installed.
PREFIX ?= /usr
datadir ?= $(PREFIX)/share
pkgdir = $(datadir)/smartmet-gdb
sysconfdir ?= /etc

MODULES = fmiprinters.py deadlock.py
INITFILE = smartmet-gdb.gdb

.PHONY: all install rpm clean test

all:
	@echo "Nothing to build (pure Python/data). Targets: install, rpm, test, clean"

install:
	mkdir -p $(DESTDIR)$(pkgdir)
	install -m 0644 $(MODULES) $(DESTDIR)$(pkgdir)/
	cp -a boost $(DESTDIR)$(pkgdir)/
	find $(DESTDIR)$(pkgdir) -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	mkdir -p $(DESTDIR)$(sysconfdir)/gdbinit.d
	install -m 0644 $(INITFILE) $(DESTDIR)$(sysconfdir)/gdbinit.d/$(INITFILE)
	# Precompile with gdb's OWN embedded Python so the bytecode cache tag
	# matches at runtime; unchecked-hash so Python never rewrites it.
	gdb -nx -q -batch -ex "python import compileall, py_compile; compileall.compile_dir('$(DESTDIR)$(pkgdir)', quiet=1, ddir='$(pkgdir)', invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)"

rpm: clean
	mkdir -p $(rpmsourcedir)
	tar -C ../ --exclude-vcs --exclude='$(NAME)/test/deadlock_ab' --exclude='$(NAME)/test/deadlock_self' \
		-cf $(rpmsourcedir)/$(SPECNAME).tar $(NAME)
	gzip -f $(rpmsourcedir)/$(SPECNAME).tar
	rpmbuild -v -ta $(rpmsourcedir)/$(SPECNAME).tar.gz

test:
	$(MAKE) -C test test

clean:
	rm -f $(rpmsourcedir)/$(SPECNAME).tar.gz
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
	$(MAKE) -C test clean 2>/dev/null || true
