#!/usr/bin/env bash
#
# Build and install PJSIP with Python bindings from source.
# Run with --help for detailed usage information.

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
PJSIP_VERSION="${PJSIP_VERSION:-2.16}"
PYTHON="${PYTHON:-python3.14}"
BUILD_DIR="/tmp/pjsip-build"
INSTALL_TARGET=""  # set by --system / --venv / --venv-all

# ── Help ──────────────────────────────────────────────────────────────────────
show_help() {
    cat <<'HELPTEXT'
install_pjsip.sh — Build and install PJSIP with Python (pjsua2) bindings

SYNOPSIS
    ./install_pjsip.sh --system   [OPTIONS]
    ./install_pjsip.sh --venv     [OPTIONS]
    ./install_pjsip.sh --venv-all [OPTIONS]

DESCRIPTION
    pjsua2 is not available on PyPI. It must be compiled from the PJSIP C/C++
    source tree and its SWIG-generated Python wrapper installed manually. This
    script automates the full process:

      1. Download & extract the pjproject source tarball from GitHub
      2. Configure and compile the PJSIP C libraries (libpj, libpjsua2, …)
      3. Install the shared libraries system-wide (sudo make install + ldconfig)
      4. Build the Python SWIG bindings (_pjsua2.*.so + pjsua2.py)
      5. Install the Python package into the chosen target
      6. Verify the import works

INSTALL TARGETS (required — pick one)

    IMPORTANT: --system and --venv control ONLY where the Python bindings
    (pjsua2.py + _pjsua2.*.so) are installed. The PJSIP C shared libraries
    (libpj.so, libpjsua2.so, …) are installed system-wide into
    /usr/local/lib/ via "sudo make install". The exception is --venv-all,
    which installs EVERYTHING into the venv. See "HOW PJSIP INSTALLATION
    WORKS" below for details on the two-layer architecture.

    So the choice is:

        --system / --venv / --venv-all
              │         │         │
              │         │         ├─ Python bindings → $VIRTUAL_ENV/lib/pythonX.Y/site-packages/
              │         │         └─ C libraries     → $VIRTUAL_ENV/lib/
              │         ├─ Python bindings → $VIRTUAL_ENV/lib/pythonX.Y/site-packages/
              │         └─ C libraries     → /usr/local/lib/
              ├─ Python bindings → /usr/lib/pythonX.Y/site-packages/
              └─ C libraries     → /usr/local/lib/

    --system
        Install the Python bindings into the system-wide site-packages of
        the selected Python interpreter via "sudo <python> setup.py install".
        The Python files end up in e.g.:

            /usr/lib/python3.14/site-packages/pjsua2-2.16-….egg/

        This means any Python script using that interpreter can
        "import pjsua2" without a virtualenv.

        Use this for:
        - Docker images (no venv needed, keeps layers simple)
        - CI runners (GitHub Actions installs deps globally when
          GITHUB_RUN_ID is set)
        - Machines with a single Python version used globally

        NOTE: When running with --system, do NOT have a virtualenv activated,
        otherwise setup.py may install into the venv instead of the system
        site-packages (which might be what you want — then use --venv).

    --venv
        Install the Python bindings into the currently active virtualenv.
        The script checks that VIRTUAL_ENV is set. The Python files end up in:

            $VIRTUAL_ENV/lib/pythonX.Y/site-packages/pjsua2-2.16-….egg/

        This means "import pjsua2" works only when the venv is activated.
        The system Python will NOT see pjsua2.

        Use this for:
        - Local development where sipstuff runs inside a venv
        - Keeping the system Python clean
        - Having multiple projects with different PJSIP versions
          (though the C libraries in /usr/local/lib/ are still shared —
          only the Python wrappers are isolated)

        IMPORTANT: You must activate the venv BEFORE running this script:

            source .venv/bin/activate
            ./dist_scripts/install_pjsip.sh --venv

    --venv-all
        Install EVERYTHING into the currently active virtualenv — both the
        Python bindings AND the PJSIP C shared libraries. No sudo required.

        The C libraries end up in:

            $VIRTUAL_ENV/lib/libpj.so.2
            $VIRTUAL_ENV/lib/libpjsua2.so.2
            …

        The Python bindings end up in:

            $VIRTUAL_ENV/lib/pythonX.Y/site-packages/pjsua2-2.16-….egg/

        Because the C libraries are NOT in a system path, the dynamic linker
        cannot find them by default. This script automatically patches the
        venv's activate script to set LD_LIBRARY_PATH when the venv is
        activated (and restore it on deactivate).

        Use this for:
        - Local development without root access
        - Full isolation (nothing touches /usr/local/)
        - Machines where you cannot or do not want to use sudo
        - Reproducible builds where the venv is the single source of truth

        IMPORTANT: You must activate the venv BEFORE running this script,
        and RE-ACTIVATE it afterwards so the LD_LIBRARY_PATH change takes
        effect:

            source .venv/bin/activate
            ./dist_scripts/install_pjsip.sh --venv-all
            source .venv/bin/activate   # picks up LD_LIBRARY_PATH

PYTHON VERSION
    The script must build the SWIG bindings against the exact Python version
    you will use at runtime. The compiled _pjsua2.*.so contains the CPython
    ABI tag (e.g. cpython-314) and cannot be loaded by a different version.

    By default, the script uses "python3.14". Override with:

        PYTHON=python3.13 ./install_pjsip.sh --system

    Common pitfall: your system "python3" might point to 3.13 while your
    venv uses 3.14. Always verify:

        python3 --version        # system python
        python --version         # venv python (if activated)

PJSIP VERSION
    Default: 2.16. Override with:

        PJSIP_VERSION=2.14.1 ./install_pjsip.sh --system

    The tarball is downloaded from:
        https://github.com/pjsip/pjproject/archive/refs/tags/<VERSION>.tar.gz

ENVIRONMENT VARIABLES
    PYTHON          Python interpreter to use          (default: python3.14)
    PJSIP_VERSION   PJSIP release tag to build         (default: 2.16)
    BUILD_DIR       Temporary build directory           (default: /tmp/pjsip-build)

BUILD PREREQUISITES (Debian/Ubuntu)
    sudo apt install build-essential python3-dev python3.14-dev swig \
        libasound2-dev libssl-dev libopus-dev wget

    If building for a different Python version, install the matching -dev
    package (e.g. python3.13-dev).

HOW PJSIP INSTALLATION WORKS (two layers)
    pjsua2 is unusual because it has TWO separate layers that must both be
    present at runtime. Understanding this is key to troubleshooting:

    Layer 1: C shared libraries (system-wide, requires sudo)
    ─────────────────────────────────────────────────────────
        These are the core PJSIP libraries written in C/C++. They are
        installed via "sudo make install" into /usr/local/lib/:

            /usr/local/lib/libpj.so.2           core library
            /usr/local/lib/libpjsua2.so.2       high-level C++ API
            /usr/local/lib/libpjsip.so.2        SIP stack
            /usr/local/lib/libpjmedia.so.2      media framework
            /usr/local/lib/libpjnath.so.2       NAT traversal
            … (about 15 .so files total)

        These are ALWAYS installed system-wide because:
        - They are shared libraries (.so), not Python packages
        - The dynamic linker (ld.so) needs to find them at runtime
        - The Python _pjsua2.*.so extension is linked against them

        After installation, "sudo ldconfig" updates the linker cache so
        the system knows where to find these libraries.

    Layer 2: Python bindings (system or venv, controlled by --system/--venv)
    ────────────────────────────────────────────────────────────────────────
        These are the SWIG-generated Python wrappers:

            _pjsua2.cpython-314-x86_64-linux-gnu.so   C extension (calls Layer 1)
            pjsua2.py                                   Python API wrapper

        When you do "import pjsua2" in Python, this happens:

            pjsua2.py  →  imports _pjsua2  →  _pjsua2.*.so  →  dlopen(libpjsua2.so.2)
              (Layer 2)     (Layer 2)          (Layer 2)          (Layer 1)

        If Layer 1 is missing or not found → "cannot open shared object file"
        If Layer 2 is missing or wrong Python version → "No module named '_pjsua2'"

    WHY THIS MATTERS: You can install the Python bindings into a venv, but
    the C libraries MUST be findable by the dynamic linker. With --system
    and --venv, they go to /usr/local/lib/ and ldconfig handles this. With
    --venv-all, they go to $VIRTUAL_ENV/lib/ and LD_LIBRARY_PATH is needed
    (the script patches the activate script automatically).

LD_LIBRARY_PATH AND LINKER CONFIGURATION
    The dynamic linker (ld-linux.so) searches for shared libraries in:

        1. Paths in LD_LIBRARY_PATH (environment variable)
        2. Paths cached by ldconfig (from /etc/ld.so.conf.d/*.conf)
        3. Default paths: /lib, /usr/lib

    This script installs C libraries to /usr/local/lib/ and runs ldconfig.
    On most Debian/Ubuntu systems, /usr/local/lib is already listed in
    /etc/ld.so.conf.d/libc.conf, so ldconfig picks it up automatically.

    If ldconfig alone does not work (library not found at runtime), you have
    two options:

    Option A: Add /usr/local/lib to the linker config (permanent, recommended)

        echo "/usr/local/lib" | sudo tee /etc/ld.so.conf.d/pjsip.conf
        sudo ldconfig

    Option B: Set LD_LIBRARY_PATH (per-session, useful for debugging)

        export LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH:-}"
        python -c "import pjsua2"   # should work now

    You can also add this to your shell profile (~/.bashrc) or to the venv's
    activate script to make it permanent:

        echo 'export LD_LIBRARY_PATH="/usr/local/lib:${LD_LIBRARY_PATH:-}"' \
            >> .venv/bin/activate

    To check which libraries are found/missing at runtime:

        ldd $(python -c "import _pjsua2; print(_pjsua2.__file__)")

    This shows every .so that _pjsua2 depends on and whether it was resolved.
    Any line showing "not found" is the problem.

WHAT GETS INSTALLED WHERE (summary)
    C shared libraries (--system / --venv: system-wide, requires sudo):

        /usr/local/lib/libpj*.so.*          C shared libraries (~15 files)
        /usr/local/lib/pkgconfig/libpj*.pc  pkg-config files
        /usr/local/include/pj*/             C/C++ headers

    C shared libraries (--venv-all: inside venv, no sudo):

        $VIRTUAL_ENV/lib/libpj*.so.*        C shared libraries (~15 files)
        $VIRTUAL_ENV/lib/pkgconfig/libpj*.pc  pkg-config files
        $VIRTUAL_ENV/include/pj*/           C/C++ headers

    Python bindings (system or venv site-packages):

        _pjsua2.cpython-3XX-….so            C extension module
        pjsua2.py                            Python wrapper (SWIG-generated)

DOCKER / CI USAGE
    In a Dockerfile, use --system (no venv available):

        RUN ./install_pjsip.sh --system

    The Dockerfile in this repo handles this in the pjsip-builder stage.

    In CI (GitHub Actions), if GITHUB_RUN_ID is set, deps are installed
    globally — use --system as well.

TROUBLESHOOTING
    "ModuleNotFoundError: No module named '_pjsua2'"
        → Python version mismatch (Layer 2 problem). The .so was built for a
          different Python. The ABI tag in the filename must match your runtime:
            _pjsua2.cpython-314-x86_64-linux-gnu.so  ← needs Python 3.14
            _pjsua2.cpython-313-x86_64-linux-gnu.so  ← needs Python 3.13
          Check which Python you're actually running:
            python3 --version; python --version
          Rebuild with the correct PYTHON= variable.

    "error while loading shared libraries: libpjsua2.so.2: cannot open"
    "OSError: libpj.so.2: cannot open shared object file"
        → Layer 1 C libraries not found by the dynamic linker.
          Fix: sudo ldconfig
          If that doesn't help: see LD_LIBRARY_PATH section above.
          Quick diagnostic:
            ls /usr/local/lib/libpj*           # are the files there?
            ldconfig -p | grep libpj           # does the linker know about them?
            ldd /path/to/_pjsua2.*.so          # which deps are "not found"?

    "ImportError: … undefined symbol: …"
        → Version mismatch between Layer 1 (C libs) and Layer 2 (Python .so).
          Both must be built from the same PJSIP version. Rebuild everything
          from scratch (the script cleans BUILD_DIR automatically).

    "Remove port failed" warnings at runtime
        → This is a known PJSIP quirk, handled by sipstuff's orphan pattern.
          See CLAUDE.md for details.

    "cannot open shared object file" with --venv-all
        → The LD_LIBRARY_PATH is not set. Re-activate the venv:
            source .venv/bin/activate
          The script patches the activate script automatically. If it still
          fails, check manually:
            echo $LD_LIBRARY_PATH   # should contain $VIRTUAL_ENV/lib
            ls $VIRTUAL_ENV/lib/libpj*   # are the files there?

EXAMPLES
    # Local development with Python 3.14 venv (most common)
    source .venv/bin/activate
    ./dist_scripts/install_pjsip.sh --venv

    # System-wide install with default Python 3.14
    sudo ./dist_scripts/install_pjsip.sh --system

    # Build against Python 3.13 system-wide
    PYTHON=python3.13 ./dist_scripts/install_pjsip.sh --system

    # Build a specific PJSIP version
    PJSIP_VERSION=2.14.1 ./dist_scripts/install_pjsip.sh --venv

    # Fully isolated install into venv (no sudo needed)
    source .venv/bin/activate
    ./dist_scripts/install_pjsip.sh --venv-all
    source .venv/bin/activate   # re-activate to pick up LD_LIBRARY_PATH

    # Custom build directory (e.g. if /tmp is too small)
    BUILD_DIR=/var/tmp/pjsip ./dist_scripts/install_pjsip.sh --venv
HELPTEXT
    exit 0
}

# ── Argument Parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            show_help
            ;;
        --system)
            INSTALL_TARGET="system"
            shift
            ;;
        --venv)
            INSTALL_TARGET="venv"
            shift
            ;;
        --venv-all)
            INSTALL_TARGET="venv-all"
            shift
            ;;
        *)
            echo "ERROR: Unknown option '$1'. Use --help for usage." >&2
            exit 1
            ;;
    esac
