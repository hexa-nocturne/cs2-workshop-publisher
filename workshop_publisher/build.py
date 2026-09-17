"""Configurable build/package steps.

A build is an ordered list of steps. Each step may be restricted to specific
platforms. If any step cannot run on the current platform the build as a whole
is reported as unsupported here, so a Linux machine never runs half a build and
then uploads a mixed artifact. Linux can still publish a previously built
artifact with --no-build.

Step types:
  {"name": "...", "run": ["program", "arg", ...], "platforms": ["windows"]}
  {"name": "...", "copy": {"from": "./content", "to": "{content}", "exclude": ["*.psd"]}}
  {"name": "...", "clean": "{content}"}
  {"name": "...", "cs2": {...}}        compile + pack a CS2 addon (see cs2.py)

Placeholders: {root} (directory of workshop.json), {content} (contentDirectory).
Environment variables: ${NAME}. Commands are executed without a shell.
"""

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from .platforms import current_os

PLATFORMS = ("windows", "linux", "macos")
ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
STEP_TYPES = ("run", "copy", "clean", "cs2")


class BuildError(Exception):
    def __init__(self, message, **data):
        super().__init__(message)
        self.data = data


class BuildStep:
    def __init__(self, index, name, kind, spec, platforms, cwd):
        self.index = index
        self.name = name
        self.kind = kind
        self.spec = spec
        self.platforms = platforms
        self.cwd = cwd

    def supported_on(self, os_name):
        return not self.platforms or os_name in self.platforms

    def describe(self):
        label = self.name or "%s step" % self.kind
        if self.platforms:
            label += " (%s only)" % ", ".join(self.platforms)
        return label


class BuildConfig:
    def __init__(self, steps):
        self.steps = steps

    def unsupported_steps(self, os_name=None):
        os_name = os_name or current_os()
        return [s for s in self.steps if not s.supported_on(os_name)]


def parse_build_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("must be an object with a 'steps' list")
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list):
        raise ValueError("'steps' must be a list")
    steps = []
    for i, item in enumerate(steps_raw, 1):
        where = "step %d" % i
        if not isinstance(item, dict):
            raise ValueError("%s must be an object" % where)
        kinds = [k for k in STEP_TYPES if k in item]
        if len(kinds) != 1:
            raise ValueError("%s must contain exactly one of: %s" % (where, ", ".join(STEP_TYPES)))
        kind = kinds[0]
        spec = item[kind]
        platforms = item.get("platforms") or []
        if isinstance(platforms, str):
            platforms = [platforms]
        if not isinstance(platforms, list) or any(p not in PLATFORMS for p in platforms):
            raise ValueError("%s: 'platforms' entries must be from %s" % (where, ", ".join(PLATFORMS)))
        if kind == "run":
            if not isinstance(spec, list) or not spec or not all(isinstance(a, str) for a in spec):
                raise ValueError("%s: 'run' must be a non-empty list of strings (program and arguments)" % where)
        elif kind == "copy":
            if not isinstance(spec, dict) or not isinstance(spec.get("from"), str) or not isinstance(spec.get("to"), str):
                raise ValueError("%s: 'copy' needs string 'from' and 'to'" % where)
            exclude = spec.get("exclude", [])
            if not isinstance(exclude, list) or not all(isinstance(e, str) for e in exclude):
                raise ValueError("%s: 'copy.exclude' must be a list of glob patterns" % where)
        elif kind == "clean":
            if not isinstance(spec, str):
                raise ValueError("%s: 'clean' must be a directory path" % where)
        elif kind == "cs2":
            from .cs2 import Cs2Settings

            spec = Cs2Settings(spec, where + ".cs2")
        cwd = item.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("%s: 'cwd' must be a string" % where)
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError("%s: 'name' must be a string" % where)
        steps.append(BuildStep(i, name, kind, spec, platforms, cwd))
    return BuildConfig(steps)


def expand(value, cfg, env):
    """Substitute placeholders and ${ENV} references; raise on undefined variables."""
    missing = [name for name in ENV_VAR_RE.findall(value) if not env.get(name)]
    if missing:
        raise BuildError("environment variable(s) not set: %s" % ", ".join(sorted(set(missing))))
    value = ENV_VAR_RE.sub(lambda m: env[m.group(1)], value)
    return value.replace("{root}", str(cfg.root)).replace("{content}", str(cfg.content_dir))


