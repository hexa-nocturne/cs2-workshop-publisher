"""Readiness checks shared by `doctor`, `validate`, `build` and `update`."""

import os
import shutil
import sys
from pathlib import Path

from . import platforms, vdf
from .credentials import CredentialError, resolve_credentials

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"
PREVIEW_MAX_BYTES = 1024 * 1024
PREVIEW_SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
}
# Files that must never be uploaded to the Workshop.
SENSITIVE_NAMES = (".env", "config.vdf", "loginusers.vdf", "workshop.json", "steam_credentials")
SENSITIVE_PREFIXES = (".env.", "ssfn", "steam_credentials")


class Report:
    def __init__(self):
        self.items = []

    def add(self, name, status, detail=""):
        self.items.append({"check": name, "status": status, "detail": detail})

    @property
    def failures(self):
        return [i for i in self.items if i["status"] == FAIL]

    @property
    def warnings(self):
        return [i for i in self.items if i["status"] == WARN]

    @property
    def ok(self):
        return not self.failures

    def print(self, console):
        width = max([len(i["check"]) for i in self.items] + [10]) + 2
        for item in self.items:
            console.status(item["check"], item["status"], item["detail"], width=width)


def display_path(path, base=None):
    """Show paths relative to the project when possible (keeps logs free of home directories)."""
    try:
        return "./" + Path(path).resolve().relative_to(Path(base).resolve()).as_posix()
    except (ValueError, TypeError, OSError):
        return str(path)


def check_system(report):
    distro = platforms.linux_distribution() if platforms.current_os() == "linux" else None
    label = "%s %s" % (platforms.os_label(), platforms.architecture())
    if distro:
        label += " (%s)" % distro
    status = OK if platforms.current_os() == "linux" and platforms.architecture() == "x86_64" else INFO
    report.add("OS", status, label)
    report.add("Python", OK if sys.version_info >= (3, 8) else FAIL, sys.version.split()[0])


def check_config_values(cfg, report, mode):
    report.add("App ID", OK, str(cfg.app_id))
    if cfg.published_file_id:
        detail = cfg.published_file_id
        status = OK
        if mode == "publish":
            status = FAIL
            detail += " (already published; use `workshop update`)"
        report.add("Workshop ID", status, detail)
    else:
        status = FAIL if mode == "update" else INFO
        detail = "not set: this would be a first publish (`workshop publish`)"
        report.add("Workshop ID", status, detail)
    if mode == "publish" and not cfg.title:
        report.add("Title", FAIL, "'title' is required to create a new Workshop item")
    elif cfg.title and len(cfg.title) > vdf.TITLE_MAX:
        report.add("Title", FAIL, "longer than %d characters" % vdf.TITLE_MAX)
    if cfg.description and len(cfg.description) > vdf.TEXT_MAX:
        report.add("Description", FAIL, "longer than %d characters" % vdf.TEXT_MAX)
    if cfg.visibility is not None:
        report.add("Visibility", INFO, vdf.VISIBILITY_NAMES[cfg.visibility])
    for warning in cfg.warnings:
        report.add("Config", WARN, warning)


def check_content(cfg, report, built_by_pipeline=False):
    content = cfg.content_dir
    shown = display_path(content, cfg.root)
    if not content.exists():
        if built_by_pipeline:
            report.add("Content", WARN, "%s does not exist yet (it is produced by the build)" % shown)
        else:
            report.add("Content", FAIL, "Workshop content directory does not exist: %s" % content)
        return
    if not content.is_dir():
        report.add("Content", FAIL, "contentDirectory is not a directory: %s" % content)
        return
    root = Path(os.path.abspath(str(cfg.root)))
    if Path(os.path.abspath(str(content))) == root:
        report.add("Content", FAIL, "contentDirectory must not be the project root (it would upload the repository)")
        return
    files, total, sensitive, links = 0, 0, [], []
    for dirpath, dirnames, filenames in os.walk(str(content)):
        if ".git" in dirnames:
            sensitive.append(os.path.join(dirpath, ".git"))
            dirnames.remove(".git")
        for name in filenames:
            full = os.path.join(dirpath, name)
            lower = name.lower()
            if lower in SENSITIVE_NAMES or lower.startswith(SENSITIVE_PREFIXES):
                sensitive.append(full)
            if os.path.islink(full):
                links.append(full)
            files += 1
            try:
                total += os.path.getsize(full)
            except OSError:
                pass
    if sensitive:
        report.add("Content", FAIL, "refusing to upload sensitive files: %s" % ", ".join(sensitive[:5]))
        return
    if files == 0:
        if built_by_pipeline:
            report.add("Content", WARN, "%s is empty (it is produced by the build)" % shown)
        else:
            report.add("Content", FAIL, "Workshop content directory is empty: %s" % content)
        return
    from .manifest import format_size

    report.add("Content", OK, "%s (%d files, %s)" % (shown, files, format_size(total)))
    if links:
        report.add("Content", WARN, "%d symbolic link(s) in content; SteamCMD uploads their targets" % len(links))
    if cfg.app_id == 730:
        vpks = [p for p in content.rglob("*.vpk")]
        if not vpks:
            report.add("Content", WARN, "no .vpk found; CS2 Workshop addons are normally a packed .vpk")