done

if [[ -z "$INSTALL_TARGET" ]]; then
    echo "ERROR: You must specify --system, --venv, or --venv-all." >&2
    echo "Run with --help for detailed usage information." >&2
    exit 1
fi

# ── Venv Validation ──────────────────────────────────────────────────────────
if [[ "$INSTALL_TARGET" == "venv" || "$INSTALL_TARGET" == "venv-all" ]]; then
    if [[ -z "${VIRTUAL_ENV:-}" ]]; then
        echo "ERROR: --${INSTALL_TARGET} requires an activated virtualenv (VIRTUAL_ENV is not set)." >&2
        echo "Run: source .venv/bin/activate" >&2
        exit 1
    fi
    echo "=== Virtualenv detected: ${VIRTUAL_ENV} ==="
fi

# ── Start ─────────────────────────────────────────────────────────────────────
TARBALL_URL="https://github.com/pjsip/pjproject/archive/refs/tags/${PJSIP_VERSION}.tar.gz"

echo "=== Installing PJSIP ${PJSIP_VERSION} with Python bindings ==="
echo "    Python:    $PYTHON"
echo "    Target:    $INSTALL_TARGET"
echo "    Build dir: $BUILD_DIR"

# Check prerequisites
for cmd in "$PYTHON" swig make gcc; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found. Install build dependencies first." >&2
        echo "Run with --help for prerequisite details." >&2
        exit 1
    fi
