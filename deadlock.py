"""
deadlock.py -- gdb Python extension for hunting pthread-mutex deadlocks.

Adds a `deadlock` command family that builds a wait-for graph across all
threads, detects cycles, decodes any pthread_mutex_t, and annotates
well-known runtime locks (glibc dynamic-linker, C++ static-init guard,
malloc arenas, stdio / fork locks).

Load it from gdb:

    (gdb) source /home/mheiskan/hub/tools/gdb/deadlock.py
    (gdb) deadlock scan

Or add the source line to ~/.gdbinit (done by the installer).

Commands:
    deadlock scan            build the wait-for graph and report cycles
    deadlock mutex  EXPR     decode a pthread_mutex_t (lvalue or address)
    deadlock dllock          dump _rtld_global._dl_load_lock + rtld counter
    deadlock waiters EXPR    which threads are blocked on this mutex
    deadlock owner   EXPR    describe / switch to the mutex's owning thread

Design notes
------------
The engine does NOT need glibc debuginfo. A thread blocked in
pthread_mutex_lock is parked inside the `futex` syscall; on x86-64 the
saved user registers at syscall entry give us everything:
    $orig_rax == 202 (SYS_futex)  -> it is a futex wait
    $rsi (op)   & CMD_MASK is a WAIT/LOCK_PI op
    $rdi (uaddr) == &mutex->__data.__lock == the mutex address
(`__data.__lock` is the first member of pthread_mutex_t, so the futex
address equals the mutex address.)  We read the owner TID straight out of
the struct at fixed x86-64 glibc offsets.  When the program has debuginfo
for pthread_mutex_t we still use it for a nicer `deadlock mutex` dump.

x86-64 / Linux / glibc-NPTL only for the register trick.  On other
architectures `deadlock mutex EXPR` still works via struct offsets.
"""

import gdb

# --------------------------------------------------------------------------
# Constants (x86-64 Linux / glibc NPTL)
# --------------------------------------------------------------------------

SYS_FUTEX = 202  # x86-64 __NR_futex

FUTEX_WAIT = 0
FUTEX_LOCK_PI = 6
FUTEX_WAIT_BITSET = 9
FUTEX_WAIT_REQUEUE_PI = 11
FUTEX_LOCK_PI2 = 13
FUTEX_PRIVATE_FLAG = 128
FUTEX_CLOCK_REALTIME = 256
FUTEX_CMD_MASK = ~(FUTEX_PRIVATE_FLAG | FUTEX_CLOCK_REALTIME) & 0xFFFFFFFF

# Commands that mean "this thread is blocked acquiring something".
WAIT_CMDS = {
    FUTEX_WAIT,
    FUTEX_LOCK_PI,
    FUTEX_WAIT_BITSET,
    FUTEX_WAIT_REQUEUE_PI,
    FUTEX_LOCK_PI2,
}

# pthread_mutex_t.__data field offsets (x86-64 glibc; stable for years).
OFF_LOCK = 0     # int   __lock
OFF_COUNT = 4    # unsigned __count
OFF_OWNER = 8    # int   __owner
OFF_NUSERS = 12  # unsigned __nusers
OFF_KIND = 16    # int   __kind

# __kind low bits -> mutex type
MUTEX_TYPE = {0: "normal/timed", 1: "recursive", 2: "errorcheck", 3: "adaptive"}
KIND_TYPE_MASK = 3
KIND_ROBUST = 0x10
KIND_PRIO_INHERIT = 0x20   # PI
KIND_PRIO_PROTECT = 0x40
KIND_PSHARED = 0x80

# futex/PI lock-word bits
FUTEX_TID_MASK = 0x3FFFFFFF
FUTEX_OWNER_DIED = 0x40000000
FUTEX_WAITERS = 0x80000000

MAX_FRAMES = 64  # cap backtrace walks against corrupt stacks

# --------------------------------------------------------------------------
# Known-lock registry
# --------------------------------------------------------------------------

