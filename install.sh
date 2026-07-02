#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_FILE="${SCRIPT_DIR}/aim_power_resume.py"
TARGET_DIR="/home/kickpi/klipper/klippy/extras"
TARGET_FILE="${TARGET_DIR}/aim_power_resume.py"

if [[ ! -f "${SOURCE_FILE}" ]]; then
    echo "Error: source file not found: ${SOURCE_FILE}" >&2
    exit 1
fi

if [[ ! -d "${TARGET_DIR}" ]]; then
    echo "Creating target directory: ${TARGET_DIR}"
    mkdir -p "${TARGET_DIR}"
fi

echo "Installing ${SOURCE_FILE}"
echo "       to ${TARGET_FILE}"
cp "${SOURCE_FILE}" "${TARGET_FILE}"
chmod 644 "${TARGET_FILE}"

echo "Install complete."
echo "Restart Klipper after updating printer.cfg with [aim_power_resume]."
