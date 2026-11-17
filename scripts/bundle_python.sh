#!/bin/bash
# bundle_python.sh — put a relocatable CPython 3.11 inside the macOS app bundle.
#
# The Swift app at macos/Vantage/Vantage.app used to *name* this checkout's
# `.venv/bin/python` in Contents/Resources/python_path.txt. Copied to another
# Mac, or with the checkout gone, it started nothing: the packages travelled
# with the app but the interpreter did not.
#
# This script closes that gap with python-build-standalone (astral-sh), the
# maintained prebuilt relocatable CPython for macOS. It extracts it to
# Contents/Resources/python/, prunes development-only payload, rewrites the two
# build-time absolute paths of record, precompiles the stdlib (so a signed
# bundle never tries to write __pycache__ into itself), verifies the result by
# importing the modules the backend actually needs, and records
# Contents/Resources/python_path.txt so the app launches from the bundle.
#
# Usage:
#   scripts/bundle_python.sh                 # build into macos/Vantage/Vantage.app
#   scripts/bundle_python.sh --app PATH      # build into another .app
#   scripts/bundle_python.sh --force         # rebuild even if already current
#   scripts/bundle_python.sh --keep-full-stdlib
#                                            # skip pruning (debugging only)
#
# Idempotent: a second run with an intact runtime reports "current" and exits 0.
# The download is cached outside the repository, so a rebuild needs no network.
#
# Not for: iOS, Windows, Linux. arm64 macOS only — the bundle ships arm64
# extension modules (pydantic_core is cpython-311-darwin arm64), and pinning an
# x86_64 interpreter beside them would fail at import.

set -euo pipefail

# --- Pinned upstream release -------------------------------------------------
#
# 20261110 is the newest release at the time of writing. The SHA-256 below was
# downloaded from the URL and cross-checked against the asset `digest` field of
# the GitHub release API, so the pin is not a hash of whatever arrived.
PBS_TAG="20261110"
PBS_PY_VERSION="3.11.18"
PBS_TRIPLE="aarch64-apple-darwin"
PBS_ASSET="cpython-${PBS_PY_VERSION}+${PBS_TAG}-${PBS_TRIPLE}-install_only.tar.gz"
# GitHub percent-encodes '+' in release asset paths.
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/cpython-3.11.18%2B${PBS_TAG}-${PBS_TRIPLE}-install_only.tar.gz"
PBS_SHA256="73f1ed14486aa278d331e95f7d6d452f5cfb69d90b418e26e62fd4846d53557d"

#: Marker file inside Resources/python describing what was installed. Its
#: contents are the idempotency key: same version + same hash means the tree is
#: the one this release of the script produces.
MARKER=".vantage-python-runtime"

#: Directories in the extracted tree that nothing the backend imports can use.
#:
#: * idlelib / turtledemo / lib2to3 / ensurepip — an IDE, two demos, a retired
#:   2to3 library and the pip bootstrapper. None is imported by FastAPI or by
#:   anything under jarvis/.
#: * tkinter + tcl/tk dylibs — the SwiftUI app is the UI; no backend module
#:   imports tkinter (verified with grep over jarvis/, run.py, dashboard/).
#: * pip / setuptools — build tools. The bundle does not install packages at
#:   runtime, and keeping them adds 10 MB plus a `.pth` that mounts the
#:   setuptools distutils shim on every start.
#: * include / share/man / config-3.11-darwin / pkgconfig — headers, man pages
#:   and link metadata, for building extension modules on a development machine.
#: * lib/libpython3.11.dylib — dead weight here, not a linker dependency of
#:   anything in the tree: the interpreter is statically linked
#:   (`--enable-static-libpython-for-interpreter`) and the extension modules use
#:   `-undefined dynamic_lookup`. Verified with `otool -L` over every Mach-O in
#:   the tree, and again by the import check at the end of this script — if a
#:   future package really links against it, this script fails loudly instead of
#:   shipping a runtime that cannot import.
PRUNE_ALWAYS=(
    "lib/python3.11/idlelib"
    "lib/python3.11/turtledemo"
    "lib/python3.11/lib2to3"
    "lib/python3.11/ensurepip"
    "lib/python3.11/tkinter"
    "lib/python3.11/lib-dynload/_tkinter.cpython-311-darwin.so"
    "lib/python3.11/config-3.11-darwin"
    "lib/python3.11/site-packages"
    "lib/tcl9"
    "lib/tcl9.0"
    "lib/tk9.0"
    "lib/itcl4.3.8"
    "lib/thread3.0.6"
    "lib/libtcl9.0.dylib"
    "lib/libtcl9tk9.0.dylib"
    "lib/pkgconfig"
    "lib/libpython3.11.dylib"
    "include"
    "share"
    "bin/2to3"
    "bin/2to3-3.11"
    "bin/idle3"
    "bin/idle3.11"
    "bin/pip"
    "bin/pip3"
    "bin/pip3.11"
    "bin/pydoc3"
    "bin/pydoc3.11"
    "bin/python3-config"
)