# (a) Address-matched locks: resolvable only when the relevant debuginfo /
#     exported symbol is present.  Built lazily per session.
_ADDR_LOCK_SYMS = [
    (
        "_rtld_global._dl_load_lock",
        "_dl_load_lock",
        "glibc dynamic-linker load lock (recursive)",
        "dlopen/dlclose re-entered from a shared-object constructor or "
        "destructor, or from an atfork/signal handler, while the lock is held.",
    ),
    (
        "_rtld_global._dl_load_write_lock",
        "_dl_load_write_lock",
        "glibc dynamic-linker link-map write lock",
        "contends with _dl_load_lock during concurrent load/unload of shared "
        "objects.",
    ),
    (
        "_rtld_global._dl_load_tls_lock",
        "_dl_load_tls_lock",
        "glibc dynamic-linker TLS lock (glibc >= 2.34)",
        "guards static-TLS setup during dlopen; present in a hang here is a "
        "strong signal of TLS-vs-dlopen ordering trouble.",
    ),
]

# (b) Stack-signature locks: matched by substrings appearing in the blocked
#     thread's backtrace.  Work even against fully stripped libc because the
#     entry-point symbols are exported.
_STACK_SIGS = [
    (
        ("cxa_guard_acquire",),
        "C++ static-init guard",
        "function-local `static` initialization guard",
        "reentrant initialization of a function-local `static`. If the "
        "initializing thread is also the waiter, it is a self-deadlock -- "
        "classically a SmartMet plugin/engine constructor whose static "
        "initializer re-enters its own initialization.",
    ),
    (
        ("_int_malloc", "_int_free", "malloc_consolidate", "arena_get", "sysmalloc"),
        "malloc arena lock",
        "glibc malloc arena lock",
        "hang after fork() in a multithreaded process, or a signal handler "
        "that allocates while malloc holds the arena lock.",
    ),
    (
        ("_IO_flush_all", "_IO_cleanup", "_IO_file_", "funlockfile", "fflush"),
        "stdio lock",
        "stdio FILE / _IO_list lock",
        "buffered stdio used after fork() or from a signal handler.",
    ),
    (
        ("__run_fork_handlers", "__libc_fork", "atfork"),
        "fork/atfork lock",
        "glibc fork / atfork lock",
        "a pthread_atfork handler is blocked, or fork() contends with another "
        "thread's lock.",
    ),
    (
        ("_dl_open", "dlopen", "_dl_close", "dlclose", "_dl_map_object", "rtld_lock"),
        "dynamic-linker",
        "dynamic-linker (dl) operation",
        "dlopen/dlclose contention -- see _dl_load_lock.",
    ),
]


def _build_addr_registry():
    """Resolve known-lock futex addresses that this session can see."""
    out = []
    for sym, name, desc, hint in _ADDR_LOCK_SYMS:
        try:
            val = gdb.parse_and_eval("&(%s).__data.__lock" % sym)
            out.append((int(val), name, desc, hint))
        except gdb.error:
            # Type not available (no ld.so debuginfo) -- skip; the stack
            # signature will still label dl operations.
            pass
    return out


# --------------------------------------------------------------------------
# Low-level memory / register helpers
# --------------------------------------------------------------------------

def _inferior():
    return gdb.selected_inferior()


def _rd(addr, size):
    return _inferior().read_memory(addr, size)


def rd_u32(addr):
    return int.from_bytes(_rd(addr, 4), "little", signed=False)


def rd_s32(addr):
    return int.from_bytes(_rd(addr, 4), "little", signed=True)


def _is_x86_64():
    try:
        return "x86-64" in gdb.selected_frame().architecture().name()
    except gdb.error:
        return False


def threads_by_lwp():
    """Map kernel TID (LWP, == mutex.__owner) -> gdb.InferiorThread."""
    out = {}
    for t in _inferior().threads():
        # ptid == (pid, lwp, tid); lwp is the kernel TID matching __owner.
        lwp = t.ptid[1]
        if lwp:
            out[lwp] = t
    return out


def frame_names(thread, limit=MAX_FRAMES):
    """Backtrace function names for a thread (newest first)."""
    thread.switch()
    names = []
    try:
        f = gdb.newest_frame()
    except gdb.error:
        return names
    n = 0
    while f is not None and n < limit:
        try:
            nm = f.name()
        except gdb.error:
            nm = None
        names.append(nm or "??")
        try:
            f = f.older()
        except gdb.error:
            break
        n += 1
    return names


