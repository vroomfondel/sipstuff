#!/usr/bin/env bash
#
# Extract pre-built pjsua2 bindings + PJSIP shared libraries from the
# sipstuff Docker image into the local .venv so you can develop without
# building PJSIP from source.
#
# Run with --help for detailed usage information.

set -euo pipefail

DOCKER_IMAGE="${DOCKER_IMAGE:-xomoxcc/sipstuff:latest}"
VENV_DIR=".venv"
PJSIP_LIBS_DIR="${VENV_DIR}/pjsip-libs"
LIB_TARGET=""  # set by --system-libs / --local-libs or interactive prompt

# ── Help ──────────────────────────────────────────────────────────────────────
show_help() {
    cat <<'HELPTEXT'
copy_pjsua2_from_docker.sh — Extract pre-built pjsua2 from Docker image

SYNOPSIS
    ./dist_scripts/copy_pjsua2_from_docker.sh --system-libs
    ./dist_scripts/copy_pjsua2_from_docker.sh --local-libs
    ./dist_scripts/copy_pjsua2_from_docker.sh              # interactive prompt

DESCRIPTION
    pjsua2 is not available on PyPI — it must be compiled from PJSIP C/C++
    source. This script skips that by extracting the pre-built bindings from
    the sipstuff Docker image into your local .venv.

    It performs the following steps:

      1. Pull the Docker image (if not already present)
      2. Create a temporary container for filesystem access
      3. Copy Python bindings (pjsua2.py + _pjsua2.*.so) into .venv/site-packages
      4. Copy PJSIP shared libraries to the chosen location
      5. Verify that "import pjsua2" works

    The Python bindings always go into .venv/site-packages. You choose where
    the PJSIP shared libraries (.so files) are installed.

WHAT GETS INSTALLED
    Two separate components are needed to use pjsua2 from Python:

    1. Python bindings (always → .venv/site-packages/)
        pjsua2.py                            SWIG-generated Python wrapper
        _pjsua2.cpython-314-….so             C extension module (CPython ABI-specific)

       These are the files you import in Python ("import pjsua2"). The C
       extension is compiled against a specific Python version — a 3.14 build
       cannot be loaded by Python 3.13.

    2. PJSIP shared libraries (you choose the location)
        libpj.so, libpjsua2.so, libpjmedia.so, libpjnath.so, …

       The C extension (_pjsua2.*.so) dynamically links against these at
       runtime. Without them, "import pjsua2" fails with:
           "error while loading shared libraries: libpj.so.2"

    The --system-libs / --local-libs flags control where the C libraries go.
    Python bindings always go into the venv:

        --system-libs / --local-libs
              │              │
              │              ├─ Python bindings → .venv/lib/pythonX.Y/site-packages/
              │              └─ C libraries     → .venv/pjsip-libs/
              ├─ Python bindings → .venv/lib/pythonX.Y/site-packages/
              └─ C libraries     → /usr/local/lib/

    See LIBRARY INSTALL MODES below for details on each flag.

LIBRARY INSTALL MODES

    --system-libs
        Copy shared libraries to /usr/local/lib/ and run ldconfig (needs sudo).
        No LD_LIBRARY_PATH needed at runtime — the system linker finds them
        automatically.

    --local-libs
        Copy shared libraries to .venv/pjsip-libs/ (no sudo required).

        The system linker only searches standard paths like /usr/local/lib/
        by default. When libs live inside .venv/, you must point
        LD_LIBRARY_PATH there so the Python extension module can find them
        at runtime:

            export LD_LIBRARY_PATH=".venv/pjsip-libs:${LD_LIBRARY_PATH:-}"

    Without a flag, the script asks interactively which mode to use.

ENVIRONMENT VARIABLES
    DOCKER_IMAGE    Docker image to extract from   (default: xomoxcc/sipstuff:latest)

PREREQUISITES
    - docker or podman installed
    - .venv exists (run "make install" first)
    - .venv uses Python 3.14 (must match the Docker image)

EXAMPLES
    # System-wide libs (no LD_LIBRARY_PATH needed)
    ./dist_scripts/copy_pjsua2_from_docker.sh --system-libs

    # Local libs (no sudo needed)
    ./dist_scripts/copy_pjsua2_from_docker.sh --local-libs

    # Interactive — script asks where to put libs
    ./dist_scripts/copy_pjsua2_from_docker.sh

    # Use a custom Docker image
    DOCKER_IMAGE=myregistry/sipstuff:dev ./dist_scripts/copy_pjsua2_from_docker.sh --local-libs
HELPTEXT
    exit 0
}

