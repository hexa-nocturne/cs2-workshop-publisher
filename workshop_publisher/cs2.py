"""Counter-Strike 2 addon compile and pack stage.

Valve's Source 2 resource compiler (resourcecompiler.exe) only ships for
Windows and no open-source replacement can compile Panorama, sounds, models,
materials and textures. On Linux it is therefore run through Wine, headless,
from a Windows build of CS2 that SteamCMD downloads onto the server
(`workshop tools install`). Everything around it (source sync, incremental
state, reference checks, VPK packing, manifests) is native Python.

Layout used by the compiler:
  <tools>/content/csgo_addons/<addon>/...   uncompiled sources (synced from the repo)
  <tools>/game/csgo_addons/<addon>/...      compiled *_c resources
"""

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import manifest as manifest_mod
from . import references, vpk
from .platforms import current_os

ADDON_NAME_RE = re.compile(r"^[a-z0-9_]+$")
STATE_VERSION = 1

DEFAULT_ROOT_EXTENSIONS = [".xml", ".css", ".js", ".vsndevts", ".wav", ".mp3", ".vmdl", ".vmat", ".vtex", ".vpcf"]
DEFAULT_COMPILE = ["{compiler}", "-nop4", "-i", "{input}"]
DEFAULT_PACK_EXCLUDE = ["tools_*", "*.vpk", "_bakeresourcecache*", "*.log"]
DEFAULT_WINE_PREFIX = "~/.local/share/workshop-publisher/wineprefix"
SOURCE_SKIP = {".git", ".svn", ".DS_Store", "Thumbs.db", "desktop.ini"}


class Cs2Error(Exception):
    pass


def _opt_list(spec, key, default, where):
    value = spec.get(key, default)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError("%s: '%s' must be a list of strings" % (where, key))
    return list(value)


class Cs2Settings:
    KNOWN = {
        "addon", "source", "tools", "vpk", "runtime", "wine", "compile", "rootExtensions", "extraRoots",
        "packExclude", "referenceCheck", "resources", "cacheDirectory", "timeoutSeconds",
        "prebuilt", "vpkChunkMB", "jobs", "compileMode", "compileBatch", "batchSize",
    }

    def __init__(self, spec, where="cs2"):
        if not isinstance(spec, dict):
            raise ValueError("%s must be an object" % where)
        unknown = sorted(set(spec) - self.KNOWN)
        if unknown:
            raise ValueError("%s: unknown key(s): %s" % (where, ", ".join(unknown)))
        self.addon = spec.get("addon")
        if not isinstance(self.addon, str) or not ADDON_NAME_RE.match(self.addon):
            raise ValueError("%s: 'addon' must be a lowercase addon name ([a-z0-9_])" % where)
        self.source = spec.get("source", "./content")
        self.tools = spec.get("tools", "${CS2_TOOLS_DIR}")
        self.vpk = spec.get("vpk")
        if not isinstance(self.vpk, str) or not self.vpk.lower().endswith(".vpk"):
            raise ValueError("%s: 'vpk' is required and must be a path ending in .vpk" % where)
        self.runtime = spec.get("runtime", "auto")
        if self.runtime not in ("auto", "native", "wine"):
            raise ValueError("%s: 'runtime' must be auto, native or wine" % where)
        wine = spec.get("wine", {})
        if not isinstance(wine, dict):
            raise ValueError("%s: 'wine' must be an object" % where)
        self.wine_binary = wine.get("binary", "wine")
        self.wine_prefix = wine.get("prefix", DEFAULT_WINE_PREFIX)
        self.xvfb = wine.get("xvfb", "auto")
        if self.xvfb not in ("auto", "always", "never"):
            raise ValueError("%s: 'wine.xvfb' must be auto, always or never" % where)
        self.compile = _opt_list(spec, "compile", DEFAULT_COMPILE, where)
        if "{input}" not in self.compile:
            raise ValueError("%s: 'compile' must contain the {input} placeholder" % where)
        self.root_extensions = [e.lower() for e in _opt_list(spec, "rootExtensions", DEFAULT_ROOT_EXTENSIONS, where)]
        self.extra_roots = _opt_list(spec, "extraRoots", [], where)
        self.pack_exclude = _opt_list(spec, "packExclude", DEFAULT_PACK_EXCLUDE, where)
        ref = spec.get("referenceCheck", {})
        if not isinstance(ref, dict):
            raise ValueError("%s: 'referenceCheck' must be an object" % where)
        self.reference_mode = ref.get("mode", "error")
        if self.reference_mode not in ("error", "warn", "off"):
            raise ValueError("%s: 'referenceCheck.mode' must be error, warn or off" % where)
        self.reference_ignore = _opt_list(ref, "ignore", [], where + ".referenceCheck")
        self.reference_aliases = ref.get("panoramaAliases", {})
        self.base_vpks = _opt_list(ref, "baseVpks", ["{tools}/game/csgo/pak01_dir.vpk", "{tools}/game/core/pak01_dir.vpk"], where)
        res = spec.get("resources", {})
        if not isinstance(res, dict):
            raise ValueError("%s: 'resources' must be an object" % where)
        self.nice = int(res.get("nice", 10))
        self.ionice_idle = bool(res.get("ioniceIdle", True))
        self.memory_max = res.get("memoryMax")
        self.cpu_quota = res.get("cpuQuota")
        self.cache_directory = spec.get("cacheDirectory", "./build-cache")
        self.timeout = int(spec.get("timeoutSeconds", 1800))
        # Already-compiled files packed as-is (e.g. third-party models with no sources).
        self.prebuilt = _opt_list(spec, "prebuilt", [], where)
        self.vpk_chunk_mb = int(spec.get("vpkChunkMB", 100))
        if self.vpk_chunk_mb < 1:
            raise ValueError("%s: 'vpkChunkMB' must be at least 1" % where)
        self.jobs = int(spec.get("jobs", 1))
        if not 1 <= self.jobs <= 64:
            raise ValueError("%s: 'jobs' must be between 1 and 64" % where)
        self.compile_mode = spec.get("compileMode", "per-file")
        if self.compile_mode not in ("per-file", "batch"):
            raise ValueError("%s: 'compileMode' must be per-file or batch" % where)
        self.compile_batch = _opt_list(spec, "compileBatch", [], where)
        if self.compile_mode == "batch" and "{filelist}" not in " ".join(self.compile_batch):
            raise ValueError("%s: batch mode needs a 'compileBatch' command containing {filelist}" % where)
        self.batch_size = int(spec.get("batchSize", 200))