def one_line_frame(thread):
    """A short 'where is this thread' string."""
    thread.switch()
    try:
        f = gdb.newest_frame()
        nm = f.name() or "??"
        pc = int(f.pc())
        return "%s (0x%x)" % (nm, pc)
    except gdb.error:
        return "??"


# --------------------------------------------------------------------------
# Mutex decoding
# --------------------------------------------------------------------------

class MutexInfo:
    def __init__(self, addr):
        self.addr = addr
        self.lock = rd_s32(addr + OFF_LOCK)
        self.count = rd_u32(addr + OFF_COUNT)
        self.owner = rd_s32(addr + OFF_OWNER)
        self.nusers = rd_u32(addr + OFF_NUSERS)
        self.kind = rd_s32(addr + OFF_KIND)

        self.type = MUTEX_TYPE.get(self.kind & KIND_TYPE_MASK, "?")
        self.robust = bool(self.kind & KIND_ROBUST)
        self.pi = bool(self.kind & KIND_PRIO_INHERIT)
        self.pp = bool(self.kind & KIND_PRIO_PROTECT)
        self.pshared = bool(self.kind & KIND_PSHARED)

        lockw = self.lock & 0xFFFFFFFF
        self.waiters = bool(lockw & FUTEX_WAITERS)
        self.owner_died = bool(lockw & FUTEX_OWNER_DIED)

        # Effective owner TID.  Normal/recursive/errorcheck mutexes record it
        # in __owner; PI mutexes keep it in the low bits of the lock word.
        if self.owner:
            self.eff_owner = self.owner
        elif self.pi:
            self.eff_owner = lockw & FUTEX_TID_MASK
        else:
            self.eff_owner = 0

    def flag_str(self):
        flags = [self.type]
        if self.robust:
            flags.append("robust")
        if self.pi:
            flags.append("PI")
        if self.pp:
            flags.append("prio-protect")
        if self.pshared:
            flags.append("pshared")
        return ", ".join(flags)

    def describe(self, lwp_map=None):
        lines = []
        lines.append("mutex @ 0x%x" % self.addr)
        lines.append("  __lock   = %d  (0x%x)%s%s" % (
            self.lock, self.lock & 0xFFFFFFFF,
            "  WAITERS" if self.waiters else "",
            "  OWNER_DIED" if self.owner_died else ""))
        lines.append("  __count  = %d" % self.count)
        lines.append("  __owner  = %d" % self.owner)
        lines.append("  __nusers = %d" % self.nusers)
        lines.append("  __kind   = %d  (%s)" % (self.kind, self.flag_str()))

        eo = self.eff_owner
        if eo == 0:
            if self.lock == 0:
                lines.append("  -> unlocked")
            else:
                lines.append("  -> locked, owner not recorded "
                             "(plain contended fast-path or non-owner-tracking lock)")
        else:
            who = _owner_desc(eo, lwp_map)
            lines.append("  -> held by %s" % who)
            if self.owner_died:
                lines.append("     (!) OWNER_DIED set: holder exited without "
                             "unlocking a robust mutex")
        return "\n".join(lines)


def _owner_desc(lwp, lwp_map=None):
    if lwp_map is None:
        lwp_map = threads_by_lwp()
    t = lwp_map.get(lwp)
    if t is None:
        return ("LWP %d (no live gdb thread -- exited, or a stale/robust owner)"
                % lwp)
    name = (" \"%s\"" % t.name) if t.name else ""
    return "thread #%d [LWP %d]%s @ %s" % (t.num, lwp, name, one_line_frame(t))


def resolve_mutex_addr(expr):
    """Accept either a pthread_mutex_t lvalue or a plain address expression."""
    v = gdb.parse_and_eval(expr)
    t = v.type.strip_typedefs()
    if t.code == gdb.TYPE_CODE_PTR:
        return int(v)
    if t.code in (gdb.TYPE_CODE_INT, gdb.TYPE_CODE_ENUM):
        return int(v)
    # An lvalue struct -- take its address.
    try:
        return int(v.address)
    except Exception:
        return int(gdb.parse_and_eval("&(%s)" % expr))


