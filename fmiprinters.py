"""
fmiprinters -- render C++ objects by calling a stringification method.

This is a small, *generic* gdb pretty-printer framework: for a C++ object it
calls a member method (e.g. `to_string()`, `ToStr()`, `c_str()`) and displays
the returned string as the value. It is NOT limited to FMI types, and NOT
limited to time/ISO strings -- those are just what the shipped rules cover.

Two mechanisms:

1. SPECIALIZED_PRINTERS -- an explicit registry for the "specialized" cases:
   classes whose conversion method has a non-obvious name and/or takes constant
   arguments (e.g. `TextGenPosixTime::ToIsoExtendedStr()`). Matched by
   fully-qualified type name.

2. Automatic detection (AUTO_DETECT, on by default) -- for any *unregistered*
   class, fmiprinters looks for a conventional stringification method
   (`to_string`, `toString`, `str`, `ToStr`, `c_str`) and, if the class has
   one, calls it. Whether a type has such a method is detected WITHOUT calling
   anything (by trying to take the method's address) and cached per type, so
   the probe runs once per type, not per value. Types in library namespaces
   (std::, boost::, __gnu_cxx::, ...) are skipped so their own printers win.

Rendering *calls a function in the inferior*, so both mechanisms require a
LIVE process (`gdb -p PID` or a running program). Function calls cannot be
evaluated against a core dump, so these printers do not fire on core files --
gdb shows the default structure dump there. (The specialized printers report
an error string on a core; the automatic ones stay silent and fall back.)

Each SPECIALIZED_PRINTERS rule has:

  method              member function to call, e.g. "to_string", "ToStr"
  args                literal/constant args including parentheses, or ""
                      (e.g. "", "(126)", "(true, 3)"); "" means "()"
  ret_is_std_string   True  -> method returns std::string (read via .c_str())
                      False -> method returns const char* (read directly)

Usage from gdb (done for you by /etc/gdbinit.d/smartmet-gdb.gdb):

    python
    from fmiprinters import register_fmi_printers
    register_fmi_printers(None)
    end

Toggle / configure at runtime:

    (gdb) set fmi-auto-tostring off          # disable automatic detection
    (gdb) python import fmiprinters
    (gdb) python fmiprinters.add_specialized_printer("My::Class", "to_string")
    (gdb) python fmiprinters.register_fmi_printers(None)
    (gdb) python fmiprinters.show_registered_types()
"""

import gdb
import gdb.printing
import re

# --- Specialized registry ---------------------------------------------------
# Classes whose conversion method has a non-obvious name and/or takes constant
# arguments. NOT limited to time types or ISO strings -- add any class here.

SPECIALIZED_PRINTERS = {
    "Fmi::date_time::DateTime":      {"method": "to_iso_extended_string", "args": "",   "ret_is_std_string": True},
    "Fmi::date_time::Date":          {"method": "to_iso_extended_string", "args": "",   "ret_is_std_string": True},
    "Fmi::date_time::TimeDuration":  {"method": "to_iso_extended_string", "args": "",   "ret_is_std_string": True},
    "TextGenPosixTime":              {"method": "ToIsoExtendedStr", "args": "",         "ret_is_std_string": True},
}

def add_specialized_printer(type_name, method, args="", ret_is_std_string=True):
    """Register/override a specialized rule at runtime from gdb."""
    SPECIALIZED_PRINTERS[type_name] = {
        "method": method,
        "args": args,
        "ret_is_std_string": bool(ret_is_std_string),
    }
    _refresh_type_regex()

# --- Automatic detection ----------------------------------------------------
# Conventional stringification methods tried, in order, on unregistered
# classes. (method, ret_is_std_string). c_str() returns const char*, the rest
# are assumed to return std::string.
AUTO_METHODS = (
    ("to_string", True),
    ("toString", True),
    ("str",       True),
    ("ToStr",     True),
    ("c_str",     False),
)

# On by default; flip with `set fmi-auto-tostring off` or this global.
AUTO_DETECT = True

# Type-name prefixes whose own pretty-printers should win; never auto-probe.
AUTO_EXCLUDE_PREFIXES = ("std::", "__", "boost::", "google::", "absl::",
                         "fmt::", "Poco::", "QString", "Qt")

# Per-type cache: tag -> rule dict (has a method) or None (does not).
_AUTO_CACHE = {}