class Cs2Paths:
    def __init__(self, settings, cfg, env):
        from .build import _resolve

        self.source = _resolve(settings.source, cfg, env)
        self.tools = _resolve(settings.tools, cfg, env)
        self.vpk = _resolve(settings.vpk.replace("{publishedFileId}", cfg.published_file_id or "unpublished"), cfg, env)
        self.cache = _resolve(settings.cache_directory, cfg, env)
        self.compiler = self.tools / "game" / "bin" / "win64" / "resourcecompiler.exe"
        self.addon_content = self.tools / "content" / "csgo_addons" / settings.addon
        self.addon_game = self.tools / "game" / "csgo_addons" / settings.addon
        self.state = self.cache / "cs2" / ("%s-state.json" % settings.addon)
        self.manifests = self.cache / "manifests"
        self.logs = self.cache / "logs"
        self.wine_prefix = Path(os.path.expanduser(settings.wine_prefix))
        self.base_vpks = [Path(p.replace("{tools}", str(self.tools))) for p in settings.base_vpks]
        self.prebuilt = [_resolve(p, cfg, env) for p in settings.prebuilt]


# ---------------------------------------------------------------------------
# Source scanning and incremental state


def scan_source(source_dir):
    files = {}
    source_dir = Path(source_dir)
    for dirpath, dirnames, filenames in os.walk(str(source_dir)):
        dirnames[:] = sorted(d for d in dirnames if d not in SOURCE_SKIP and not d.startswith("."))
        for name in sorted(filenames):
            if name in SOURCE_SKIP or name.startswith("."):
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(source_dir).as_posix()
            files[rel] = full
    return files