# --------------------------------------------------------------------------
# Blocked-thread detection (the wait-for graph edge source)
# --------------------------------------------------------------------------

class Blocked:
    def __init__(self, thread, uaddr, op, cmd):
        self.thread = thread
        self.lwp = thread.ptid[1]
        self.uaddr = uaddr
        self.op = op
        self.cmd = cmd
        self.bt = frame_names(thread)
        self.kind_label = None   # "mutex" / "dl" / "guard" / "other"
        self.annotation = None   # (name, desc, hint)
        self.mutex = None        # MutexInfo, if a mutex
        self.owner_lwp = 0

    def is_lock(self):
        return self.kind_label in ("mutex", "dl")


def futex_target(thread):
    """If `thread` is parked in a futex WAIT/LOCK_PI, return (uaddr, op, cmd)."""
    thread.switch()
    if not _is_x86_64():
        return None
    try:
        f = gdb.newest_frame()
        orig = int(f.read_register("orig_rax")) & 0xFFFFFFFFFFFFFFFF
    except gdb.error:
        return None
    if orig != SYS_FUTEX:
        return None
    try:
        op = int(f.read_register("rsi")) & 0xFFFFFFFF
        uaddr = int(f.read_register("rdi")) & 0xFFFFFFFFFFFFFFFF
    except gdb.error:
        return None
    cmd = op & FUTEX_CMD_MASK
    if cmd not in WAIT_CMDS:
        return None
    return (uaddr, op, cmd)


def _match_stack_sig(bt):
    for subs, name, desc, hint in _STACK_SIGS:
        for frame in bt:
            for s in subs:
                if s in frame:
                    return (name, desc, hint)
    return None


def _is_guard_sig(name):
    return name == "C++ static-init guard"


def _is_dl_sig(name):
    return name == "dynamic-linker"


def classify(blocked, addr_registry):
    """Decide what `blocked.uaddr` is and fill in owner/annotation."""
    # 1. Exact address match against known runtime locks.
    for addr, name, desc, hint in addr_registry:
        if addr == blocked.uaddr:
            blocked.kind_label = "dl"
            blocked.annotation = (name, desc, hint)
            try:
                blocked.mutex = MutexInfo(blocked.uaddr)
                blocked.owner_lwp = blocked.mutex.eff_owner
            except gdb.error:
                pass
            return

    # 2. Stack-signature annotation (dl / guard / malloc / stdio / fork).
    sig = _match_stack_sig(blocked.bt)

    # 3. Does the backtrace look like a pthread mutex acquisition?
    mutex_sig = any(
        s in fn
        for fn in blocked.bt
        for s in ("pthread_mutex_lock", "pthread_mutex_timedlock",
                  "lll_lock_wait", "__pthread_mutex")
    )

    if mutex_sig:
        try:
            blocked.mutex = MutexInfo(blocked.uaddr)
            blocked.owner_lwp = blocked.mutex.eff_owner
            blocked.kind_label = "mutex"
        except gdb.error:
            blocked.kind_label = "other"
        if sig:
            blocked.annotation = sig
        return

    if sig:
        # No pthread_mutex frame but a known signature (guard/malloc/dl/...).
        name = sig[0]
        if _is_guard_sig(name):
            blocked.kind_label = "guard"
        elif _is_dl_sig(name):
            blocked.kind_label = "dl"
            try:
                blocked.mutex = MutexInfo(blocked.uaddr)
                blocked.owner_lwp = blocked.mutex.eff_owner
            except gdb.error:
                pass
        else:
            blocked.kind_label = "other"
        blocked.annotation = sig
        return

    # 4. A futex wait we can't attribute to a mutex (condvar/rwlock/sem).
    blocked.kind_label = "other"


def collect_blocked():
    """Scan all threads; return list[Blocked] plus the resolved lwp map."""
    addr_registry = _build_addr_registry()
    lwp_map = threads_by_lwp()
    out = []
    for t in _inferior().threads():
        tgt = futex_target(t)
        if tgt is None:
            continue
        b = Blocked(t, tgt[0], tgt[1], tgt[2])
        classify(b, addr_registry)
        out.append(b)
    return out, lwp_map