# --- Type-name regex for the specialized set --------------------------------

def _build_type_regex():
    if not SPECIALIZED_PRINTERS:
        return r"(?!)"  # matches nothing
    alts = "|".join(map(re.escape, SPECIALIZED_PRINTERS.keys()))
    return r"^(?:.*::)?(?:" + alts + r")$"

_TYPE_RX = None
def _refresh_type_regex():
    global _TYPE_RX
    _TYPE_RX = re.compile(_build_type_regex())

_refresh_type_regex()

# --- Helpers ----------------------------------------------------------------

def _peel(val):
    """Follow a reference to the referred value; return (value, type)."""
    v = val
    t = v.type.strip_typedefs()
    if t.code == gdb.TYPE_CODE_REF:
        v = v.referenced_value()
        t = v.type.strip_typedefs()
    return v, t

# Type codes that mean "this resolved to a member function", not a data member.
_METHOD_CODES = tuple(
    c for c in (getattr(gdb, "TYPE_CODE_METHOD", None),
                getattr(gdb, "TYPE_CODE_FUNC", None),
                getattr(gdb, "TYPE_CODE_METHODPTR", None))
    if c is not None)

def _method_exists(obj_expr, method):
    """True if `method` is a member FUNCTION of the object, WITHOUT calling it.

    Accessing `obj.method` (no parentheses) either resolves to a method-typed
    value (gdb returns a TYPE_CODE_METHOD/FUNC/METHODPTR value) or raises
    "Cannot take address of method ..." -- both mean the method exists. A
    missing member raises "There is no member ...". A resolvable *data* member
    is not a function and is ignored. The exact behaviour varies by expression
    form and gdb version, so both signals are handled."""
    try:
        v = gdb.parse_and_eval("%s.%s" % (obj_expr, method))
        return v.type.strip_typedefs().code in _METHOD_CODES
    except gdb.error as e:
        return "address of method" in str(e).lower()

def _auto_rule(tag, val, obj_expr):
    """Return a *working* auto rule for type `tag`, or None. Cached per type.

    A candidate is accepted only if it (a) exists as a member function and
    (b) actually renders to a non-empty string on the sample value -- so a
    method that cannot be called (inlined, or on a core dump) is rejected and
    the type falls back to gdb's default dump instead of printing empty."""
    if tag in _AUTO_CACHE:
        return _AUTO_CACHE[tag]
    rule = None
    for method, ret_is_std_string in AUTO_METHODS:
        if not _method_exists(obj_expr, method):
            continue
        cand = {"method": method, "args": "", "ret_is_std_string": ret_is_std_string}
        if _render(val, cand, quiet=True):   # non-empty string == usable
            rule = cand
            break
    _AUTO_CACHE[tag] = rule
    return rule

def _specialized_rule(tag):
    """Look up a specialized rule by exact or namespace-suffix match."""
    rule = SPECIALIZED_PRINTERS.get(tag)
    if rule is not None:
        return rule
    parts = tag.split("::")
    for i in range(len(parts)):
        cand = "::".join(parts[i:])
        if cand in SPECIALIZED_PRINTERS:
            return SPECIALIZED_PRINTERS[cand]
    return None

def _render(val, rule, quiet):
    """Call rule['method'] on val and return the resulting string.

    On failure return None when quiet (automatic path -> fall back to the
    default dump), else a diagnostic string (explicit registry path)."""
    def fail(msg):
        return None if quiet else "<fmiprinters: " + msg + ">"
    try:
        v, t = _peel(val)
        tag = (t.tag or t.name or "").strip()
        if not tag:
            return fail("no type tag")
        addr = v.address
        if addr is None:
            return fail("value has no address; cannot call method")

        method = rule["method"]
        args = rule.get("args", "") or "()"
        base = "((const " + tag + "*)" + str(addr) + ")->" + method + args

        # --- Strategy A: read the returned string directly ---
        try:
            if rule.get("ret_is_std_string", True):
                return gdb.parse_and_eval(base + ".c_str()").string()
            # method returns const char* (or similar) -> read directly
            return gdb.parse_and_eval(base).string()
        except gdb.error:
            pass

        # --- Strategy B: bind to const std::string& via a statement-expr ---
        try:
            wrapped = ("({ const std::string& __s = (" + base + "); (__s).c_str(); })")
            return gdb.parse_and_eval(wrapped).string()
        except gdb.error:
            pass

        # --- Strategy C: print/s and parse gdb's output ---
        try:
            try:
                return gdb.parse_and_eval("(const char*)(" + base + ").c_str()").string()
            except gdb.error:
                pass
            out = gdb.execute("print/s " + base, to_string=True).strip()
            m = re.search(r'=\s*"((?:\\.|[^"])*)"', out)
            if m:
                return m.group(1).replace('\\\"', '"').replace('\\\\', '\\')
            m2 = re.search(r'=\s*(.+?)\s*$', out)
            if m2:
                return m2.group(1)
            return fail("could not parse output")
        except gdb.error as e:
            return fail("gdb error: " + str(e))

    except gdb.error as e:
        return fail("gdb error: " + str(e))
    except Exception as e:
        return fail("unexpected error: " + str(e))

