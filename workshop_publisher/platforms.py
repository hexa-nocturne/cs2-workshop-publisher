"""Operating-system detection and SteamCMD discovery."""

import os
import platform
import shutil
import sys
from pathlib import Path


def current_os():
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    return sys.platform


def os_label(os_name=None):
    return {"windows": "Windows", "linux": "Linux", "macos": "macOS"}.get(os_name or current_os(), os_name)


def architecture():
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine or "unknown")


def linux_distribution():
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            values = dict(line.rstrip("\n").split("=", 1) for line in handle if "=" in line)
        return values.get("PRETTY_NAME", "").strip('"') or None
    except OSError:
        return None


def executable_names(os_name):
    return ["steamcmd.exe"] if os_name == "windows" else ["steamcmd.sh", "steamcmd"]


def steamcmd_candidates(os_name, env, home):
    """Well-known SteamCMD locations, in search order (excluding PATH lookup)."""
    home = Path(home)
    if os_name == "windows":
        candidates = [
            Path("C:/steamcmd/steamcmd.exe"),
            home / "steamcmd" / "steamcmd.exe",
        ]
        for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            if env.get(var):
                candidates.append(Path(env[var]) / "steamcmd" / "steamcmd.exe")
        return candidates
    return [
        home / "steamcmd" / "steamcmd.sh",
        home / "Steam" / "steamcmd.sh",
        home / ".steam" / "steamcmd" / "steamcmd.sh",
        home / ".local" / "share" / "Steam" / "steamcmd" / "steamcmd.sh",
        Path("/usr/games/steamcmd"),
        Path("/usr/bin/steamcmd"),
        Path("/usr/local/bin/steamcmd"),
        Path("/opt/steamcmd/steamcmd.sh"),
    ]


class SteamCmdLocation:
    def __init__(self, path, source, tried, error=None):
        self.path = path
        self.source = source
        self.tried = tried
        self.error = error

    @property
    def found(self):
        return self.path is not None


def find_steamcmd(env=None, os_name=None, home=None, which=shutil.which):
    env = os.environ if env is None else env
    os_name = os_name or current_os()
    home = home or Path.home()
    tried = []

    explicit = env.get("STEAMCMD_PATH", "").strip()
    if explicit:
        path = Path(os.path.expanduser(explicit))
        if path.is_dir():
            for name in executable_names(os_name):
                if (path / name).is_file():
                    return SteamCmdLocation(path / name, "STEAMCMD_PATH", [str(path)])
        elif path.is_file():
            return SteamCmdLocation(path, "STEAMCMD_PATH", [str(path)])
        # An explicit path that is wrong is an error, not a reason to guess elsewhere.
        return SteamCmdLocation(None, "STEAMCMD_PATH", [str(path)], "STEAMCMD_PATH points to a missing file: %s" % path)

    for name in ("steamcmd",) + (("steamcmd.sh",) if os_name != "windows" else ()):
        tried.append("PATH:" + name)
        resolved = which(name)
        if resolved:
            return SteamCmdLocation(Path(resolved), "PATH", tried)

    for candidate in steamcmd_candidates(os_name, env, home):
        tried.append(str(candidate))
        if candidate.is_file():
            return SteamCmdLocation(candidate, "auto-detected", tried)
    return SteamCmdLocation(None, None, tried)


def install_help(os_name=None):
    os_name = os_name or current_os()
    if os_name == "windows":
        return (
            "SteamCMD was not found.\n"
            "\n"
            "Install on Windows:\n"
            "  1. Download steamcmd.zip from Valve:\n"
            "     https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip\n"
            "  2. Extract it to C:\\steamcmd (or any folder) and run steamcmd.exe once.\n"
            "\n"
            "You may also set:\n"
            "  $env:STEAMCMD_PATH = 'C:\\path\\to\\steamcmd.exe'"
        )
    return (
        "SteamCMD was not found.\n"
        "\n"
        "Install on Ubuntu (the package is in the multiverse repository):\n"
        "  sudo add-apt-repository multiverse\n"
        "  sudo dpkg --add-architecture i386\n"
        "  sudo apt update\n"
        "  sudo apt install steamcmd\n"
        "\n"
        "Install on Debian (enable the non-free component first):\n"
        "  sudo dpkg --add-architecture i386\n"
        "  sudo apt update\n"
        "  sudo apt install steamcmd\n"
        "\n"
        "Or download it manually from Valve (no root needed, requires lib32gcc-s1):\n"
        "  mkdir -p ~/steamcmd && cd ~/steamcmd\n"
        "  curl -fsSLO https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz\n"
        "  tar -xzf steamcmd_linux.tar.gz\n"
        "\n"
        "You may also set:\n"
        "  STEAMCMD_PATH=/path/to/steamcmd.sh"
    )


LINUX_32BIT_LIBGCC = (
    "/lib/i386-linux-gnu/libgcc_s.so.1",
    "/usr/lib/i386-linux-gnu/libgcc_s.so.1",
    "/usr/lib32/libgcc_s.so.1",
    "/lib32/libgcc_s.so.1",
)


def linux_32bit_runtime_present():
    """SteamCMD's bootstrapper is 32-bit and needs lib32gcc-s1 (or equivalent)."""
    return any(os.path.exists(p) for p in LINUX_32BIT_LIBGCC)