# --------------------------------------------------------------------------
# Cycle detection
# --------------------------------------------------------------------------

def find_cycles(edges):
    """edges: dict waiter_lwp -> owner_lwp.  Return list of cycles (lwp lists)."""
    cycles = []
    seen_global = set()
    for start in list(edges.keys()):
        if start in seen_global:
            continue
        path = []
        on_path = set()
        node = start
        while node in edges:
            if node in on_path:
                # Found a cycle: trim path down to the repeat.
                idx = path.index(node)
                cyc = path[idx:]
                key = frozenset(cyc)
                if key not in [frozenset(c) for c in cycles]:
                    cycles.append(cyc)
                break
            if node in seen_global:
                break
            path.append(node)
            on_path.add(node)
            node = edges[node]
        seen_global.update(path)
    return cycles


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

class DeadlockPrefix(gdb.Command):
    """Deadlock / mutex analysis toolkit.  See `help deadlock`."""

    def __init__(self):
        super().__init__("deadlock", gdb.COMMAND_STACK, prefix=True)

    def invoke(self, arg, from_tty):
        gdb.write(
            "deadlock: subcommands: scan | mutex EXPR | dllock | "
            "waiters EXPR | owner EXPR\n")


class DeadlockScan(gdb.Command):
    """deadlock scan -- build the wait-for graph and report deadlock cycles."""

    def __init__(self):
        super().__init__("deadlock scan", gdb.COMMAND_STACK)

    def invoke(self, arg, from_tty):
        orig = gdb.selected_thread()
        try:
            self._run()
        finally:
            if orig is not None:
                try:
                    orig.switch()
                except gdb.error:
                    pass

    def _run(self):
        if not _is_x86_64():
            gdb.write("deadlock scan: the automatic blocked-thread detector is "
                      "x86-64 only.\nUse `deadlock mutex EXPR` manually on this "
                      "architecture.\n")
            return

        blocked, lwp_map = collect_blocked()
        if not blocked:
            gdb.write("deadlock scan: no threads are blocked in a futex wait.\n")
            return

        gdb.write("== Blocked threads ==\n")
        edges = {}
        for b in blocked:
            t = b.thread
            name = (" \"%s\"" % t.name) if t.name else ""
            if b.kind_label in ("mutex", "dl") and b.mutex is not None:
                owner = b.owner_lwp
                lbl = b.annotation[0] if b.annotation else "mutex"
                if owner:
                    edges[b.lwp] = owner
                    who = _owner_desc(owner, lwp_map)
                    gdb.write("  thread #%d [LWP %d]%s\n"
                              "      waiting on %s @ 0x%x\n"
                              "      held by %s\n"
                              % (t.num, b.lwp, name, lbl, b.uaddr, who))
                else:
                    gdb.write("  thread #%d [LWP %d]%s\n"
                              "      waiting on %s @ 0x%x (no recorded owner)\n"
                              % (t.num, b.lwp, name, lbl, b.uaddr))
            elif b.kind_label == "guard":
                gdb.write("  thread #%d [LWP %d]%s\n"
                          "      waiting on C++ static-init guard @ 0x%x\n"
                          % (t.num, b.lwp, name, b.uaddr))
            else:
                gdb.write("  thread #%d [LWP %d]%s\n"
                          "      waiting in futex @ 0x%x "
                          "(non-mutex: condvar/rwlock/sem -- not graphed)\n"
                          % (t.num, b.lwp, name, b.uaddr))
            if b.annotation:
                nm, desc, hint = b.annotation
                gdb.write("      [%s] %s\n      hint: %s\n" % (nm, desc, hint))

        # Cycle detection over the mutex/dl wait-for edges.
        gdb.write("\n== Wait-for cycles ==\n")
        # Length-1 cycles are self-deadlocks, reported separately below.
        cycles = [c for c in find_cycles(edges) if len(c) > 1]

        # Self-deadlock: waiter owns the very mutex it waits on.
        self_dl = [b for b in blocked
                   if b.is_lock() and b.owner_lwp == b.lwp and b.owner_lwp]
        for b in self_dl:
            recursive = b.mutex and (b.mutex.kind & KIND_TYPE_MASK) == 1
            note = ("recursive mutex re-locked while blocked -- anomalous"
                    if recursive else
                    "SELF-DEADLOCK: non-recursive mutex re-locked by its owner")
            gdb.write("  (!) thread #%d [LWP %d]: %s (mutex @ 0x%x)\n"
                      % (b.thread.num, b.lwp, note, b.uaddr))

        if not cycles and not self_dl:
            gdb.write("  none found. Threads are blocked but the owners are "
                      "making progress (or own no graphed lock).\n")

        for i, cyc in enumerate(cycles, 1):
            gdb.write("  (!) DEADLOCK cycle #%d:\n" % i)
            n = len(cyc)
            for j, lwp in enumerate(cyc):
                nxt = cyc[(j + 1) % n]
                # find the mutex on this edge
                mtx = next((bb.uaddr for bb in blocked
                            if bb.lwp == lwp and bb.owner_lwp == nxt), None)
                t = lwp_map.get(lwp)
                tnum = ("#%d" % t.num) if t else "?"
                mtxs = (" waits on 0x%x held by" % mtx) if mtx else " -> "
                gdb.write("        thread %s [LWP %d]%s LWP %d\n"
                          % (tnum, lwp, mtxs, nxt))
        gdb.write("\n")


