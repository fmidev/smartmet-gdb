import gdb
import gdb.printing
import re

# --- Registry ---------------------------------------------------------------
# Map fully-qualified class names (as they appear in debug info) to
# how to call the render method.
#
# Fields:
#   method: member function to call (e.g. "to_iso_string", "ToStr")
#   args:   string of literal/constant args, including parentheses; can be ""
#           e.g. "", "(126)", "(true, 3)"
#   ret_is_std_string: if True, append ".c_str()" so GDB can read a C string
#
# Add more with add_iso_printer(...) at runtime.

ISO_PRINTERS = {
    "Fmi::date_time::DateTime":      {"method": "to_iso_extended_string", "args": "",   "ret_is_std_string": True},
    "Fmi::date_time::Date":          {"method": "to_iso_extended_string", "args": "",   "ret_is_std_string": True},
    "Fmi::date_time::TimeDuration":  {"method": "to_iso_extended_string", "args": "",   "ret_is_std_string": True},
    "TextGenPosixTime":              {"method": "ToIsoExtendedStr", "args": "",         "ret_is_std_string": True},
}

def add_iso_printer(type_name, method, args="", ret_is_std_string=True):
    """Register/override a rule at runtime from GDB."""
    ISO_PRINTERS[type_name] = {
        "method": method,
        "args": args,
        "ret_is_std_string": bool(ret_is_std_string),
    }

# Build a regex that matches exactly any of the registered type names, while
# tolerating extra leading namespaces some compilers may bake in.
def _build_type_regex():
    # e.g. ^(?:.*::)?(?:Fmi::date_time::Date|TextGenPosixTime|... )$
    alts = "|".join(map(re.escape, ISO_PRINTERS.keys()))
    return r"^(?:.*::)?(?:" + alts + r")$"

_TYPE_RX = None
def _refresh_type_regex():
    global _TYPE_RX
    _TYPE_RX = re.compile(_build_type_regex())

_refresh_type_regex()

# --- Pretty-printer ---------------------------------------------------------

class _IsoMappedPrinter:
    """Generic printer that calls a configured member method (with optional constant args)."""

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
            rule = ISO_PRINTERS.get(tag)
            if rule is None:
                parts = tag.split("::")
                for i in range(len(parts)):
                    cand = "::".join(parts[i:])
                    if cand in ISO_PRINTERS:
                        rule = ISO_PRINTERS[cand]
                        break
            if rule is None:
                return "<iso-printer: no rule>"

            addr = v.address
            if addr is None:
                return "<iso-printer: value has no address; cannot call method>"

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
                return "<iso-printer: gdb error: " + str(e) + ">"

        except gdb.error as e:
            return "<iso-printer: gdb error: " + str(e) + ">"
        except Exception as e:
            return "<iso-printer: unexpected error: " + str(e) + ">"

def _lookup(val):
    # Quick reject via type name regex
    t = val.type
    if t.code == gdb.TYPE_CODE_REF:
        t = t.target()
    t = t.strip_typedefs()
    tag = (t.tag or t.name or "").strip()
    if tag and _TYPE_RX and _TYPE_RX.match(tag):
        return _IsoMappedPrinter(val)
    return None

def register_fmi_printers(objfile=None):
    """Register the pretty-printers for the current (or given) objfile."""
    _refresh_type_regex()  # pick up any runtime additions
    pcoll = gdb.printing.RegexpCollectionPrettyPrinter("fmiprinters")
    pcoll.add_printer("iso-mapped", _build_type_regex(), _IsoMappedPrinter)
    gdb.printing.register_pretty_printer(objfile or gdb.current_objfile(), pcoll)

def show_registered_types():
    """Debug helper: show all registered types and current regex."""
    print("Registered ISO printers:")
    for type_name, rule in ISO_PRINTERS.items():
        print("  " + type_name + ": " + rule['method'] + (rule.get('args') or '()'))
    if _TYPE_RX:
        print("\nRegex pattern: " + _TYPE_RX.pattern)
    else:
        print("\nRegex pattern: None")

# Auto-register when sourced
# register_fmi_printers()