def file_digest(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_state(path):
    try:
        with open(str(path), encoding="utf-8") as handle:
            state = json.load(handle)
        if state.get("version") == STATE_VERSION:
            return state
    except (OSError, ValueError):
        pass
    return None


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def tool_fingerprint(paths, settings):
    try:
        stat = paths.compiler.stat()
        compiler = "%d:%d" % (stat.st_size, int(stat.st_mtime))
    except OSError:
        compiler = "missing"
    return hashlib.sha256(json.dumps([compiler, settings.compile, settings.addon]).encode()).hexdigest()


def plan_changes(current, previous_state, fingerprint, force_full=False):
    """Return (full_rebuild, reason, changed, deleted)."""
    if force_full:
        return True, "clean build requested", sorted(current), []
    if not previous_state:
        return True, "no previous build state", sorted(current), []
    if previous_state.get("fingerprint") != fingerprint:
        return True, "compiler or compile settings changed", sorted(current), []
    old = previous_state.get("files", {})
    deleted = sorted(set(old) - set(current))
    changed = sorted(p for p, digest in current.items() if old.get(p) != digest)
    if deleted:
        # Stale compiled outputs cannot be mapped back reliably, so start clean.
        return True, "%d source file(s) were removed" % len(deleted), sorted(current), deleted
    return False, "incremental", changed, []


def select_roots(settings, all_files, changed, refs, full):
    def is_root(rel):
        lower = rel.lower()
        if any(fnmatch.fnmatch(lower, pat.lower()) for pat in settings.extra_roots):
            return True
        if lower.endswith((".xml", ".css", ".js")) and not lower.startswith("panorama/"):
            return False
        return os.path.splitext(lower)[1] in settings.root_extensions

    if full:
        return sorted(r for r in all_files if is_root(r))
    targets = {r for r in changed if is_root(r)}
    targets |= {r for r in references.dependents(refs, changed) if is_root(r)}
    return sorted(targets)


def read_texts(files):
    texts = {}
    for rel, path in files.items():
        if rel.lower().endswith(references.PANORAMA_TEXT + references.KV3_TEXT):
            try:
                texts[rel] = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
    return texts


def base_game_files(paths, console=None):
    names = set()
    for base in paths.base_vpks:
        if base.is_file():
            try:
                names.update(e.path.lower() for e in vpk.list_files(base))
            except vpk.VpkError as exc:
                if console:
                    console.warn("cannot read base game pak %s: %s" % (base, exc))
    return names


def check_references(settings, paths, files, console=None):
    refs = references.scan_sources(read_texts(files), settings.reference_aliases)
    base = base_game_files(paths, console) | set(collect_prebuilt(paths))
    missing = references.find_missing(refs, files.keys(), base, settings.reference_ignore)
    return refs, missing, bool(base)


# ---------------------------------------------------------------------------
# Runtime (native / Wine) and resource limits


def use_wine(settings, os_name=None):
    os_name = os_name or current_os()
    if settings.runtime == "auto":
        return os_name != "windows"
    return settings.runtime == "wine"


def to_wine_path(path):
    """Wine maps the Linux root filesystem to drive Z: in every prefix by default."""
    return "Z:" + str(path).replace("/", "\\")


def runtime_prefix(settings, env, which=shutil.which):
    """Command prefix that lowers priority and caps memory/CPU for compiler processes."""
    prefix = []
    if current_os() == "windows":
        return prefix
    if (settings.memory_max or settings.cpu_quota) and which("systemd-run"):
        prefix += ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
        if settings.memory_max:
            prefix += ["-p", "MemoryMax=%s" % settings.memory_max]
        if settings.cpu_quota:
            prefix += ["-p", "CPUQuota=%s" % settings.cpu_quota]
        prefix.append("--")
    if settings.nice and which("nice"):
        prefix += ["nice", "-n", str(settings.nice)]
    if settings.ionice_idle and which("ionice"):
        prefix += ["ionice", "-c", "3"]
    return prefix


def wine_environment(settings, paths, env):
    wenv = dict(env)
    wenv["WINEPREFIX"] = str(paths.wine_prefix)
    wenv.setdefault("WINEDEBUG", "-all")
    wenv.setdefault("WINEARCH", "win64")
    # Never let Wine pop up its Mono/Gecko installers on a headless server.
    wenv.setdefault("WINEDLLOVERRIDES", "mscoree,mshtml=")
    return wenv


def compiler_argv(settings, paths, env, template_values, which=shutil.which, template=None):
    wine = use_wine(settings)
    convert = to_wine_path if wine else str
    values = {
        "{compiler}": convert(paths.compiler),
        "{addon_content}": convert(paths.addon_content),
        "{addon_game}": convert(paths.addon_game),
        "{tools}": convert(paths.tools),
    }
    values.update({k: convert(v) for k, v in template_values.items()})
    argv = []
    for part in (template or settings.compile):
        for key, value in values.items():
            part = part.replace(key, value)
        argv.append(part)
    prefix = runtime_prefix(settings, env, which)
    if wine:
        wine_bin = which(settings.wine_binary) or settings.wine_binary
        use_xvfb = settings.xvfb == "always" or (settings.xvfb == "auto" and not env.get("DISPLAY"))
        if use_xvfb:
            xvfb = which("xvfb-run")
            if not xvfb:
                raise Cs2Error("xvfb-run is required for headless Wine. Install it with: sudo apt install xvfb")
            prefix += [xvfb, "-a"]
        argv = [wine_bin] + argv
    return prefix + argv


def check_runtime(settings, paths, env, which=shutil.which):
    """Return a list of human-readable problems with the compiler runtime."""
    problems = []
    if not paths.compiler.is_file():
        problems.append(
            "resourcecompiler.exe not found at %s. Download the CS2 tools with: ./workshop tools install" % paths.compiler
        )
    if use_wine(settings):
        if not which(settings.wine_binary):
            problems.append("Wine ('%s') is not installed. Install with: sudo apt install wine wine64" % settings.wine_binary)
        if settings.xvfb != "never" and not env.get("DISPLAY") and not which("xvfb-run"):
            problems.append("xvfb-run is not installed (needed for headless Wine). Install with: sudo apt install xvfb")
        if not (paths.wine_prefix / "system.reg").is_file():
            problems.append("Wine prefix %s is not initialised. Run: ./workshop tools setup-wine" % paths.wine_prefix)
    return problems


def init_wine_prefix(settings, paths, env, console):
    wenv = wine_environment(settings, paths, env)
    paths.wine_prefix.mkdir(parents=True, exist_ok=True)
    wineboot = shutil.which("wineboot") or "wineboot"
    argv = [wineboot, "--init"]
    if settings.xvfb != "never" and not env.get("DISPLAY") and shutil.which("xvfb-run"):
        argv = [shutil.which("xvfb-run"), "-a"] + argv
    console.debug("initialising Wine prefix: %s" % " ".join(argv))
    try:
        completed = subprocess.run(argv, env=wenv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Cs2Error("could not initialise the Wine prefix: %s" % exc)
    console.debug(completed.stdout.decode("utf-8", "replace"))
    if completed.returncode != 0:
        raise Cs2Error("wineboot failed with exit code %d (re-run with --verbose)" % completed.returncode)
    # Keep one wineserver alive between compiler invocations.
    subprocess.run([shutil.which("wineserver") or "wineserver", "-w"], env=wenv, timeout=600)


# ---------------------------------------------------------------------------
# Build


def _mirror(files, digests, changed, deleted, paths, full, console):
    target = paths.addon_content
    _ensure_under(target, paths.tools / "content" / "csgo_addons")
    if full and target.exists():
        shutil.rmtree(str(target))
    target.mkdir(parents=True, exist_ok=True)
    for rel in (sorted(files) if full else changed):
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(files[rel]), str(dest))
    for rel in deleted:
        stale = target / rel
        if stale.is_file():
            stale.unlink()
    console.debug("synced %d source file(s) into %s" % (len(files) if full else len(changed), target))


def _ensure_under(path, parent):
    path, parent = Path(os.path.abspath(str(path))), Path(os.path.abspath(str(parent)))
    if parent not in path.parents:
        raise Cs2Error("refusing to modify %s: not inside %s" % (path, parent))


def _run_compiler(settings, paths, env, label, console, log, template_values, template=None, lock=None):
    argv = compiler_argv(settings, paths, env, template_values, template=template)
    run_env = wine_environment(settings, paths, env) if use_wine(settings) else env
    console.debug("compile: %s" % subprocess.list2cmdline(argv))
    try:
        completed = subprocess.run(
            argv, env=run_env, cwd=str(paths.compiler.parent), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=settings.timeout,
        )
        code = completed.returncode
        output = completed.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        code, output = -1, "timed out after %d seconds" % settings.timeout
    except OSError as exc:
        raise Cs2Error("could not start the compiler: %s" % exc)
    errors = [line.strip() for line in output.splitlines()
              if re.search(r"\berror\b", line, re.I) and not re.search(r"\b0 errors?\b", line, re.I)]
    with lock or threading.Lock():
        log.write("=== %s (exit %d)\n%s\n" % (label, code, output))
        log.flush()
        if console.verbose:
            console.write(output if output.endswith("\n") else output + "\n")
    return code, errors


def _compile_all(settings, paths, env, roots, console, log):
    """Compile ``roots`` honouring ``jobs`` and batch mode. Returns a list of failures."""
    lock = threading.Lock()
    counter = {"done": 0}
    failures = []

    def check(rel, code, errors):
        outputs = expected_outputs(rel)
        produced = any((paths.addon_game / out).is_file() for out in outputs)
        if code != 0 or not produced:
            detail = "exit code %d" % code if code != 0 else "no %s was produced" % " / ".join(outputs)
            with lock:
                failures.append({"file": rel, "detail": detail, "errors": errors[:5]})

    if settings.compile_mode == "batch":
        convert = to_wine_path if use_wine(settings) else str
        batches = [roots[i:i + settings.batch_size] for i in range(0, len(roots), settings.batch_size)]

        def work(numbered):
            number, batch = numbered
            listing = paths.cache / "cs2" / ("filelist-%03d.txt" % number)
            listing.parent.mkdir(parents=True, exist_ok=True)
            listing.write_text("\n".join(convert(paths.addon_content / rel) for rel in batch) + "\n", encoding="utf-8")
            code, errors = _run_compiler(settings, paths, env, "batch %d (%d files)" % (number, len(batch)), console,
                                         log, {"{filelist}": listing}, template=settings.compile_batch, lock=lock)
            with lock:
                counter["done"] += len(batch)
                console.info("  [%d/%d] batch %d" % (counter["done"], len(roots), number))
            for rel in batch:
                check(rel, code, errors)

        items = list(enumerate(batches, 1))
    else:
        def work(rel):
            code, errors = _run_compiler(settings, paths, env, rel, console, log,
                                         {"{input}": paths.addon_content / rel}, lock=lock)
            with lock:
                counter["done"] += 1
                console.info("  [%d/%d] %s" % (counter["done"], len(roots), rel))
            check(rel, code, errors)

        items = roots

    if settings.jobs == 1:
        for item in items:
            work(item)
    else:
        with ThreadPoolExecutor(max_workers=settings.jobs) as pool:
            for future in [pool.submit(work, item) for item in items]:
                future.result()
    return sorted(failures, key=lambda f: f["file"])


def expected_outputs(rel):
    return references._variants(rel.lower())[1]


def build(settings, cfg, console, env=None, clean=False):
    env = dict(os.environ if env is None else env)
    paths = Cs2Paths(settings, cfg, env)
    started = time.time()
    result = {"addon": settings.addon, "vpk": str(paths.vpk)}

    for folder in paths.prebuilt:
        if not folder.is_dir():
            raise Cs2Error("prebuilt directory does not exist: %s" % folder)
    if paths.source.is_dir():
        files = scan_source(paths.source)
    elif paths.prebuilt:
        files = {}
    else:
        raise Cs2Error("addon source directory does not exist: %s" % paths.source)
    if not files and not paths.prebuilt:
        raise Cs2Error("addon source directory is empty: %s" % paths.source)

    console.info("Checking asset references...")
    refs, missing, have_base = check_references(settings, paths, files, console)
    result["missingReferences"] = [m.to_dict() for m in missing]
    if not have_base:
        console.warn("base game paks not found; references to stock CS2 assets cannot be verified")
    if missing and settings.reference_mode != "off":
        for ref in missing[:50]:
            console.line("  %s:%d  %s" % (ref.source, ref.line, ref.text))
        if len(missing) > 50:
            console.line("  ... and %d more" % (len(missing) - 50))
        message = "%d reference(s) point at files that are not in the addon or the base game" % len(missing)
        if settings.reference_mode == "error":
            raise Cs2Error(message + " (set build step referenceCheck.mode to 'warn' or add 'ignore' patterns if intended)")
        console.warn(message)

    digests = {rel: file_digest(path) for rel, path in files.items()}
    fingerprint = tool_fingerprint(paths, settings)
    full, reason, changed, deleted = plan_changes(digests, load_state(paths.state), fingerprint, clean)
    roots = select_roots(settings, files, changed, refs, full)
    result.update({"fullRebuild": full, "reason": reason, "changedSources": len(changed), "compiled": roots})
    console.info("%s build: %s; %d resource(s) to compile" % ("Full" if full else "Incremental", reason, len(roots)))
    if roots:
        problems = check_runtime(settings, paths, env)
        if problems:
            raise Cs2Error("the CS2 compiler is not ready:\n  - " + "\n  - ".join(problems))

    if files:
        try:
            _mirror(files, digests, changed, deleted, paths, full, console)
            if full and paths.addon_game.exists():
                _ensure_under(paths.addon_game, paths.tools / "game" / "csgo_addons")
                shutil.rmtree(str(paths.addon_game))
            paths.addon_game.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise Cs2Error("cannot write to the CS2 tools directory %s: %s" % (paths.tools, exc))

    failures = []
    if roots:
        paths.logs.mkdir(parents=True, exist_ok=True)
        log_path = paths.logs / ("compile-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
        result["compileLog"] = str(log_path)
        if use_wine(settings):
            subprocess.run(
                [shutil.which("wineserver") or "wineserver", "-p", "300"],
                env=wine_environment(settings, paths, env),
            )
        with open(str(log_path), "w", encoding="utf-8") as log:
            failures = _compile_all(settings, paths, env, roots, console, log)
        if failures:
            # Don't record state: the next run must retry these files.
            for failure in failures:
                console.error("%s: %s" % (failure["file"], failure["detail"]))
                for line in failure["errors"]:
                    console.line("    " + line)
            result["failures"] = failures
            raise Cs2Error("%d resource(s) failed to compile; see %s" % (len(failures), log_path))

    save_state(paths.state, {"version": STATE_VERSION, "fingerprint": fingerprint, "files": digests})

    # Without sources nothing is compiled, so stale output from earlier builds must not be packed.
    compiled_files = collect_pack_files(settings, paths) if files else {}
    prebuilt_files = collect_prebuilt(paths)
    overridden = sorted(set(compiled_files) & set(prebuilt_files))
    pack_files = dict(prebuilt_files)
    pack_files.update(compiled_files)
    if not pack_files:
        raise Cs2Error("nothing to pack: no compiled files in %s and no prebuilt files" % paths.addon_game)
    if overridden:
        console.warn("%d prebuilt file(s) replaced by freshly compiled versions" % len(overridden))
        for rel in overridden[:20]:
            console.debug("compiled replaces prebuilt: %s" % rel)
    console.info("Packing %d file(s) (%d compiled, %d prebuilt) into %s" % (
        len(pack_files), len(compiled_files), len(pack_files) - len(compiled_files), paths.vpk))
    written = vpk.write(paths.vpk, pack_files, chunk_size=settings.vpk_chunk_mb * 1024 * 1024)
    vpk.verify(paths.vpk)
    result.update({"compiledFiles": len(compiled_files), "prebuiltFiles": len(pack_files) - len(compiled_files),
                   "overriddenPrebuilt": overridden, "vpkFiles": written["files"], "vpkChunks": written["chunks"]})

    previous = manifest_mod.load(paths.manifests / "latest.json")
    current = manifest_mod.from_vpk(paths.vpk)
    changes = manifest_mod.diff(previous, current)
    manifest_path = manifest_mod.save(current, paths.manifests)
    result.update(
        {
            "vpkSize": current["vpkSize"],
            "fileCount": current["fileCount"],
            "manifest": str(manifest_path),
            "diff": {k: v for k, v in changes.items()},
            "seconds": round(time.time() - started, 1),
        }
    )
    console.ok(
        "Packed %s: %d files, %s (+%d added, -%d removed, ~%d changed since last build)"
        % (paths.vpk.name, current["fileCount"], manifest_mod.format_size(current["vpkSize"]),
           len(changes["added"]), len(changes["removed"]), len(changes["changed"]))
    )
    return result


def collect_pack_files(settings, paths):
    pack = {}
    root = paths.addon_game
    if not root.is_dir():
        return pack
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if not rel.endswith("_c"):
                continue
            if any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat) for pat in settings.pack_exclude):
                continue
            pack[rel.lower()] = full
    return pack


def collect_prebuilt(paths):
    """Files from the prebuilt folders keyed by lowercase pack path (later folders win)."""
    pack = {}
    for folder in paths.prebuilt:
        if folder.is_dir():
            for rel, full in scan_source(folder).items():
                pack[rel.lower()] = full
    return pack