def check_preview(cfg, report, required=False):
    preview = cfg.preview_image
    if preview is None:
        report.add("Preview", FAIL if required else INFO, "no previewImage configured")
        return
    shown = display_path(preview, cfg.root)
    if not preview.is_file():
        report.add("Preview", FAIL, "preview image does not exist: %s" % preview)
        return
    ext = preview.suffix.lower()
    if ext not in PREVIEW_SIGNATURES:
        report.add("Preview", FAIL, "preview image must be .png, .jpg or .gif: %s" % shown)
        return
    size = preview.stat().st_size
    if size >= PREVIEW_MAX_BYTES:
        report.add("Preview", FAIL, "preview image exceeds the supported size (%d bytes; must be under 1 MB)" % size)
        return
    with open(str(preview), "rb") as handle:
        head = handle.read(16)
    if not any(head.startswith(sig) for sig in PREVIEW_SIGNATURES[ext]):
        report.add("Preview", FAIL, "%s does not look like a valid %s image" % (shown, ext[1:].upper()))
        return
    report.add("Preview", OK, "%s (%d KB)" % (shown, max(1, size // 1024)))


def check_build(cfg, report, env, strict_runtime=False):
    """Check build configuration; strict_runtime makes a missing compiler a failure."""
    if not cfg.build or not cfg.build.steps:
        report.add("Build", INFO, "no build steps (contentDirectory is uploaded as-is)")
        return
    unsupported = cfg.build.unsupported_steps()
    if unsupported:
        report.add("Build", WARN, "steps not available on this OS: %s" % ", ".join(s.describe() for s in unsupported))
    else:
        report.add("Build", OK, "%d step(s)" % len(cfg.build.steps))
    from . import cs2
    from .build import BuildError

    for step in cfg.build.steps:
        if step.kind != "cs2":
            continue
        settings = step.spec
        try:
            paths = cs2.Cs2Paths(settings, cfg, env)
        except (BuildError, ValueError) as exc:
            report.add("CS2 build", FAIL if strict_runtime else WARN, str(exc))
            continue
        if not paths.source.is_dir():
            report.add("CS2 sources", FAIL, "addon source directory does not exist: %s" % paths.source)
            continue
        files = cs2.scan_source(paths.source)
        report.add("CS2 sources", OK if files else FAIL, "%s (%d files)" % (display_path(paths.source, cfg.root), len(files)))
        problems = cs2.check_runtime(settings, paths, env)
        if problems:
            for problem in problems:
                report.add("CS2 compiler", FAIL if strict_runtime else WARN, problem)
        else:
            runtime = "Wine" if cs2.use_wine(settings) else "native"
            report.add("CS2 compiler", OK, "%s (%s)" % (paths.compiler, runtime))
        if settings.reference_mode != "off" and files:
            refs, missing, have_base = cs2.check_references(settings, paths, files)
            if not have_base:
                report.add("References", WARN, "base game paks not found; stock CS2 references cannot be verified")
            if missing:
                status = FAIL if settings.reference_mode == "error" and have_base else WARN
                sample = "; ".join("%s:%d %s" % (m.source, m.line, m.text) for m in missing[:5])
                report.add("References", status, "%d unresolved: %s" % (len(missing), sample))
            else:
                report.add("References", OK, "%d reference(s) resolved" % len(refs))
        if cs2.use_wine(settings) and platforms.current_os() == "linux":
            if (settings.memory_max or settings.cpu_quota) and not shutil.which("systemd-run"):
                report.add("Resources", WARN, "systemd-run not found; memoryMax/cpuQuota limits will not be applied")
            else:
                report.add("Resources", INFO, "nice %d, ionice %s%s" % (
                    settings.nice, "idle" if settings.ionice_idle else "default",
                    ", MemoryMax=%s" % settings.memory_max if settings.memory_max else ""))


def check_steamcmd(report, env, required=True):
    location = platforms.find_steamcmd(env)
    if location.found:
        path = location.path
        executable = os.name == "nt" or os.access(str(path), os.X_OK)
        report.add("SteamCMD", OK if executable else FAIL,
                   "%s%s" % (path, "" if executable else " (not executable: chmod +x)"))
        if platforms.current_os() == "linux" and path.name == "steamcmd.sh" and not platforms.linux_32bit_runtime_present():
            report.add("SteamCMD", WARN, "32-bit runtime not detected; install with: sudo apt install lib32gcc-s1")
    else:
        detail = location.error or "not found (install instructions below; or set STEAMCMD_PATH)"
        report.add("SteamCMD", FAIL if required else WARN, detail)
    return location


def check_credentials(report, env, cfg=None, credentials_file=None, required=True, redactor=None):
    dotenv = (cfg.root / ".env") if cfg else None
    try:
        creds = resolve_credentials(env, credentials_file, dotenv, redactor)
    except CredentialError as exc:
        report.add("Authentication", FAIL, str(exc))
        return None
    if not creds.username:
        report.add(
            "Authentication",
            FAIL if required else WARN,
            "no Steam username: set STEAM_USERNAME or WORKSHOP_CREDENTIALS_FILE",
        )
        return creds
    report.add("Authentication", OK, creds.describe())
    return creds
