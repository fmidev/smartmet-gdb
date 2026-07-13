# smartmet-gdb

GDB Python helpers for debugging the SmartMet Server: pretty-printers for FMI
and Boost types, plus a pthread-mutex **deadlock analyzer**.

Installed by the `smartmet-gdb` RPM to `/usr/share/smartmet-gdb`, and wired
into every gdb session via `/etc/gdbinit.d/smartmet-gdb.gdb`.

## Contents

| File | Purpose |
|---|---|
| `deadlock.py` | Wait-for-graph deadlock analyzer + `pthread_mutex_t` decoder, incl. the glibc dynamic-linker `_dl_load_lock`. See below. |
| `fmiprinters.py` | Pretty-printers for FMI types with `to_string`-like methods (`Fmi::date_time::*`, `TextGenPosixTime`, ...). Extend `ISO_PRINTERS`. |
| `boost/` | The [ruediger/Boost-Pretty-Printer](https://github.com/ruediger/Boost-Pretty-Printer) package. See `boost/UPSTREAM_VERSION.txt` for the vendored commit. |
| `smartmet-gdb.gdb` | The `/etc/gdbinit.d` drop-in that registers everything. |
| `test/` | Synthetic deadlocks that validate `deadlock.py`. |

**libstdc++ printers are intentionally NOT shipped.** gdb auto-loads the
version-matched copy from the system `libstdc++` package (`std::string`,
`std::vector`, ... just work), on RHEL8 (`/usr/share/gcc-8/...`) and RHEL10
alike. A frozen vendored copy would be redundant and could mis-print types
whose layout changed.

## The deadlock analyzer

| Command | Purpose |
|---|---|
| `deadlock scan` | Scan all threads, build the wait-for graph, report deadlock **cycles** and self-deadlocks. Start here. |
| `deadlock mutex EXPR` | Decode a `pthread_mutex_t` (lvalue or address): owner→thread#, count, kind flags, futex/PI/robust state. |
| `deadlock dllock` | Dump `_rtld_global._dl_load_lock` and interpret the recursion/leak shape. |
| `deadlock waiters EXPR` | Threads blocked on a specific mutex. |
| `deadlock owner EXPR` | Describe / switch to the thread holding a mutex. |

Works on a live process (`gdb -p PID`) or a core dump (`gdb EXE CORE`).

The scanner needs **no glibc debuginfo**: a thread blocked in
`pthread_mutex_lock` is parked in the `futex` syscall, so on x86-64 the saved
registers give the mutex address (`$rdi`) and the owner is read from
`__data.__owner` at fixed offsets. Only `deadlock dllock` uses symbolic
`_rtld_global` access, i.e. needs glibc debuginfo
(`dnf debuginfo-install glibc`); without it, a blocked `_dl_load_lock` is
still detected via its backtrace signature.

x86-64 Linux only for the auto-scan; `deadlock mutex EXPR` works on any arch.

## Boost printer version

The Boost printers support **Boost ≤ 1.73**. `smartmet-gdb.gdb` therefore
**pins** the version:

```
SMARTMET_BOOST_VERSION = (1, 69, 0)   # matches RHEL8 production (boost169)
```

Pinning is deliberate — the printers' autodetection *compiles and runs* a C++
program including `<boost/version.hpp>` at gdb startup, which needs a compiler
and boost headers (usually absent on servers) and is slow. Edit this value in
the installed `%config(noreplace)` file to match your deployment. On RHEL9+
(Boost 1.83) the printers register but may be incomplete, since upstream has
not tracked Boost past 1.73.

## Building the RPM

```bash
make rpm      # tars the repo and runs rpmbuild -ta
```

The package precompiles `.pyc` using **gdb's own embedded Python** (so the
cache tag matches at runtime) with unchecked-hash invalidation, so gdb never
needs write access to the install directory. `noarch`, requires `gdb`.

## Tests

```bash
cd test && make test          # builds deadlock_ab / deadlock_self
./deadlock_ab & sleep 2
sudo gdb -q -batch -ex 'source ../deadlock.py' -ex 'deadlock scan' -p $!
```