done

# Python 3.12+ removed distutils; PJSIP's setup.py needs setuptools
"$PYTHON" -c "import setuptools" 2>/dev/null || {
    echo "=== Installing setuptools (required for Python bindings build) ==="
    "$PYTHON" -m pip install --no-cache-dir setuptools
}

PYTHON_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "    Python version: ${PYTHON_VERSION}"

# Clean previous build
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

echo "=== Downloading pjproject-${PJSIP_VERSION} ==="
wget -q "${TARBALL_URL}" -O pjproject.tar.gz
tar xzf pjproject.tar.gz
cd "pjproject-${PJSIP_VERSION}"

echo "=== Configuring PJSIP ==="
CONFIGURE_ARGS=(
    --enable-shared
    --disable-video
    --disable-v4l2
    --disable-libyuv
    --disable-libwebrtc
    --with-external-opus
)
if [[ "$INSTALL_TARGET" == "venv-all" ]]; then
    CONFIGURE_ARGS+=(--prefix="$VIRTUAL_ENV")
fi
./configure "${CONFIGURE_ARGS[@]}" \
    CFLAGS="-O2 -fPIC" \
    CXXFLAGS="-O2 -fPIC"

echo "=== Building PJSIP (this may take a few minutes) ==="
make -j"$(nproc)" dep
make -j"$(nproc)"