# ── Argument Parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h)
            show_help
            ;;
        --system-libs)
            LIB_TARGET="system"
            shift
            ;;
        --local-libs)
            LIB_TARGET="local"
            shift
            ;;
        *)
            echo "ERROR: Unknown option '$1'. Use --help for usage." >&2
            exit 1
            ;;
    esac
done

# ── Interactive prompt if no flag given ───────────────────────────────────────
if [[ -z "${LIB_TARGET}" ]]; then
    echo "Where should PJSIP shared libraries be installed?"
    echo "  1) /usr/local/lib/  (system-wide, needs sudo, no LD_LIBRARY_PATH needed)"
    echo "  2) .venv/pjsip-libs/ (local, needs LD_LIBRARY_PATH at runtime)"
    read -rp "Choice [1/2]: " choice
    case "${choice}" in
        1) LIB_TARGET="system" ;;
        2) LIB_TARGET="local" ;;
        *)
            echo "ERROR: Invalid choice '${choice}'. Expected 1 or 2." >&2
            exit 1
            ;;
    esac
fi

# --- Detect container runtime (docker or podman) ---
if command -v docker &>/dev/null; then
    RUNTIME="docker"
elif command -v podman &>/dev/null; then
    RUNTIME="podman"
else
    echo "ERROR: Neither docker nor podman found. Install one of them first." >&2
    exit 1
fi
echo "Using container runtime: ${RUNTIME}"

# --- Check .venv exists ---
if [[ ! -d "${VENV_DIR}" ]]; then
    echo "ERROR: ${VENV_DIR} not found. Run 'make install' first." >&2
    exit 1
fi