def _resolve(value, cfg, env):
    from .config import resolve_config_path

    return resolve_config_path(expand(value, cfg, env), cfg.root)


def _ensure_inside_root(path, cfg, action):
    root = Path(os.path.abspath(str(cfg.root)))
    target = Path(os.path.abspath(str(path)))
    if target == root or root not in target.parents:
        raise BuildError("refusing to %s '%s': it must be a sub-directory of %s" % (action, target, root))


def run_build(cfg, console, env=None, os_name=None, clean=False):
    """Run all build steps. Returns a list of per-step result dicts."""
    env = dict(os.environ if env is None else env)
    os_name = os_name or current_os()
    results = []
    if not cfg.build or not cfg.build.steps:
        console.info("No build steps configured; using contentDirectory as-is.")
        return results
    unsupported = cfg.build.unsupported_steps(os_name)
    if unsupported:
        raise BuildError(
            "the build cannot run on this platform because these steps are restricted to other platforms:\n  - "
            + "\n  - ".join(s.describe() for s in unsupported)
        )
    env.setdefault("WORKSHOP_ROOT", str(cfg.root))
    env.setdefault("WORKSHOP_CONTENT", str(cfg.content_dir))
    total = len(cfg.build.steps)
    for step in cfg.build.steps:
        console.heading("[%d/%d] %s" % (step.index, total, step.describe()))
        result = {"step": step.describe(), "type": step.kind}
        if step.kind == "run":
            _run_step(step, cfg, console, env)
        elif step.kind == "copy":
            result["copied"] = _copy_step(step, cfg, console, env)
        elif step.kind == "clean":
            target = _resolve(step.spec, cfg, env)
            _ensure_inside_root(target, cfg, "clean")
            console.debug("clean %s" % target)
            if target.exists():
                shutil.rmtree(str(target))
        elif step.kind == "cs2":
            from . import cs2

            try:
                result.update(cs2.build(step.spec, cfg, console, env, clean=clean))
            except cs2.Cs2Error as exc:
                raise BuildError(str(exc), **exc.data)
        results.append(result)
    return results


def _run_step(step, cfg, console, env):
    argv = [expand(a, cfg, env) for a in step.spec]
    program = argv[0]
    if "/" in program or "\\" in program:
        program_path = _resolve(program, cfg, env)
        if not program_path.is_file():
            raise BuildError("build program not found: %s" % program_path)
        argv[0] = str(program_path)
    else:
        found = shutil.which(program)
        if not found:
            raise BuildError("build program '%s' was not found on PATH" % program)
        argv[0] = found
    cwd = _resolve(step.cwd, cfg, env) if step.cwd else cfg.root
    console.debug("run (cwd=%s): %s" % (cwd, subprocess.list2cmdline(argv)))
    try:
        completed = subprocess.run(argv, cwd=str(cwd), env=env)
    except OSError as exc:
        raise BuildError("could not start %s: %s" % (argv[0], exc))
    if completed.returncode != 0:
        raise BuildError("'%s' exited with code %d" % (step.describe(), completed.returncode))


def _copy_step(step, cfg, console, env):
    source = _resolve(step.spec["from"], cfg, env)
    dest = _resolve(step.spec["to"], cfg, env)
    excludes = step.spec.get("exclude", [])
    if not source.is_dir():
        raise BuildError("copy source directory does not exist: %s" % source)
    _ensure_inside_root(dest, cfg, "copy into")
    if dest == source or source in dest.parents:
        raise BuildError("copy destination %s must not be inside the source %s" % (dest, source))

    def ignored(rel):
        parts = rel.split("/")
        return any(fnmatch.fnmatch(rel, pat) or any(fnmatch.fnmatch(p, pat) for p in parts) for pat in excludes)

    copied = 0
    for dirpath, dirnames, filenames in os.walk(str(source)):
        rel_dir = Path(dirpath).relative_to(source).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [d for d in dirnames if not ignored(rel_dir + d)]
        for filename in filenames:
            rel = rel_dir + filename
            if ignored(rel):
                continue
            target = dest / rel
            copied += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(os.path.join(dirpath, filename), str(target))
    console.debug("copied %d file(s) from %s to %s" % (copied, source, dest))
    return copied