if [[ "$INSTALL_TARGET" == "venv-all" ]]; then
    make install
elif [[ $(id -u) -eq 0 ]]; then
    make install
else
    sudo make install
fi

echo "=== Building Python bindings ==="
cd pjsip-apps/src/swig/python
make PYTHON="$PYTHON"

echo "=== Installing Python bindings (target: ${INSTALL_TARGET}) ==="
if [[ "$INSTALL_TARGET" == "system" ]]; then
    if [[ $(id -u) -eq 0 ]]; then
        "$PYTHON" setup.py install
    else
        sudo "$PYTHON" setup.py install
    fi
else
    "$PYTHON" setup.py install
fi

# Refresh shared library cache / set up LD_LIBRARY_PATH
if [[ "$INSTALL_TARGET" == "venv-all" ]]; then
    echo "=== Patching venv activate script for LD_LIBRARY_PATH ==="
    ACTIVATE_SCRIPT="$VIRTUAL_ENV/bin/activate"

    # Patch deactivate function to restore old LD_LIBRARY_PATH
    if ! grep -q '_OLD_PJSIP_LD_LIBRARY_PATH' "$ACTIVATE_SCRIPT"; then
        # Insert LD_LIBRARY_PATH restore into the deactivate() function, right
        # before the "unset VIRTUAL_ENV" line
        sed -i '/^    unset VIRTUAL_ENV$/i\
    # Restore LD_LIBRARY_PATH (added by install_pjsip.sh --venv-all)\
    if [ -n "${_OLD_PJSIP_LD_LIBRARY_PATH+set}" ]; then\
        LD_LIBRARY_PATH="$_OLD_PJSIP_LD_LIBRARY_PATH"\
        export LD_LIBRARY_PATH\
        unset _OLD_PJSIP_LD_LIBRARY_PATH\
    fi' "$ACTIVATE_SCRIPT"

        # Append LD_LIBRARY_PATH setup at the end of activate
        cat >> "$ACTIVATE_SCRIPT" <<'PJSIP_ACTIVATE'

