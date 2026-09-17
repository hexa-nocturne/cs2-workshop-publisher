import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAKE_DIR = ROOT / "tests" / "fake_steamcmd"
FAKE_STEAMCMD = FAKE_DIR / ("steamcmd.cmd" if os.name == "nt" else "steamcmd.sh")
DEMO = ROOT / "examples" / "demo"


class TempProject:
    """A copy of the demo project inside a directory whose path contains spaces."""

    def __init__(self):
        self.base = Path(tempfile.mkdtemp(prefix="workshop test "))
        self.root = self.base / "my project dir"
        shutil.copytree(str(DEMO), str(self.root), ignore=shutil.ignore_patterns("build", "build-cache"))
        self.config = self.root / "workshop.json"

    def edit(self, **changes):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        for key, value in changes.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        self.config.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def data(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def cleanup(self):
        shutil.rmtree(str(self.base), ignore_errors=True)


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("STEAM", "WORKSHOP", "FAKE_", "CS2_"))}
    env["FAKE_STEAMCMD_PYTHON"] = sys.executable
    env["NO_COLOR"] = "1"
    # An empty HOME so a real SteamCMD installation is never auto-detected.
    empty_home = tempfile.mkdtemp(prefix="workshop-home-")
    env["HOME"] = empty_home
    env["USERPROFILE"] = empty_home
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep) if "steamcmd" not in p.lower())
    env.update(extra)
    return env


def run_cli(args, env=None, cwd=None, timeout=120):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "workshop.py")] + [str(a) for a in args],
        env=env or clean_env(),
        cwd=str(cwd or ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    completed.out = completed.stdout.decode("utf-8", "replace")
    completed.err = completed.stderr.decode("utf-8", "replace")
    return completed
