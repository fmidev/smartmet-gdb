# SmartMet gdb helpers -- FMI + Boost pretty-printers and the deadlock toolkit.
#
# Installed by the smartmet-gdb RPM to /etc/gdbinit.d/, which the distro's
# /etc/gdbinit sources for every gdb invocation and every user. Marked
# %config(noreplace): local edits below are preserved across package upgrades.

set history save on
set print thread-events off
set unwindonsignal on
set print pretty on

python
import sys

# The install dir is not writable by ordinary users and the package already
# ships precompiled bytecode, so never try to write .pyc at runtime.
sys.dont_write_bytecode = True
sys.path.insert(0, '/usr/share/smartmet-gdb')

# --- Boost version -------------------------------------------------------
# Injected at RPM build time from the target distribution (%{?rhel}), clamped
# to the newest Boost the printers support (1.73): RHEL8->1.69, RHEL9/10->1.73.
# Pinned on purpose -- the printers' autodetection COMPILES AND RUNS a tiny C++
# program at startup (needs a compiler + boost headers), which fails on servers
# and is slow. Edit only if your Boost differs from the build target.
SMARTMET_BOOST_VERSION = (1, 73, 0)
# -------------------------------------------------------------------------

def _load(desc, fn):
    try:
        fn()
    except Exception as e:
        sys.stderr.write('smartmet-gdb: %s failed: %s\n' % (desc, e))

def _fmi():
    from fmiprinters import register_fmi_printers
    register_fmi_printers(None)

def _boost():
    import boost
    boost.register_printers(boost_version=SMARTMET_BOOST_VERSION)

def _deadlock():
    from deadlock import register_deadlock_commands
    register_deadlock_commands()

_load('fmi printers', _fmi)
_load('boost printers', _boost)
_load('deadlock toolkit', _deadlock)
end