# PJSIP LD_LIBRARY_PATH (added by install_pjsip.sh --venv-all)
_OLD_PJSIP_LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
LD_LIBRARY_PATH="$VIRTUAL_ENV/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LD_LIBRARY_PATH
PJSIP_ACTIVATE
        echo "    Patched: $ACTIVATE_SCRIPT"
    else
        echo "    Already patched: $ACTIVATE_SCRIPT (skipped)"
    fi

    # Set for current session so verify step works
    export LD_LIBRARY_PATH="$VIRTUAL_ENV/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
else
    if [[ $(id -u) -eq 0 ]]; then
        ldconfig
    else
        sudo ldconfig
    fi
fi

echo "=== Verifying installation ==="
"$PYTHON" -c "import pjsua2; print('pjsua2 imported successfully')" || {
    echo "ERROR: pjsua2 import failed. Check the build output above." >&2
    echo "Hint: Run with --help for troubleshooting tips." >&2
    exit 1
}

echo "=== PJSIP ${PJSIP_VERSION} installed successfully (${INSTALL_TARGET}) ==="
if [[ "$INSTALL_TARGET" == "venv-all" ]]; then
    echo ""
    echo "NOTE: Re-activate your venv to pick up the LD_LIBRARY_PATH change:"
    echo "    source ${VIRTUAL_ENV}/bin/activate"
fi

# Cleanup
rm -rf "${BUILD_DIR}"
