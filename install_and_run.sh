#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$APP_DIR/live_assistant.py"
# Model presets
MODEL_SIZE="small" # small | medium
MODEL_DIR_DEFAULT_SMALL="$HOME/.cache/vosk-model-small-en-us-0.15"
MODEL_ZIP_URL_SMALL="https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
MODEL_DIR_DEFAULT_MEDIUM="$HOME/.cache/vosk-model-en-us-0.22"
MODEL_ZIP_URL_MEDIUM="https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--no-sudo] [--no-model] [--model-path PATH] [--model SIZE] [--skip-install]

Installs dependencies (Debian/Ubuntu), sets up Vosk model, and launches the TUI.

Options:
  --no-sudo        Do not use sudo for package installation.
  --no-model       Do not download a Vosk model automatically.
  --model-path P   Use an existing Vosk model directory (overrides default).
  --model SIZE     Vosk model to auto-download: small (default) or medium.
  --skip-install   Skip package and pip installation steps.
  -h, --help       Show this help message.

Notes:
  - Requires internet for apt/pip/model download.
  - For other distros, install equivalents of: ffmpeg, pactl (pulseaudio-utils), python3, pip3, unzip, wget.
EOF
}

SUDO=sudo
DOWNLOAD_MODEL=1
CUSTOM_MODEL_PATH=""
DO_INSTALL=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-sudo) SUDO=""; shift ;;
    --no-model) DOWNLOAD_MODEL=0; shift ;;
    --model-path) CUSTOM_MODEL_PATH="$2"; shift 2 ;;
    --model) MODEL_SIZE="$2"; shift 2 ;;
    --skip-install) DO_INSTALL=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

command_exists() { command -v "$1" >/dev/null 2>&1; }

ensure_apt_packages() {
  local pkgs=(ffmpeg pulseaudio-utils python3 python3-pip unzip wget)
  echo "[+] Installing system packages: ${pkgs[*]}"
  if [[ -n "$SUDO" ]]; then
    $SUDO apt-get update -y
    $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
  else
    apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${pkgs[@]}"
  fi
}

install_system_packages() {
  if command_exists apt-get; then
    ensure_apt_packages
  else
    echo "[!] Non-Debian/Ubuntu system detected. Please install: ffmpeg, pactl, python3, pip, unzip, wget"
  fi
}

install_pip_packages() {
  echo "[+] Installing Python packages"
  if command_exists python3; then
    python3 -m pip install --upgrade pip setuptools wheel --break-system-packages || true
    python3 -m pip install --upgrade vosk requests --break-system-packages || true
  else
    echo "[!] python3 not found. Please install it first."; exit 1
  fi
}

download_vosk_model() {
  local dest_dir="$1"
  local url="$2"
  if [[ -d "$dest_dir" ]]; then
    echo "[=] Vosk model already present at: $dest_dir"
    return
  fi
  mkdir -p "$(dirname "$dest_dir")"
  local tmp_zip="$dest_dir.zip"
  echo "[+] Downloading Vosk model to $tmp_zip"
  wget -O "$tmp_zip" "$url"
  echo "[+] Unpacking model to $dest_dir"
  mkdir -p "$dest_dir"
  unzip -q "$tmp_zip" -d "$(dirname "$dest_dir")"
  local extracted
  extracted=$(unzip -Z1 "$tmp_zip" | head -n1 | cut -d/ -f1)
  if [[ -n "$extracted" && -d "$(dirname "$dest_dir")/$extracted" ]]; then
    mv "$(dirname "$dest_dir")/$extracted" "$dest_dir"
  fi
  rm -f "$tmp_zip"
  echo "[+] Model ready at $dest_dir"
}

run_app() {
  local model_path="$1"
  echo "[+] Launching Live Assistant"
  if [[ -n "$model_path" ]]; then
    VOSK_MODEL_PATH="$model_path" python3 "$PY_SCRIPT"
  else
    python3 "$PY_SCRIPT"
  fi
}

echo "=== LiveAssistant Setup ==="

if [[ $DO_INSTALL -eq 1 ]]; then
  install_system_packages
  install_pip_packages
else
  echo "[=] Skipping package installations as requested"
fi

MODEL_PATH_FINAL=""
if [[ $DOWNLOAD_MODEL -eq 1 ]]; then
  case "$MODEL_SIZE" in
    medium)
      DEF_DIR="$MODEL_DIR_DEFAULT_MEDIUM"; DEF_URL="$MODEL_ZIP_URL_MEDIUM" ;;
    small|*)
      DEF_DIR="$MODEL_DIR_DEFAULT_SMALL"; DEF_URL="$MODEL_ZIP_URL_SMALL" ;;
  esac
  MODEL_PATH_FINAL="${CUSTOM_MODEL_PATH:-$DEF_DIR}"
  download_vosk_model "$MODEL_PATH_FINAL" "$DEF_URL"
else
  MODEL_PATH_FINAL="$CUSTOM_MODEL_PATH"
fi

if [[ ! -f "$PY_SCRIPT" ]]; then
  echo "[!] Could not find Python script at: $PY_SCRIPT" >&2
  exit 1
fi

run_app "$MODEL_PATH_FINAL"