# --- Arguments ---------------------------------------------------------------
APP=""
FORCE=0
KEEP_FULL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --app)             APP="${2:-}"; [ -n "$APP" ] || { echo "bundle_python: --app needs a path" >&2; exit 2; }; shift 2 ;;
        --force)           FORCE=1; shift ;;
        --keep-full-stdlib) KEEP_FULL=1; shift ;;
        -h|--help)         awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
        *)                 echo "bundle_python: unknown argument '$1' (try --help)" >&2; exit 2 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP="${APP:-${REPO_DIR}/macos/Vantage/Vantage.app}"

die() { echo "bundle_python: $*" >&2; exit 1; }

# --- Host checks -------------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || die "this script builds a macOS app runtime"
[ "$(uname -m)" = "arm64" ] || die "the bundle's extension modules are arm64; this host is $(uname -m)"
[ -d "${APP}/Contents/Resources" ] || \
    die "no app bundle at ${APP} — run macos/Vantage/assemble_app.sh first"

RESOURCES="$(cd "${APP}/Contents/Resources" && pwd)"
RUNTIME="${RESOURCES}/python"
STAGED_PACKAGES="${RESOURCES}/site-packages"

echo "==> App bundle:  ${APP}"
echo "==> Runtime:     ${RUNTIME}"

# --- Cache the download outside the repository -------------------------------
# 27 MB does not belong in a git tree, and the .app is gitignored anyway.
CACHE="${XDG_CACHE_HOME:-${HOME}/.cache}/vantage/python-build-standalone"
TARBALL="${CACHE}/${PBS_ASSET}"
mkdir -p "${CACHE}"

sha256_of() { shasum -a 256 "$1" | awk '{print $1}'; }

fetch_tarball() {
    if [ -f "${TARBALL}" ] && [ "$(sha256_of "${TARBALL}")" = "${PBS_SHA256}" ]; then
        echo "    using cached ${TARBALL}"
        return 0
    fi
    rm -f "${TARBALL}"
    echo "    downloading ${PBS_URL}"
    # The connection to the release CDN was observed resetting mid-transfer, so
    # retry rather than fail the build on a transient network fault.
    local attempt
    for attempt in 1 2 3 4 5; do
        if curl -sSfL --retry 3 --retry-delay 2 --connect-timeout 20 \
                    --max-time 600 -o "${TARBALL}.part" "${PBS_URL}"; then
            mv "${TARBALL}.part" "${TARBALL}"
            return 0
        fi
        echo "    attempt ${attempt} failed; retrying"
        sleep $((attempt * 2))
    done
    rm -f "${TARBALL}.part"
    die "could not download ${PBS_URL} (5 attempts)"
}

verify_tarball() {
    local got
    got="$(sha256_of "${TARBALL}")"
    [ "${got}" = "${PBS_SHA256}" ] || \
        die "sha256 mismatch for ${PBS_ASSET}: got ${got}, expected ${PBS_SHA256}"
    echo "    sha256 ok: ${PBS_SHA256}"
}

# --- Is the existing runtime already the one we ship? ------------------------
runtime_is_current() {
    [ "${FORCE}" = "0" ] || return 1
    [ -f "${RUNTIME}/${MARKER}" ] || return 1
    [ -x "${RUNTIME}/bin/python3.11" ] || return 1
    grep -qx "version=${PBS_PY_VERSION}" "${RUNTIME}/${MARKER}" || return 1
    grep -qx "sha256=${PBS_SHA256}" "${RUNTIME}/${MARKER}" || return 1
    grep -qx "full_stdlib=${KEEP_FULL}" "${RUNTIME}/${MARKER}" || return 1
    return 0
}

