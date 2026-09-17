#!/usr/bin/env bash
set -Eeuo pipefail
exec "${FAKE_STEAMCMD_PYTHON:-python3}" "$(dirname "${BASH_SOURCE[0]}")/fake_steamcmd.py" "$@"
