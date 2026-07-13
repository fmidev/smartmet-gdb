"""
fmiprinters -- render C++ objects by calling a stringification method.

This is a small, *generic* gdb pretty-printer framework: for a registered C++
type it calls a configured member method (e.g. `to_string()`, `ToStr()`,
`c_str()`, `to_iso_extended_string()`) and displays the returned string as the
value. It is NOT limited to FMI types, and NOT limited to time/ISO strings --
those are just what the shipped defaults happen to be. Any class with a method
that returns a string (or a std::string) can be handled by adding a rule.

Registration is explicit, by design. The renderer *calls a function in the
inferior*, so:

  * It requires a LIVE process (`gdb -p PID` or a running program). Function
    calls cannot be evaluated against a core dump, so these printers do not
    fire on core files -- gdb falls back to the default structure dump there.
  * Auto-detecting a stringification method on every value would be slow and
    fragile, so instead you opt specific types in via SPECIALIZED_PRINTERS.

Each rule (a value in SPECIALIZED_PRINTERS) has:

  method              member function to call, e.g. "to_string", "ToStr"
  args                literal/constant args including parentheses, or ""
                      (e.g. "", "(126)", "(true, 3)"); "" means "()"
  ret_is_std_string   if True, append ".c_str()" so gdb can read a C string
                      out of the returned std::string

The renderer tries several strategies so it copes with methods that return
either a std::string or a plain `const char*`:

  A. call `<expr>.c_str()` directly (when ret_is_std_string)
  B. bind the result to `const std::string&` via a GNU statement-expression
     and take `.c_str()`
  C. `print/s <expr>` and parse the quoted string out of gdb's output

Usage from gdb (done for you by /etc/gdbinit.d/smartmet-gdb.gdb):

    python
    from fmiprinters import register_fmi_printers
    register_fmi_printers(None)
    end

Add your own type at runtime:

    (gdb) python import fmiprinters
    (gdb) python fmiprinters.add_specialized_printer("My::Class", "to_string")
    (gdb) python fmiprinters.register_fmi_printers(None)   # re-register

Inspect what is registered:

    (gdb) python import fmiprinters; fmiprinters.show_registered_types()
"""

import gdb
import gdb.printing
import re

# --- Registry ---------------------------------------------------------------
# Map fully-qualified class names (as they appear in debug info) to how to call
# the stringification method. These are the "specialized" cases: classes whose
# conversion method has a non-obvious name and/or takes constant arguments.
# The list is NOT limited to time types or ISO strings -- add any class whose
# method returns a string. See the module docstring for the field meanings.

SPECIALIZED_PRINTERS = {
    "Fmi::date_time::DateTime":      {"method": "to_iso_extended_string", "args": "",   "ret_is_std_string": True},
    "Fmi::date_time::Date":          {"method": "to_iso_extended_string", "args": "",   "ret_is_std_string": True},
    "Fmi::date_time::TimeDuration":  {"method": "to_iso_extended_string", "args": "",   "ret_is_std_string": True},
    "TextGenPosixTime":              {"method": "ToIsoExtendedStr", "args": "",         "ret_is_std_string": True},
}

def add_specialized_printer(type_name, method, args="", ret_is_std_string=True):
    """Register/override a rule at runtime from gdb."""
    SPECIALIZED_PRINTERS[type_name] = {
        "method": method,
        "args": args,
        "ret_is_std_string": bool(ret_is_std_string),
    }

# Build a regex that matches exactly any of the registered type names, while
# tolerating extra leading namespaces some compilers may bake in.
def _build_type_regex():
    # e.g. ^(?:.*::)?(?:Fmi::date_time::Date|TextGenPosixTime|... )$
    alts = "|".join(map(re.escape, SPECIALIZED_PRINTERS.keys()))
    return r"^(?:.*::)?(?:" + alts + r")$"

_TYPE_RX = None
def _refresh_type_regex():
    global _TYPE_RX
    _TYPE_RX = re.compile(_build_type_regex())

_refresh_type_regex()

# --- Pretty-printer ---------------------------------------------------------