# --- Validate Python version (Docker image has 3.14) ---
REQUIRED_PY="3.14"
LOCAL_PY=$("${VENV_DIR}/bin/python" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if [[ "${LOCAL_PY}" != "${REQUIRED_PY}" ]]; then
    echo "ERROR: Local .venv uses Python ${LOCAL_PY}, but Docker image has ${REQUIRED_PY} bindings." >&2
    echo "       pjsua2 C extensions are version-specific and cannot be mixed." >&2
    exit 1
fi

# --- Resolve site-packages path ---
SITE_PACKAGES=$("${VENV_DIR}/bin/python" -c "import site; print(site.getsitepackages()[0])")
echo "site-packages: ${SITE_PACKAGES}"

# --- Pull image if not present ---
if ! ${RUNTIME} image inspect "${DOCKER_IMAGE}" &>/dev/null; then
    echo "Pulling ${DOCKER_IMAGE} ..."
    ${RUNTIME} pull "${DOCKER_IMAGE}"
fi

# --- Create temporary container (no run, just filesystem access) ---
CID=$(${RUNTIME} create "${DOCKER_IMAGE}" true)
echo "Temporary container: ${CID:0:12}"

cleanup() {
    echo "Removing temporary container ..."
    ${RUNTIME} rm "${CID}" >/dev/null 2>&1 || true
    rm -rf /tmp/pjsip-all-libs
}
trap cleanup EXIT

# --- Copy Python bindings ---
echo "Copying Python bindings → ${SITE_PACKAGES}/"
${RUNTIME} cp "${CID}:/usr/local/lib/python3.14/site-packages/pjsua2.py" "${SITE_PACKAGES}/pjsua2.py"
${RUNTIME} cp "${CID}:/usr/local/lib/python3.14/site-packages/_pjsua2.cpython-314-x86_64-linux-gnu.so" "${SITE_PACKAGES}/_pjsua2.cpython-314-x86_64-linux-gnu.so"
echo "  pjsua2.py"
echo "  _pjsua2.cpython-314-x86_64-linux-gnu.so"

# --- Copy PJSIP shared libraries ---
echo "Extracting PJSIP shared libraries ..."
rm -rf /tmp/pjsip-all-libs
mkdir -p /tmp/pjsip-all-libs
${RUNTIME} cp "${CID}:/usr/local/lib/." /tmp/pjsip-all-libs/

if [[ "${LIB_TARGET}" == "system" ]]; then
    echo "Installing PJSIP libs → /usr/local/lib/ (sudo) ..."
    sudo cp /tmp/pjsip-all-libs/libpj*.so* /usr/local/lib/
    sudo cp /tmp/pjsip-all-libs/libpjsip*.so* /usr/local/lib/ 2>/dev/null || true
    sudo cp /tmp/pjsip-all-libs/libpjmedia*.so* /usr/local/lib/ 2>/dev/null || true
    sudo cp /tmp/pjsip-all-libs/libpjnath*.so* /usr/local/lib/ 2>/dev/null || true
    sudo cp /tmp/pjsip-all-libs/libpjlib-util*.so* /usr/local/lib/ 2>/dev/null || true
    sudo cp /tmp/pjsip-all-libs/libresample*.so* /usr/local/lib/ 2>/dev/null || true
    sudo ldconfig
    echo "Libraries installed system-wide. No LD_LIBRARY_PATH needed."
else
    mkdir -p "${PJSIP_LIBS_DIR}"
    cp /tmp/pjsip-all-libs/libpj*.so* "${PJSIP_LIBS_DIR}/" 2>/dev/null || true
    cp /tmp/pjsip-all-libs/libpjsip*.so* "${PJSIP_LIBS_DIR}/" 2>/dev/null || true
    cp /tmp/pjsip-all-libs/libpjmedia*.so* "${PJSIP_LIBS_DIR}/" 2>/dev/null || true
    cp /tmp/pjsip-all-libs/libpjnath*.so* "${PJSIP_LIBS_DIR}/" 2>/dev/null || true
    cp /tmp/pjsip-all-libs/libpjlib-util*.so* "${PJSIP_LIBS_DIR}/" 2>/dev/null || true
    cp /tmp/pjsip-all-libs/libresample*.so* "${PJSIP_LIBS_DIR}/" 2>/dev/null || true
    LIB_COUNT=$(find "${PJSIP_LIBS_DIR}" -name "*.so*" | wc -l)
    echo "Copied ${LIB_COUNT} library files → ${PJSIP_LIBS_DIR}/"
fi

# --- Verify import ---
echo ""
echo "=== Verifying pjsua2 import ==="
if [[ "${LIB_TARGET}" == "system" ]]; then
    "${VENV_DIR}/bin/python" -c "import pjsua2; print('pjsua2 imported successfully')"
else
    LD_LIBRARY_PATH="${PJSIP_LIBS_DIR}:${LD_LIBRARY_PATH:-}" "${VENV_DIR}/bin/python" -c "import pjsua2; print('pjsua2 imported successfully')"
fi

echo ""
echo "=== Done ==="
if [[ "${LIB_TARGET}" == "local" ]]; then
    echo ""
    echo "To use pjsua2, set LD_LIBRARY_PATH before running Python:"
    echo "  export LD_LIBRARY_PATH=\"${PJSIP_LIBS_DIR}:\${LD_LIBRARY_PATH:-}\""
fi
