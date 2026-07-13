%define DIRNAME gdb
%define SPECNAME smartmet-gdb
%define pkgdir %{_datadir}/smartmet-gdb

# Boost version to pin the pretty-printers to, clamped to the newest Boost the
# ruediger printers support (1.73). RHEL8 ships Boost 1.69, RHEL9 1.75, RHEL10
# 1.83 -- for 1.75/1.83 we clamp to 1.73 (else zero printers register), for
# 1.69 we pin exactly so no 1.71+-only printers are wrongly enabled.
%if 0%{?rhel} && 0%{?rhel} < 9
%define smartmet_boost_pin (1, 69, 0)
%else
%define smartmet_boost_pin (1, 73, 0)
%endif

Summary: SmartMet gdb pretty-printers and deadlock analysis tools
Name: %{SPECNAME}
Version: 26.7.13
Release: 2%{?dist}.fmi
License: MIT AND BSL-1.0
Group: Development/Tools
URL: https://github.com/fmidev/smartmet-gdb
Source0: %{name}.tar.gz
BuildRoot: %{_tmppath}/%{name}-%{version}-%{release}-root-%(%{__id_u} -n)
BuildArch: noarch

# gdb is used at build time to precompile bytecode with the SAME embedded
# Python interpreter gdb uses at runtime, and at runtime to load the helpers.
BuildRequires: gdb
BuildRequires: make
BuildRequires: rpm-build
Requires: gdb

%description
GDB Python helpers for debugging the SmartMet Server:

  * fmiprinters -- pretty-printers for FMI types exposing to_string-like
    methods (Fmi::date_time, TextGenPosixTime, ...).
  * boost -- the ruediger/Boost-Pretty-Printer package (Boost <= 1.73).
  * deadlock -- a wait-for-graph deadlock analyzer and pthread_mutex_t
    decoder, including the glibc dynamic-linker _dl_load_lock.

The helpers are registered for every gdb session via
%{_sysconfdir}/gdbinit.d/smartmet-gdb.gdb. libstdc++ pretty-printers are NOT
shipped here: gdb auto-loads the version-matched copy from the system
libstdc++ package.

%prep
%setup -q -n %{DIRNAME}

%build
# Pure Python / data: nothing to compile here. Bytecode is generated in
# %%install with gdb's embedded interpreter (see below).

%install
rm -rf $RPM_BUILD_ROOT

# Python modules
mkdir -m 0755 -p $RPM_BUILD_ROOT%{pkgdir}
install -m 0644 fmiprinters.py deadlock.py $RPM_BUILD_ROOT%{pkgdir}/
cp -a boost $RPM_BUILD_ROOT%{pkgdir}/
# Strip any stray bytecode from the source tree; we generate it fresh below.
find $RPM_BUILD_ROOT%{pkgdir} -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# System-wide gdb init drop-in (sourced by /etc/gdbinit's gdbinit.d glob).
mkdir -m 0755 -p $RPM_BUILD_ROOT%{_sysconfdir}/gdbinit.d
install -m 0644 smartmet-gdb.gdb \
    $RPM_BUILD_ROOT%{_sysconfdir}/gdbinit.d/smartmet-gdb.gdb

# Pin the Boost printer version for this build target (no manual editing).
sed -i "s|^SMARTMET_BOOST_VERSION = .*|SMARTMET_BOOST_VERSION = %{smartmet_boost_pin}|" \
    $RPM_BUILD_ROOT%{_sysconfdir}/gdbinit.d/smartmet-gdb.gdb
grep -q "^SMARTMET_BOOST_VERSION = %{smartmet_boost_pin}$" \
    $RPM_BUILD_ROOT%{_sysconfdir}/gdbinit.d/smartmet-gdb.gdb || {
    echo "ERROR: failed to inject Boost version pin" >&2; exit 1; }

# Precompile bytecode using gdb's OWN embedded Python interpreter, so the
# cached .pyc cache tag (cpython-3XY) matches what gdb loads at runtime. Use
# unchecked-hash invalidation so Python treats the .pyc as always valid and
# never tries to rewrite it -- the install dir is read-only for normal users
# and root should not create unpackaged files there.
# UNCHECKED_HASH invalidation needs Python >= 3.7 (gdb on RHEL9/10). On RHEL8
# gdb embeds Python 3.6, so fall back to the default (timestamp) mode there --
# safe because sys.dont_write_bytecode=True in the init prevents runtime writes.
gdb -nx -q -batch -ex "python import compileall; pc=__import__('py_compile'); m=getattr(pc,'PycInvalidationMode',None); kw=({'invalidation_mode':m.UNCHECKED_HASH} if m else {}); compileall.compile_dir('$RPM_BUILD_ROOT%{pkgdir}', quiet=1, ddir='%{pkgdir}', **kw)"

# Fail the build if bytecode was not produced (e.g. gdb without Python).
test -n "$(find $RPM_BUILD_ROOT%{pkgdir} -name '*.pyc' -print -quit)" || {
    echo "ERROR: no .pyc produced -- gdb has no usable Python?" >&2
    exit 1
}

%clean
rm -rf $RPM_BUILD_ROOT

%files
%defattr(-,root,root,-)
%license LICENSE
%doc README.md
%{pkgdir}
%config(noreplace) %{_sysconfdir}/gdbinit.d/smartmet-gdb.gdb

%changelog
* Mon Jul 13 2026 Mika Heiskanen <mika.heiskanen@fmi.fi> - 26.7.13-2.fmi
- Fix build on RHEL8: gdb embeds Python 3.6 there, which lacks
  py_compile.PycInvalidationMode; fall back to the default bytecode
  invalidation mode when unchecked-hash is unavailable.

* Mon Jul 13 2026 Mika Heiskanen <mika.heiskanen@fmi.fi> - 26.7.13-1.fmi
- Initial packaging: fmiprinters, updated Boost-Pretty-Printer, and the new
  deadlock analysis toolkit. Dropped the vendored libstdc++ printers in favour
  of the auto-loaded system copy.
