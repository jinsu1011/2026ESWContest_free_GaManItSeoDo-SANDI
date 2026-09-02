#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
RASPBERRY_PI_ROOT="$(cd -- "${RUNTIME_ROOT}/.." && pwd)"
REPOSITORY_ROOT="$(cd -- "${RASPBERRY_PI_ROOT}/.." && pwd)"
VENV_PATH="${SAFENEST_VENV_PATH:-${REPOSITORY_ROOT}/.venv}"

if [[ -f "${REPOSITORY_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPOSITORY_ROOT}/.env"
  set +a
fi

if [[ "${1:-}" == "--install" ]]; then
  shift
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 was not found. Install Python 3.10 or newer first." >&2
    exit 2
  fi
  if ! python3 -m venv "${VENV_PATH}"; then
    echo "Failed to create ${VENV_PATH}. On Raspberry Pi OS run:" >&2
    echo "  sudo apt install -y python3-venv python3-pip python3-dev build-essential" >&2
    exit 2
  fi
  "${VENV_PATH}/bin/python" -m pip install --upgrade pip
  "${VENV_PATH}/bin/python" -m pip install \
    -r "${RUNTIME_ROOT}/requirements-backend.txt" \
    -r "${RASPBERRY_PI_ROOT}/Ondevice_AI/requirements-pi.txt"
  "${VENV_PATH}/bin/python" -c "import fastapi, piper, qrcode, torch, uvicorn"

  # Voice dataset license: CC BY-NC-SA 4.0 (model stays local and untracked).
  TTS_VOICE_NAME="ko_KR-kss-medium"
  TTS_VOICE_DIR="${RUNTIME_ROOT}/data/tts"
  mkdir -p "${TTS_VOICE_DIR}"
  "${VENV_PATH}/bin/python" -m piper.download_voices \
    --data-dir "${TTS_VOICE_DIR}" \
    "${TTS_VOICE_NAME}"
fi

if [[ ! -x "${VENV_PATH}/bin/python" ]]; then
  echo "SafeNest virtual environment not found: ${VENV_PATH}" >&2
  echo "Run: ./run_safenest.sh --install" >&2
  exit 2
fi

# Every runtime module resolves siblings through RaspberryPi/Runtime/paths.py,
# so the runtime directory is the single import root.
cd "${RUNTIME_ROOT}"
"${VENV_PATH}/bin/python" -m hil.preflight
exec "${VENV_PATH}/bin/python" backend/run_backend.py "$@"
