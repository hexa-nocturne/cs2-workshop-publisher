#!/usr/bin/env bash
# Checks an Ubuntu/Debian server for everything the Workshop pipeline needs.
#
# It never runs sudo or changes system configuration. Missing packages are listed
# with the exact command to install them. Safe to run repeatedly.
#
# Usage: scripts/setup-ubuntu.sh [--config path/to/workshop.json]
set -Eeuo pipefail

script_dir="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(dirname "$script_dir")"
workshop=("$repo_dir/workshop")
if [ "${1:-}" = "--config" ] && [ -n "${2:-}" ]; then
  workshop+=(--config "$2")
fi

missing_packages=()
need_i386=0
need_multiverse=0
problems=0

ok()   { printf '  [ OK ] %s\n' "$1"; }
warn() { printf '  [WARN] %s\n' "$1"; }
bad()  { printf '  [MISS] %s\n' "$1"; problems=$((problems + 1)); }

echo "Workshop publisher: Ubuntu readiness check"
echo

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  ok "OS: ${PRETTY_NAME:-unknown}"
  case "${ID:-}:${VERSION_ID:-}" in
    ubuntu:22.04|ubuntu:24.04|debian:12) ;;
    *) warn "untested distribution; Ubuntu 22.04/24.04 are the supported targets" ;;
  esac
fi

arch="$(uname -m)"
if [ "$arch" = "x86_64" ]; then ok "Architecture: $arch"; else bad "Architecture $arch (x86_64 required for SteamCMD and Wine)"; fi

if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
  ok "Python: $(python3 --version 2>&1)"
else
  bad "Python 3.8+"
  missing_packages+=(python3)
fi

if dpkg --print-foreign-architectures 2>/dev/null | grep -qx i386; then
  ok "i386 architecture enabled (needed by SteamCMD)"
else
  bad "i386 architecture not enabled"
  need_i386=1
fi

if command -v steamcmd >/dev/null 2>&1 || [ -x "${STEAMCMD_PATH:-/nonexistent}" ] || [ -x "$HOME/steamcmd/steamcmd.sh" ]; then
  ok "SteamCMD found"
else
  bad "SteamCMD"
  missing_packages+=(steamcmd)
  if [ "${ID:-}" = "ubuntu" ]; then need_multiverse=1; fi
fi

check_command() {
  local command_name="$1" package="$2" purpose="$3"
  if command -v "$command_name" >/dev/null 2>&1; then
    ok "$command_name ($purpose)"
  else
    bad "$command_name ($purpose)"
    missing_packages+=("$package")
  fi
}
check_command wine wine "runs the CS2 resource compiler"
check_command wine64 wine64 "64-bit Wine loader"
check_command xvfb-run xvfb "headless display for Wine"

if [ -e /usr/lib32/libgcc_s.so.1 ] || [ -e /lib/i386-linux-gnu/libgcc_s.so.1 ] || [ -e /usr/lib/i386-linux-gnu/libgcc_s.so.1 ]; then
  ok "32-bit libgcc (SteamCMD runtime)"
else
  bad "32-bit libgcc (SteamCMD runtime)"
  missing_packages+=(lib32gcc-s1)
fi

if command -v systemd-run >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  ok "systemd user manager (memory/CPU limits for builds)"
else
  warn "systemd user manager unavailable; memoryMax/cpuQuota limits will be skipped (nice/ionice still apply)"
fi

if [ -n "${CS2_TOOLS_DIR:-}" ]; then
  existing="$CS2_TOOLS_DIR"
  while [ ! -d "$existing" ]; do existing="$(dirname "$existing")"; done
  free_gb="$(df -Pk "$existing" | awk 'NR == 2 { printf "%d", $4 / 1048576 }')"
  if [ "$free_gb" -ge 70 ]; then ok "CS2_TOOLS_DIR=$CS2_TOOLS_DIR (${free_gb} GB free)"; else warn "CS2_TOOLS_DIR has only ${free_gb} GB free; about 60 GB is needed"; fi
else
  bad "CS2_TOOLS_DIR is not set (directory for the Windows CS2 build used to compile assets)"
fi

echo
if [ "${#missing_packages[@]}" -gt 0 ] || [ "$need_i386" -eq 1 ]; then
  echo "Run these commands yourself (this script never uses sudo):"
  echo
  if [ "$need_multiverse" -eq 1 ]; then echo "  sudo add-apt-repository -y multiverse"; fi
  if [ "$need_i386" -eq 1 ]; then echo "  sudo dpkg --add-architecture i386"; fi
  echo "  sudo apt update"
  if [ "${#missing_packages[@]}" -gt 0 ]; then
    printf '  sudo apt install --no-install-recommends'
    printf ' %s' "${missing_packages[@]}"
    printf '\n'
  fi
  echo
fi

if [ "$problems" -eq 0 ]; then
  echo "System packages look ready. Next steps (as the build user, no root needed):"
else
  echo "After fixing the items above, continue with (as the build user, no root needed):"
fi
cat <<EOF

  ${workshop[*]} login                 # once, interactively: caches the Steam session
  ${workshop[*]} tools install         # downloads the Windows CS2 build (~60 GB) into \$CS2_TOOLS_DIR
  ${workshop[*]} tools setup-wine      # creates the Wine prefix
  ${workshop[*]} doctor
  ${workshop[*]} update --dry-run --change-note "test"
EOF

[ "$problems" -eq 0 ]