class _SpecializedPrinter:
    """Printer that calls a configured member method (with optional constant args)."""

    def __init__(self, val):
        self.val = val

    def _peel(self):
        v = self.val
        t = v.type.strip_typedefs()
        if t.code == gdb.TYPE_CODE_REF:
            v = v.referenced_value()
            t = v.type.strip_typedefs()
        return v, t

    def to_string(self):
        try:
            v, t = self._peel()
            tag = (t.tag or t.name or "").strip()

            # Debug: uncomment to diagnose matching issues
            # gdb.write("[DEBUG] Checking type: '" + tag + "'\n")

            if not tag or _TYPE_RX is None or not _TYPE_RX.match(tag):
                return None

            # Find matching rule (exact or suffix)
            rule = SPECIALIZED_PRINTERS.get(tag)
            if rule is None:
                parts = tag.split("::")
                for i in range(len(parts)):
                    cand = "::".join(parts[i:])
                    if cand in SPECIALIZED_PRINTERS:
                        rule = SPECIALIZED_PRINTERS[cand]
                        break
            if rule is None:
                return "<fmiprinters: no rule>"

            addr = v.address
            if addr is None:
                return "<fmiprinters: value has no address; cannot call method>"

            method = rule["method"]
            args = rule.get("args", "")
            # Ensure args includes parentheses for method call
            if not args:
                args = "()"

            # Use the hex pointer form directly (e.g., 0x7ffe...) to avoid int() issues
            base = "((const " + tag + "*)" + str(addr) + ")->" + method + args

            # --- Strategy A: direct call returning const char* from std::string ---
            if rule.get("ret_is_std_string", True):
                try:
                    expr = base + ".c_str()"
                    c1 = gdb.parse_and_eval(expr)
                    return c1.string()
                except gdb.error:
                    pass  # fall through

            # --- Strategy B: bind temporary to const std::string& and return c_str() via
            # a GNU statement-expression so the block yields a value.
            try:
                wrapped = (
                    "({"
                    "  const std::string& __s = (" + base + ");"
                    "  (__s).c_str();"
                    "})"
                )
                c2 = gdb.parse_and_eval(wrapped)
                return c2.string()
            except gdb.error:
                pass  # fall through

            # --- Strategy C: try explicit cast, then parse print/s output ---
            try:
                # Try direct c_str() call one more time with explicit casting
                try:
                    expr = "(const char*)(" + base + ").c_str()"
                    c3 = gdb.parse_and_eval(expr)
                    return c3.string()
                except gdb.error:
                    pass

                # Last resort: use gdb.execute to print and parse output
                # Use /s format to ensure string output
                cmd = "print/s " + base
                out = gdb.execute(cmd, to_string=True).strip()

                # Try to extract quoted "text" (allow escaped chars)
                # Matches: $1 = "some \"text\""  (captures inner, unescaped later)
                m = re.search(r'=\s*"((?:\\.|[^"])*)"', out)
                if m:
                    s = m.group(1)
                    # Unescape common sequences as gdb shows them
                    s = s.replace('\\\"', '"').replace('\\\\', '\\')
                    return s

                # Try without quotes in case it's a plain string
                m2 = re.search(r'=\s*(.+?)\s*$', out)
                if m2:
                    return m2.group(1)

                # Fallback: return the raw line
                return out
            except gdb.error as e:
                return "<fmiprinters: gdb error: " + str(e) + ">"

        except gdb.error as e:
            return "<fmiprinters: gdb error: " + str(e) + ">"
        except Exception as e:
            return "<fmiprinters: unexpected error: " + str(e) + ">"

def _lookup(val):
    # Quick reject via type name regex
    t = val.type
    if t.code == gdb.TYPE_CODE_REF:
        t = t.target()
    t = t.strip_typedefs()
    tag = (t.tag or t.name or "").strip()
    if tag and _TYPE_RX and _TYPE_RX.match(tag):
        return _SpecializedPrinter(val)
    return None

def register_fmi_printers(objfile=None):
    """Register the pretty-printers for the current (or given) objfile."""
    _refresh_type_regex()  # pick up any runtime additions
    pcoll = gdb.printing.RegexpCollectionPrettyPrinter("fmiprinters")
    pcoll.add_printer("specialized", _build_type_regex(), _SpecializedPrinter)
    gdb.printing.register_pretty_printer(objfile or gdb.current_objfile(), pcoll)

def show_registered_types():
    """Debug helper: show all registered types and current regex."""
    print("Registered specialized printers:")
    for type_name, rule in SPECIALIZED_PRINTERS.items():
        print("  " + type_name + ": " + rule['method'] + (rule.get('args') or '()'))
    if _TYPE_RX:
        print("\nRegex pattern: " + _TYPE_RX.pattern)
    else:
        print("\nRegex pattern: None")

# Auto-register when sourced
# register_fmi_printers()
