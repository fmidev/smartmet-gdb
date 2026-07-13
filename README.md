# smartmet-gdb

GDB Python helpers for debugging the SmartMet Server: pretty-printers for FMI
and Boost types, plus a pthread-mutex **deadlock analyzer**.

Installed by the `smartmet-gdb` RPM to `/usr/share/smartmet-gdb`, and wired
into every gdb session via `/etc/gdbinit.d/smartmet-gdb.gdb`.

## Contents

| File | Purpose |
|---|---|
| `deadlock.py` | Wait-for-graph deadlock analyzer + `pthread_mutex_t` decoder, incl. the glibc dynamic-linker `_dl_load_lock`. See below. |
| `fmiprinters.py` | Generic "call a stringification method" pretty-printer. Renders any registered C++ type by calling a method (`to_string`, `ToStr`, `c_str`, ...) and showing the result. Not FMI-specific — see below. |
| `boost/` | The [ruediger/Boost-Pretty-Printer](https://github.com/ruediger/Boost-Pretty-Printer) package. See `boost/UPSTREAM_VERSION.txt` for the vendored commit. |
| `smartmet-gdb.gdb` | The `/etc/gdbinit.d` drop-in that registers everything. |
| `test/` | Synthetic deadlocks that validate `deadlock.py`. |

**libstdc++ printers are intentionally NOT shipped.** gdb auto-loads the
version-matched copy from the system `libstdc++` package (`std::string`,
`std::vector`, ... just work), on RHEL8 (`/usr/share/gcc-8/...`) and RHEL10
alike. A frozen vendored copy would be redundant and could mis-print types
whose layout changed.

## fmiprinters — stringify-method pretty-printer

`fmiprinters` is a small **generic** framework: for a registered C++ type it
calls a configured member method and shows the returned string as the value.
It is **not** limited to FMI types, and **not** limited to time/ISO strings —
that is just what the default rules happen to cover. Any class with a method
that returns a string (or a `std::string`) can be added.

It works two ways:

1. **Automatic detection** (on by default). For any *unregistered* class,
   fmiprinters looks for a conventional stringification method — `to_string`,
   `toString`, `str`, `ToStr`, `c_str` — and, if the class has one, calls it.
   Whether a type has a usable method is probed once per type and cached, so
   there is no per-value cost after the first encounter. Library namespaces
   (`std::`, `boost::`, `__gnu_cxx::`, …) are skipped so their own printers
   win, and a type whose method can't be called (inlined-away, or on a core
   dump) silently falls back to gdb's default dump. Toggle it with:

   ```
   (gdb) set fmi-auto-tostring off
   ```

2. **Explicit rules** in `SPECIALIZED_PRINTERS`, for the cases automatic
   detection can't guess: a non-conventional method name or a method that
   needs **constant arguments** (e.g. `"(126)"`). These take precedence over
   automatic detection and support either return type — a `std::string`
   (read via `.c_str()`) or a plain `const char*`.

Ships with these explicit rules (in `SPECIALIZED_PRINTERS`):

| Type | Method |
|---|---|
| `Fmi::date_time::DateTime` | `to_iso_extended_string()` |
| `Fmi::date_time::Date` | `to_iso_extended_string()` |
| `Fmi::date_time::TimeDuration` | `to_iso_extended_string()` |
| `TextGenPosixTime` | `ToIsoExtendedStr()` |

The dict is named `SPECIALIZED_PRINTERS` because it holds the *specialized*
cases — classes whose conversion has a non-obvious name or takes arguments —
not because it is limited to any particular kind of type.

Add your own:

```
(gdb) python import fmiprinters
(gdb) python fmiprinters.add_specialized_printer("My::Class", "to_string")
(gdb) python fmiprinters.register_fmi_printers(None)   # re-register
(gdb) python fmiprinters.show_registered_types()       # inspect
```

**Important:** rendering *calls a function in the inferior*, so these printers
require a **live process** — they do not fire on a core dump (gdb cannot call
functions without a running program, so it shows the default struct dump
there). For post-mortem work, rely on data-only printers (libstdc++, Boost)
and the deadlock analyzer instead.

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