verify_runtime() {
    # The acceptance check. Run from a neutral directory with only the bundle's
    # own site-packages on PYTHONPATH: this proves the bundle's interpreter
    # imports the bundle's packages, and prints the two values that show it is
    # not leaning on a host interpreter. `-I` is deliberately NOT used for this
    # one — isolated mode ignores PYTHONPATH, which is the mechanism under test.
    local py="${RUNTIME}/bin/python3.11"
    local pypath="${RESOURCES}"
    [ -d "${STAGED_PACKAGES}" ] && pypath="${STAGED_PACKAGES}"

    echo "==> Verifying ${py}"
    ( cd "${CACHE}" && PYTHONPATH="${pypath}" PYTHONNOUSERSITE=1 "${py}" -c "
import sys
import fastapi, pydantic_core, ssl, sqlite3
print('ok')
print('sys.executable:', sys.executable)
print('sys.prefix:    ', sys.prefix)
print('python:        ', sys.version.split()[0])
print('fastapi:       ', fastapi.__version__)
print('pydantic_core: ', pydantic_core.__version__)
print('openssl:       ', ssl.OPENSSL_VERSION)
print('sqlite:        ', sqlite3.sqlite_version)
ctx = ssl.create_default_context()
assert ctx.cert_store_stats()['x509_ca'] > 0, 'no CA certificates loaded'
con = sqlite3.connect(':memory:')
assert con.execute('select 1').fetchone()[0] == 1
con.close()
print('ssl+sqlite:     functional')
" ) || die "the bundled interpreter failed its import check (see traceback above)"
}

check_no_build_paths() {
    # A relocatable runtime must not name the machine that built it. The
    # checkout path is the one that matters most — it is what made the previous
    # bundle only work where this repository happened to sit.
    #
    # Patterns are anchored to the forms CPython actually writes (`"BINDIR":
    # "/install/bin",`, `--prefix=/install`) rather than a bare `/install/`:
    # _aix_support.py carries a documentation URL containing `/install/binary_
    # compatability.html`, which is not a path this runtime resolves.
    echo "==> Checking for build-machine paths"
    local -a patterns=(
        "${REPO_DIR}"
        "/tmp/tmpa34w8s9s"
        "--prefix=/install"
        "\"/install/bin\""
        "\"/install/lib\""
        "\"/install/lib/python3.11\""
        "\"/install/include"
        "\"/install\""
        "'/install'"
    )
    local pattern hits found=0
    for pattern in "${patterns[@]}"; do
        hits="$(grep -rIlF --binary-files=without-match "${pattern}" "${RUNTIME}" 2>/dev/null || true)"
        if [ -n "${hits}" ]; then
            echo "${hits}" | sed "s|^|    ! ${pattern} -> |" >&2
            found=1
        fi
    done
    # Byte-compiled stdlib: file paths are embedded relative (`./lib/...`), but
    # the freshness hash of a .pyc is over the source it was built from, so a
    # path can only appear if precompilation ran without stripdir.
    if grep -rIlF --binary-files=text "${REPO_DIR}" "${RUNTIME}" 2>/dev/null | grep -q .; then
        echo "    ! the checkout path appears inside a byte-compiled file" >&2
        found=1
    fi
    [ "${found}" = "0" ] || die "absolute build paths remain in the runtime (listed above)"
    echo "    none found in ${RUNTIME}"
}

write_marker() {
    cat > "${RUNTIME}/${MARKER}" <<EOF
# Written by scripts/bundle_python.sh. Changing it by hand makes the next run
# rebuild the runtime. Do not commit: the .app is build output.
version=${PBS_PY_VERSION}
sha256=${PBS_SHA256}
release=${PBS_TAG}
triple=${PBS_TRIPLE}
url=${PBS_URL}
full_stdlib=${KEEP_FULL}
EOF
}

record_interpreter() {
    # python_path.txt is how BackendManager.backendInterpreter() finds the
    # interpreter inside a bundle. It must be the bundle-relative runtime, not
    # the build machine's .venv.
    printf '%s' "${RUNTIME}/bin/python3.11" > "${RESOURCES}/python_path.txt"
    echo "    recorded ${RESOURCES}/python_path.txt -> ${RUNTIME}/bin/python3.11"
}

# --- Fast path ---------------------------------------------------------------
if runtime_is_current; then
    echo "==> Runtime already current (${PBS_PY_VERSION}, sha256 ${PBS_SHA256:0:12}…)"
    verify_runtime
    check_no_build_paths
    record_interpreter
    echo "==> Nothing to do."
    echo "    interpreter: ${RUNTIME}/bin/python3.11"
    exit 0
fi

# --- Download ----------------------------------------------------------------
echo "==> CPython ${PBS_PY_VERSION} (python-build-standalone ${PBS_TAG}, ${PBS_TRIPLE})"
fetch_tarball
verify_tarball

# --- Extract into a private staging directory --------------------------------
# Extracted content is untrusted input: it gets its own new directory, and
# nothing below ever runs with that directory as the working directory. The
# interpreter is invoked by absolute path from ${CACHE}.
STAGING="${RUNTIME}.staging.$$"
trap 'rm -rf "${STAGING}"' EXIT
rm -rf "${STAGING}"
mkdir -p "${STAGING}"

echo "==> Extracting"
tar -xzf "${TARBALL}" -C "${STAGING}"
[ -x "${STAGING}/python/bin/python3.11" ] || die "archive did not contain python/bin/python3.11"

NEW="${STAGING}/python"

if [ "${KEEP_FULL}" = "0" ]; then
    echo "==> Pruning development-only payload"
    for entry in "${PRUNE_ALWAYS[@]}"; do
        if [ -e "${NEW}/${entry}" ] || [ -L "${NEW}/${entry}" ]; then
            rm -rf "${NEW:?}/${entry}"
        fi
    done
    # The runtime's own site-packages stays present but empty: sysconfig points
    # purelib at it, and the bundle's packages are loaded from
    # Resources/site-packages through PYTHONPATH (set by the app).
    mkdir -p "${NEW}/lib/python3.11/site-packages"
else
    echo "==> Keeping the full stdlib (--keep-full-stdlib)"
fi

# --- Neutralise the two absolute paths of record -----------------------------
# _sysconfigdata__darwin_darwin.py records the prefix the tree was built under
# (`/install`) and the temporary upstream build root. CPython overrides
# prefix/exec_prefix at runtime from sys.prefix, so these values are only ever
# read by build tooling — but they are absolute paths naming another machine,
# and a relocatable runtime should not carry them. Replace with a marker rather
# than a plausible-looking lie.
SYSCFG="${NEW}/lib/python3.11/_sysconfigdata__darwin_darwin.py"
if [ -f "${SYSCFG}" ]; then
    echo "==> Neutralising recorded build prefix"
    sed -i '' \
        -e 's|/install|@PYTHON_RUNTIME@|g' \
        -e 's|/tmp/tmpa34w8s9s|@UPSTREAM_BUILD@|g' \
        "${SYSCFG}"
fi

# --- Precompile --------------------------------------------------------------
# A signed bundle must not gain __pycache__ directories on first run: writing
# into Contents/Resources invalidates the seal `codesign --verify --strict` just
# produced. Compiling here, with `-I` (no user site, no PYTHONPATH) and paths
# relative to the runtime root so no absolute path lands in a .pyc.
if [ "${KEEP_FULL}" = "0" ]; then
    echo "==> Precompiling the stdlib"
    ( cd "${CACHE}" && PYTHONNOUSERSITE=1 "${NEW}/bin/python3.11" -I -c "
import compileall, pathlib, py_compile, sys
root = pathlib.Path(sys.argv[1]).resolve()
ok = compileall.compile_dir(
    str(root), quiet=1, force=True,
    stripdir=str(root), prependdir='.',
    invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    workers=4, legacy=False)
sys.exit(0 if ok else 1)
" "${NEW}/lib/python3.11" ) || die "stdlib precompilation failed"
fi

# --- Install ----------------------------------------------------------------
# Replace the old runtime only once the new one is built and complete.
echo "==> Installing into ${RUNTIME}"
rm -rf "${RUNTIME}"
mv "${NEW}" "${RUNTIME}"
rmdir "${STAGING}" 2>/dev/null || true
trap - EXIT

write_marker
verify_runtime
check_no_build_paths
record_interpreter

echo "==> Installed"
echo "    interpreter: ${RUNTIME}/bin/python3.11"
echo "    prefix:      ${RUNTIME}  ($(du -sh "${RUNTIME}" | cut -f1))"
echo "    release:     python-build-standalone ${PBS_TAG}, CPython ${PBS_PY_VERSION}, ${PBS_TRIPLE}"
echo "    sha256:      ${PBS_SHA256}"
echo "    marker:      ${RUNTIME}/${MARKER}"