#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/build"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# Find the venv Python, fall back to python3
if [ -f "${PROJECT_ROOT}/.venv/bin/python" ]; then
    PYTHON="${PROJECT_ROOT}/.venv/bin/python"
else
    PYTHON="python3"
fi

echo "=== Building Order Flow Engine ==="
echo "Source:  ${SCRIPT_DIR}"
echo "Build:   ${BUILD_DIR}"
echo "Python:  ${PYTHON}"

mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH="/opt/homebrew" \
    -DPython_EXECUTABLE="${PYTHON}" \
    -DPython_ROOT_DIR="$(dirname "$(dirname "${PYTHON}")")" \
    -Dpybind11_DIR="$("${PYTHON}" -m pybind11 --cmakedir)"

make -j"$(sysctl -n hw.ncpu 2>/dev/null || nproc)"

echo ""
echo "=== Build complete ==="
echo "Module: $(find . -name 'orderflow_engine*' -type f | head -1)"