# --- Printer + lookup -------------------------------------------------------

class _Printer:
    def __init__(self, val, rule, quiet):
        self.val = val
        self.rule = rule
        self.quiet = quiet

    def to_string(self):
        return _render(self.val, self.rule, self.quiet)

def _is_excluded(tag):
    return any(tag.startswith(p) for p in AUTO_EXCLUDE_PREFIXES)

def _lookup(val):
    """Consulted for every value. Specialized registry first, then auto."""
    try:
        t = val.type
        if t.code == gdb.TYPE_CODE_REF:
            t = t.target()
        t = t.strip_typedefs()
        tag = (t.tag or t.name or "").strip()
        if not tag:
            return None

        # 1. explicit registry (exact / namespace-suffix)
        if _TYPE_RX and _TYPE_RX.match(tag):
            rule = _specialized_rule(tag)
            if rule is not None:
                return _Printer(val, rule, quiet=False)

        # 2. automatic detection for unregistered classes
        if not AUTO_DETECT:
            return None
        if t.code not in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
            return None
        if _is_excluded(tag):
            return None
        v, _ = _peel(val)
        addr = v.address
        if addr is None:
            return None
        rule = _auto_rule(tag, val, "(*(const %s*)%s)" % (tag, addr))
        if rule is None:
            return None
        return _Printer(val, rule, quiet=True)
    except gdb.error:
        return None

class _FmiLookup(gdb.printing.PrettyPrinter):
    def __init__(self):
        super(_FmiLookup, self).__init__("fmiprinters")

    def __call__(self, val):
        return _lookup(val)

def register_fmi_printers(objfile=None):
    """Register the fmiprinters pretty-printer for the current (or given) objfile."""
    _refresh_type_regex()
    gdb.printing.register_pretty_printer(
        objfile or gdb.current_objfile(), _FmiLookup(), replace=True)

# --- Runtime toggle: `set fmi-auto-tostring on|off` -------------------------

class _AutoToStringParam(gdb.Parameter):
    """set fmi-auto-tostring on|off -- automatic stringification of unknown types."""
    def __init__(self):
        super(_AutoToStringParam, self).__init__(
            "fmi-auto-tostring", gdb.COMMAND_DATA, gdb.PARAM_BOOLEAN)
        self.value = AUTO_DETECT

    def get_set_string(self):
        global AUTO_DETECT
        AUTO_DETECT = bool(self.value)
        return "fmi-auto-tostring is %s" % ("on" if self.value else "off")

    def get_show_string(self, svalue):
        return "automatic to_string detection for unknown types is %s" % svalue

try:
    _AutoToStringParam()
except RuntimeError:
    pass  # already defined (module re-imported)

def show_registered_types():
    """Debug helper: show specialized rules, the auto setting, and the cache."""
    print("Specialized printers:")
    for type_name, rule in SPECIALIZED_PRINTERS.items():
        print("  " + type_name + ": " + rule['method'] + (rule.get('args') or '()'))
    print("\nAutomatic detection: " + ("on" if AUTO_DETECT else "off"))
    print("  candidate methods: " + ", ".join(m for m, _ in AUTO_METHODS))
    if _AUTO_CACHE:
        print("  detected so far:")
        for tag, rule in sorted(_AUTO_CACHE.items()):
            print("    %-40s -> %s" % (tag, (rule['method'] + "()") if rule else "(none)"))

# Auto-register when sourced
# register_fmi_printers()