class DeadlockMutex(gdb.Command):
    """deadlock mutex EXPR -- decode a pthread_mutex_t (lvalue or address)."""

    def __init__(self):
        super().__init__("deadlock mutex", gdb.COMMAND_DATA)

    def invoke(self, arg, from_tty):
        arg = arg.strip()
        if not arg:
            gdb.write("usage: deadlock mutex EXPR   (a pthread_mutex_t or its address)\n")
            return
        orig = gdb.selected_thread()
        try:
            addr = resolve_mutex_addr(arg)
            mi = MutexInfo(addr)
            gdb.write(mi.describe() + "\n")
            # Known-lock annotation by address.
            for a, name, desc, hint in _build_addr_registry():
                if a == addr:
                    gdb.write("  [%s] %s\n  hint: %s\n" % (name, desc, hint))
                    break
        except gdb.error as e:
            gdb.write("deadlock mutex: %s\n" % e)
        finally:
            if orig is not None:
                try:
                    orig.switch()
                except gdb.error:
                    pass


class DeadlockDlLock(gdb.Command):
    """deadlock dllock -- dump _rtld_global._dl_load_lock and the rtld counter."""

    def __init__(self):
        super().__init__("deadlock dllock", gdb.COMMAND_DATA)

    def invoke(self, arg, from_tty):
        orig = gdb.selected_thread()
        try:
            self._run()
        finally:
            if orig is not None:
                try:
                    orig.switch()
                except gdb.error:
                    pass

    def _run(self):
        try:
            mtx = gdb.parse_and_eval("_rtld_global._dl_load_lock.mutex")
        except gdb.error:
            gdb.write(
                "deadlock dllock: cannot read _rtld_global -- ld.so debuginfo is "
                "not available.\nInstall it (e.g. `dnf debuginfo-install glibc`) "
                "or use `deadlock scan`, which detects a blocked dl_load_lock\n"
                "without any debuginfo.\n")
            return
        try:
            addr = int(mtx.address)
        except Exception:
            gdb.write("deadlock dllock: could not take address of the mutex.\n")
            return

        mi = MutexInfo(addr)
        gdb.write("_rtld_global._dl_load_lock (dynamic-linker load lock)\n")
        gdb.write(mi.describe() + "\n")

        # The rtld recursion counter.  Old glibc kept a separate .count/.owner
        # in __rtld_lock_recursive_t; newer glibc (>= ~2.34) uses a plain
        # recursive pthread_mutex_t, so the depth is mutex.__data.__count.
        rtld_count = None
        try:
            rtld_count = int(gdb.parse_and_eval("_rtld_global._dl_load_lock.count"))
            gdb.write("  rtld .count = %d  (separate rtld recursion counter, "
                      "old-glibc layout)\n" % rtld_count)
        except gdb.error:
            gdb.write("  rtld .count = n/a on this glibc -- the recursion depth "
                      "is the recursive mutex __count (= %d above).\n" % mi.count)

        # Effective recursion depth, whichever layout applies.
        depth = rtld_count if rtld_count is not None else mi.count

        recursive = (mi.kind & KIND_TYPE_MASK) == 1
        gdb.write("\n  interpretation:\n")
        if mi.eff_owner == 0 and depth > 0:
            gdb.write("    (!) LEAK SHAPE: recursion depth %d but __owner == 0 "
                      "-- a recursive acquisition was leaked (unbalanced\n"
                      "        lock/unlock, or the owner exited mid-dlopen). No "
                      "thread will ever release it.\n" % depth)
        elif mi.eff_owner:
            gdb.write("    held by %s (recursion depth %d)\n"
                      % (_owner_desc(mi.eff_owner), depth))
            gdb.write("    -> if that thread is itself blocked, run `deadlock "
                      "scan`: something it needs is held by a thread that in\n"
                      "        turn wants the dynamic linker (dlopen re-entrancy "
                      "from a constructor / atfork / signal handler).\n")
        else:
            gdb.write("    lock looks free (owner 0, depth 0).\n")
        if not recursive:
            gdb.write("    NOTE: __kind is not recursive (%d); unusual for the "
                      "dl_load_lock.\n" % mi.kind)


