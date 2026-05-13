#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_FILE="${HOME}/Library/Application Support/FonesGo Location Changer/.limitMapData"
HISTORY_FILE="${HOME}/Library/Application Support/FonesGo Location Changer/.newHistory.dat"
KEY_NAME="0"

value="$(python3 "${SCRIPT_DIR}/check-xml-key.py" "${TARGET_FILE}" "${KEY_NAME}")"

if [[ "${value}" == "2" ]]; then
  rm -f "${TARGET_FILE}"
  rm -f "${HISTORY_FILE}"
  echo "deleted ${TARGET_FILE} ${HISTORY_FILE}"
else
  echo "${value}"
fi