class DeadlockWaiters(gdb.Command):
    """deadlock waiters EXPR -- list threads blocked on this mutex."""

    def __init__(self):
        super().__init__("deadlock waiters", gdb.COMMAND_STACK)

    def invoke(self, arg, from_tty):
        arg = arg.strip()
        if not arg:
            gdb.write("usage: deadlock waiters EXPR\n")
            return
        orig = gdb.selected_thread()
        try:
            addr = resolve_mutex_addr(arg)
            blocked, lwp_map = collect_blocked()
            hits = [b for b in blocked if b.uaddr == addr]
            if not hits:
                gdb.write("no threads are blocked on 0x%x\n" % addr)
                return
            gdb.write("threads blocked on mutex 0x%x:\n" % addr)
            for b in hits:
                t = b.thread
                name = (" \"%s\"" % t.name) if t.name else ""
                gdb.write("  thread #%d [LWP %d]%s @ %s\n"
                          % (t.num, b.lwp, name, one_line_frame(t)))
        except gdb.error as e:
            gdb.write("deadlock waiters: %s\n" % e)
        finally:
            if orig is not None:
                try:
                    orig.switch()
                except gdb.error:
                    pass


class DeadlockOwner(gdb.Command):
    """deadlock owner EXPR -- describe (and select) the mutex's owning thread."""

    def __init__(self):
        super().__init__("deadlock owner", gdb.COMMAND_STACK)

    def invoke(self, arg, from_tty):
        arg = arg.strip()
        if not arg:
            gdb.write("usage: deadlock owner EXPR\n")
            return
        try:
            addr = resolve_mutex_addr(arg)
            mi = MutexInfo(addr)
        except gdb.error as e:
            gdb.write("deadlock owner: %s\n" % e)
            return
        if mi.eff_owner == 0:
            gdb.write("mutex 0x%x has no recorded owner "
                      "(unlocked, or a non-owner-tracking lock).\n" % addr)
            return
        lwp_map = threads_by_lwp()
        t = lwp_map.get(mi.eff_owner)
        gdb.write("mutex 0x%x held by %s\n" % (addr, _owner_desc(mi.eff_owner, lwp_map)))
        if t is not None:
            t.switch()
            gdb.write("switched to thread #%d.\n" % t.num)


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

def register_deadlock_commands(objfile=None):
    """Register the `deadlock` command family. Idempotent.

    objfile is accepted for signature-parity with the printer registrars and
    is ignored (gdb commands are global, not per-objfile)."""
    DeadlockPrefix()
    DeadlockScan()
    DeadlockMutex()
    DeadlockDlLock()
    DeadlockWaiters()
    DeadlockOwner()


# Registering on import keeps `source deadlock.py` working interactively; the
# packaged .gdbinit calls register_deadlock_commands() explicitly.
register_deadlock_commands()
