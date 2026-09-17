"""Command-line interface: workshop <command> [options]."""

import argparse
import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__, checks, platforms, steamapi, steamcmd, vdf
from .build import BuildError, run_build
from .config import ConfigError, default_config_path, load_config, save_published_file_id
from .console import Console, Redactor
from .credentials import CredentialError, resolve_credentials

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 3
EXIT_AUTH = 4
EXIT_UPLOAD = 5
EXIT_BUILD = 6
EXIT_VERIFY = 7
EXIT_LOCKED = 8

OUTCOME_EXIT = {
    steamcmd.Outcome.AUTH: EXIT_AUTH,
    steamcmd.Outcome.STEAM_GUARD: EXIT_AUTH,
    steamcmd.Outcome.LICENSE: EXIT_AUTH,
    steamcmd.Outcome.ENVIRONMENT: EXIT_ENVIRONMENT,
}

HOME_DIR = Path(__file__).resolve().parent.parent


class CommandError(Exception):
    def __init__(self, message, exit_code=EXIT_FAILURE, **data):
        super().__init__(message)
        self.exit_code = exit_code
        self.data = data


class Context:
    def __init__(self, args):
        self.args = args
        self.env = os.environ
        self.redactor = Redactor()
        self.redactor.add_from_env(self.env)
        stream = sys.stderr if args.json else sys.stdout
        color = False if args.no_color else None
        self.console = Console(verbose=args.verbose, color=color, stream=stream, redactor=self.redactor)
        self.result = {"command": args.command, "ok": False}
        self._cfg = None

    @property
    def config_path(self):
        if self.args.config:
            return Path(self.args.config).expanduser()
        return default_config_path(self.env, HOME_DIR)

    def config(self):
        if self._cfg is None:
            try:
                self._cfg = load_config(self.config_path)
            except ConfigError as exc:
                raise CommandError(
                    "invalid configuration %s:\n  - %s" % (exc.path, "\n  - ".join(exc.problems)),
                    EXIT_FAILURE,
                    problems=exc.problems,
                )
        return self._cfg


# ---------------------------------------------------------------------------
# helpers


@contextlib.contextmanager
def pipeline_lock(cfg):
    """Prevent two builds/uploads of the same project from running at once."""
    lock_dir = cfg.root / "build-cache"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = open(str(lock_dir / ".workshop.lock"), "a+")
    try:
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CommandError("another workshop build or upload is already running for this project", EXIT_LOCKED)
        yield
    finally:
        handle.close()


def require_steamcmd(ctx):
    location = platforms.find_steamcmd(ctx.env)
    if not location.found:
        message = location.error or platforms.install_help()
        if location.error:
            message += "\n\n" + platforms.install_help()
        raise CommandError(message, EXIT_ENVIRONMENT)
    ctx.console.debug("SteamCMD: %s (%s)" % (location.path, location.source))
    return location.path


def require_credentials(ctx, cfg=None):
    dotenv = (cfg.root / ".env") if cfg else None
    try:
        creds = resolve_credentials(ctx.env, ctx.args.credentials_file, dotenv, ctx.redactor)
    except CredentialError as exc:
        raise CommandError(str(exc), EXIT_AUTH)
    if not creds.username:
        raise CommandError(
            "No Steam username configured.\n"
            "Set STEAM_USERNAME, or point WORKSHOP_CREDENTIALS_FILE at a KEY=VALUE file containing STEAM_USERNAME\n"
            "(and optionally STEAM_PASSWORD). Anonymous logins cannot upload Workshop items.",
            EXIT_AUTH,
        )
    ctx.console.debug("authentication: %s" % creds.describe())
    return creds


def read_change_note(args):
    if args.change_note is not None and args.change_note_file:
        raise CommandError("use either --change-note or --change-note-file, not both", EXIT_USAGE)
    if args.change_note_file:
        try:
            return Path(args.change_note_file).read_text(encoding="utf-8-sig").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise CommandError("cannot read change note file: %s" % exc, EXIT_USAGE)
    return args.change_note


def parse_visibility(value):
    if value is None:
        return None
    from .config import VISIBILITY_ALIASES

    key = value.strip().lower()
    if key in VISIBILITY_ALIASES:
        return VISIBILITY_ALIASES[key]
    if key.isdigit() and int(key) in vdf.VISIBILITY_NAMES:
        return int(key)
    raise CommandError("--visibility must be public, friends-only, private or unlisted", EXIT_USAGE)


def fail_on_report(ctx, report, message):
    if not report.ok:
        report.print(ctx.console)
        ctx.result["checks"] = report.items
        raise CommandError(message, EXIT_FAILURE)


def content_size(path):
    total = 0
    for dirpath, _, filenames in os.walk(str(path)):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# commands


def cmd_doctor(ctx):
    console = ctx.console
    report = checks.Report()
    console.heading("Workshop Publisher Doctor")
    console.line()
    checks.check_system(report)
    location = checks.check_steamcmd(report, ctx.env)
    cfg = None
    try:
        cfg = ctx.config()
        report.add("Config", checks.OK, checks.display_path(cfg.path, Path.cwd()))
    except CommandError as exc:
        problems = exc.data.get("problems") or [str(exc)]
        report.add("Config", checks.FAIL, "%s: %s" % (ctx.config_path, problems[0].splitlines()[0]))
        for problem in problems[1:]:
            report.add("Config", checks.FAIL, problem.splitlines()[0])
    if cfg:
        checks.check_config_values(cfg, report, "update" if cfg.published_file_id else "any")
        checks.check_content(cfg, report, built_by_pipeline=bool(cfg.build and cfg.build.steps))
        checks.check_preview(cfg, report)
        checks.check_build(cfg, report, ctx.env, strict_runtime=True)
        config_writable = os.access(str(cfg.path), os.W_OK)
        report.add("Permissions", checks.OK if config_writable else checks.WARN,
                   "workshop.json %s" % ("writable" if config_writable else "read-only (a new Workshop ID cannot be saved)"))
    checks.check_credentials(report, ctx.env, cfg, ctx.args.credentials_file, redactor=ctx.redactor)
    report.print(console)
    console.line()
    ctx.result["checks"] = report.items
    if not location.found and not location.error:
        console.line(platforms.install_help())
        console.line()
    if report.ok:
        console.ok("Ready to publish." if cfg and not cfg.published_file_id else "Ready to update.")
        return EXIT_OK
    console.error("%d problem(s) must be fixed before publishing." % len(report.failures))
    return EXIT_FAILURE


def cmd_validate(ctx):
    cfg = ctx.config()
    report = checks.Report()
    mode = "update" if cfg.published_file_id else "publish"
    checks.check_config_values(cfg, report, mode)
    has_build = bool(cfg.build and cfg.build.steps)
    checks.check_content(cfg, report, built_by_pipeline=has_build)
    checks.check_preview(cfg, report, required=mode == "publish")
    checks.check_build(cfg, report, ctx.env, strict_runtime=False)
    try:
        fields = item_fields(cfg, mode, change_note="validation", visibility=None, update_metadata=True)
        vdf.loads(vdf.dumps("workshopitem", fields))
        report.add("VDF", checks.OK, "generated and parsed back successfully")
    except (vdf.VdfError, ValueError) as exc:
        report.add("VDF", checks.FAIL, str(exc))
    report.print(ctx.console)
    ctx.result["checks"] = report.items
    if not report.ok:
        raise CommandError("validation failed with %d problem(s)" % len(report.failures))
    ctx.console.ok("Validation passed%s." % (" with %d warning(s)" % len(report.warnings) if report.warnings else ""))
    return EXIT_OK


def cmd_build(ctx):
    cfg = ctx.config()
    with pipeline_lock(cfg):
        return do_build(ctx, cfg)


def do_build(ctx, cfg):
    started = time.time()
    try:
        steps = run_build(cfg, ctx.console, ctx.env, clean=getattr(ctx.args, "clean", False))
    except BuildError as exc:
        raise CommandError("build failed: %s" % exc, EXIT_BUILD)
    ctx.result["build"] = {"steps": steps, "seconds": round(time.time() - started, 1)}
    return EXIT_OK


def item_fields(cfg, mode, change_note, visibility, update_metadata):
    os_name = platforms.current_os()
    is_new = mode == "publish"
    send_metadata = is_new or update_metadata
    if visibility is None and is_new:
        # New items start private unless configured otherwise, so nothing goes public by accident.
        visibility = cfg.visibility if cfg.visibility is not None else 2
    elif visibility is None and update_metadata:
        visibility = cfg.visibility
    return vdf.build_workshop_item(
        app_id=cfg.app_id,
        content_folder=str(cfg.content_dir),
        os_name=os_name,
        published_file_id=None if is_new else cfg.published_file_id,
        preview_file=str(cfg.preview_image) if cfg.preview_image else None,
        title=cfg.title if send_metadata else None,
        description=cfg.description if send_metadata else None,
        visibility=visibility,
        change_note=change_note,
    )


def cmd_upload(ctx):
    args = ctx.args
    mode = args.command
    console = ctx.console
    cfg = ctx.config()
    change_note = read_change_note(args)
    visibility = parse_visibility(args.visibility)

    if mode == "publish" and cfg.published_file_id:
        raise CommandError(
            "workshop.json already has publishedFileId %s. Use `workshop update` to update it.\n"
            "Refusing to create a second Workshop item." % cfg.published_file_id,
            EXIT_USAGE,
        )
    if mode == "update" and not cfg.published_file_id:
        raise CommandError(
            "workshop.json has no publishedFileId, so there is nothing to update.\n"
            "If this item has never been published, this is a first publish: run `workshop publish`.",
            EXIT_USAGE,
        )
    if mode == "publish" and not args.dry_run and not args.yes:
        raise CommandError(
            "`workshop publish` creates a NEW Workshop item. Re-run with --yes to confirm.", EXIT_USAGE
        )
    if change_note is None:
        change_note = ""
        if mode == "update":
            console.warn("no --change-note given; the update will have an empty change note")

    has_build = bool(cfg.build and cfg.build.steps) and not args.no_build
    report = checks.Report()
    checks.check_config_values(cfg, report, mode)
    checks.check_preview(cfg, report, required=mode == "publish")
    checks.check_build(cfg, report, ctx.env, strict_runtime=has_build)
    if not has_build:
        checks.check_content(cfg, report)
    fail_on_report(ctx, report, "pre-flight checks failed")

    steamcmd_path, creds = None, None
    if not args.dry_run:
        # Resolve SteamCMD and credentials before a potentially long build.
        steamcmd_path = require_steamcmd(ctx)
        creds = require_credentials(ctx, cfg)
    else:
        location = platforms.find_steamcmd(ctx.env)
        console.info("SteamCMD:  %s" % (location.path if location.found else "not found (not needed for --dry-run)"))

    with pipeline_lock(cfg):
        if has_build:
            do_build(ctx, cfg)
        elif cfg.build and cfg.build.steps:
            console.warn("--no-build: uploading the existing contents of %s" % cfg.content_dir)

        report = checks.Report()
        checks.check_content(cfg, report)
        fail_on_report(ctx, report, "content check failed")
        report.print(console)

        try:
            fields = item_fields(cfg, mode, change_note, visibility, getattr(args, "update_metadata", False))
            vdf_text = vdf.dumps("workshopitem", fields)
            vdf.loads(vdf_text)
        except (vdf.VdfError, ValueError) as exc:
            raise CommandError("could not generate the Workshop VDF: %s" % exc)

        size = content_size(cfg.content_dir)
        ctx.result.update({
            "appId": cfg.app_id,
            "publishedFileId": cfg.published_file_id,
            "contentDirectory": str(cfg.content_dir),
            "contentSize": size,
            "vdf": dict(fields),
        })

        if args.dry_run:
            console.heading("Dry run: nothing will be uploaded")
            console.line("Content:    %s" % cfg.content_dir)
            console.line("Preview:    %s" % (cfg.preview_image or "(unchanged)"))
            console.line("Mode:       %s" % ("update item %s" % cfg.published_file_id if mode == "update" else "create a new item"))
            console.line("Generated VDF:")
            console.write(vdf_text)
            ctx.result["dryRun"] = True
            return EXIT_OK

        console.heading("Uploading to Steam Workshop (%s)" % ("update %s" % cfg.published_file_id if mode == "update" else "new item"))
        upload_started = int(time.time()) - 5
        result = steamcmd.upload(
            steamcmd_path, creds, fields, console, ctx.redactor,
            interactive=args.interactive, show_output=args.verbose,
        )
        ctx.result["steamcmd"] = {"outcome": result.outcome, "exitCode": result.exit_code}
        if args.verbose:
            ctx.result["steamcmd"]["output"] = result.output
        if not result.ok:
            tail = "\n".join(result.output.strip().splitlines()[-15:])
            if tail and not args.verbose:
                console.line("Last SteamCMD output:")
                console.line(tail)
            raise CommandError(result.message, OUTCOME_EXIT.get(result.outcome, EXIT_UPLOAD))
        if result.outcome == steamcmd.Outcome.UNCERTAIN:
            console.warn(result.message)
        else:
            console.ok(result.message)

    published_id = result.published_file_id or cfg.published_file_id
    if mode == "publish":
        if not result.published_file_id:
            console.warn(
                "SteamCMD did not report the new Workshop ID. Find the item under your Steam profile's "
                "Workshop items and add its ID to workshop.json as \"publishedFileId\" before the next update."
            )
        elif args.no_save_id:
            console.info("New Workshop ID: %s (not saved because of --no-save-id)" % published_id)
        else:
            try:
                save_published_file_id(cfg, published_id)
                console.ok("Saved publishedFileId %s to %s" % (published_id, cfg.path.name))
            except (ConfigError, OSError) as exc:
                console.warn("could not save the Workshop ID (%s). Add \"publishedFileId\": \"%s\" manually." % (exc, published_id))
    elif result.published_file_id and result.published_file_id != cfg.published_file_id:
        console.warn("SteamCMD reported item %s but %s was configured" % (result.published_file_id, cfg.published_file_id))
    ctx.result["publishedFileId"] = published_id

    if published_id:
        url = "https://steamcommunity.com/sharedfiles/filedetails/?id=%s" % published_id
        ctx.result["url"] = url
        console.line("Workshop page: %s" % url)
        if not args.no_verify:
            return verify_upload(ctx, cfg, published_id, upload_started, size)
    return EXIT_OK


def verify_upload(ctx, cfg, published_id, uploaded_after, size, attempts=4, delay=15):
    console = ctx.console
    console.info("Verifying the update with the Steam Web API...")
    problems = []
    for attempt in range(1, attempts + 1):
        try:
            ok, details, problems = steamapi.verify_update(published_id, cfg.app_id, uploaded_after, size)
        except steamapi.SteamApiError as exc:
            console.warn("could not verify the upload: %s" % exc)
            ctx.result["verification"] = {"verified": False, "reason": str(exc)}
            return EXIT_OK
        ctx.result["verification"] = {"verified": ok, "details": details, "problems": problems}
        if ok:
            console.ok("Verified: Steam reports item %s updated at %s (%d bytes)." % (
                published_id, time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(details["timeUpdated"])),
                details["fileSize"]))
            return EXIT_OK
        if attempt < attempts:
            console.debug("verification attempt %d: %s; retrying in %ds" % (attempt, "; ".join(problems), delay))
            time.sleep(delay)
    raise CommandError("upload finished but verification failed: %s" % "; ".join(problems), EXIT_VERIFY)


def cmd_login(ctx):
    if not sys.stdin.isatty():
        raise CommandError("`workshop login` needs an interactive terminal (it may ask for a Steam Guard code)", EXIT_USAGE)
    cfg = None
    with contextlib.suppress(CommandError):
        cfg = ctx.config()
    steamcmd_path = require_steamcmd(ctx)
    creds = require_credentials(ctx, cfg)
    ctx.console.info("Logging in as %s. Enter the password and Steam Guard code if SteamCMD asks." % creds.username)
    result = steamcmd.login(steamcmd_path, creds, ctx.console, ctx.redactor)
    ctx.result["steamcmd"] = {"outcome": result.outcome, "exitCode": result.exit_code}
    if not result.ok:
        raise CommandError(result.message, OUTCOME_EXIT.get(result.outcome, EXIT_AUTH))
    ctx.console.ok(result.message)
    ctx.console.info("Unattended runs can now use `STEAM_USERNAME=%s` without a password." % creds.username)
    return EXIT_OK


def cmd_status(ctx):
    cfg = ctx.config()
    if not cfg.published_file_id:
        raise CommandError("workshop.json has no publishedFileId", EXIT_USAGE)
    try:
        details = steamapi.get_published_file_details(cfg.published_file_id)
    except steamapi.SteamApiError as exc:
        raise CommandError(str(exc), EXIT_FAILURE)
    ctx.result["details"] = details
    console = ctx.console
    console.line("Title:        %s" % details["title"])
    console.line("Workshop ID:  %s" % details["publishedFileId"])
    console.line("Updated:      %s" % time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(details["timeUpdated"])))
    console.line("File size:    %d bytes" % details["fileSize"])
    console.line("Visibility:   %s" % vdf.VISIBILITY_NAMES.get(details["visibility"], details["visibility"]))
    console.line("URL:          %s" % details["url"])
    return EXIT_OK


def _cs2_step(ctx, cfg):
    steps = [s for s in (cfg.build.steps if cfg.build else []) if s.kind == "cs2"]
    if not steps:
        raise CommandError("workshop.json has no build step of type \"cs2\"", EXIT_USAGE)
    from . import cs2

    try:
        return steps[0].spec, cs2.Cs2Paths(steps[0].spec, cfg, ctx.env)
    except (BuildError, ValueError) as exc:
        raise CommandError(str(exc), EXIT_USAGE)


def cmd_tools(ctx):
    from . import cs2

    cfg = ctx.config()
    settings, paths = _cs2_step(ctx, cfg)
    console = ctx.console
    action = ctx.args.tools_command
    ctx.result["tools"] = {"directory": str(paths.tools), "compiler": str(paths.compiler)}

    if action == "install":
        paths.tools.mkdir(parents=True, exist_ok=True)
        free = __import__("shutil").disk_usage(str(paths.tools)).free
        ctx.result["tools"]["freeBytes"] = free
        if free < 70 * 1024 ** 3:
            console.warn("only %.0f GB free in %s; the Windows CS2 install needs roughly 60 GB" % (free / 1024 ** 3, paths.tools))
        steamcmd_path = require_steamcmd(ctx)
        creds = require_credentials(ctx, cfg)
        console.info("Downloading/updating the Windows build of CS2 (app 730) into %s" % paths.tools)
        console.info("This is a large download on the first run; later runs only fetch changes.")
        result = steamcmd.install_app(
            steamcmd_path, creds, 730, str(paths.tools), console, ctx.redactor,
            platform_type="windows", interactive=ctx.args.interactive, show_output=ctx.args.verbose,
        )
        ctx.result["steamcmd"] = {"outcome": result.outcome, "exitCode": result.exit_code}
        if not result.ok:
            raise CommandError(result.message, OUTCOME_EXIT.get(result.outcome, EXIT_ENVIRONMENT))
        console.ok(result.message)
        if not paths.compiler.is_file():
            raise CommandError(
                "CS2 was downloaded but %s is missing. The resource compiler ships with the "
                "\"Counter-Strike 2 Workshop Tools\" DLC; make sure the uploader account owns it "
                "(it is free on the CS2 store page), then run this command again." % paths.compiler,
                EXIT_ENVIRONMENT,
            )
        console.ok("resourcecompiler.exe is present.")
        return EXIT_OK

    if action == "setup-wine":
        if not cs2.use_wine(settings):
            console.info("This build step does not use Wine on this platform.")
            return EXIT_OK
        try:
            cs2.init_wine_prefix(settings, paths, ctx.env, console)
        except cs2.Cs2Error as exc:
            raise CommandError(str(exc), EXIT_ENVIRONMENT)
        console.ok("Wine prefix ready: %s" % paths.wine_prefix)
        return EXIT_OK

    # check
    problems = cs2.check_runtime(settings, paths, ctx.env)
    ctx.result["tools"]["problems"] = problems
    if problems:
        raise CommandError("the CS2 compiler is not ready:\n  - " + "\n  - ".join(problems), EXIT_ENVIRONMENT)
    console.ok("CS2 compiler runtime looks ready: %s" % paths.compiler)
    return EXIT_OK


def cmd_pack_list(ctx):
    from . import manifest, vpk

    target = ctx.args.vpk
    if not target:
        cfg = ctx.config()
        _, paths = _cs2_step(ctx, cfg)
        target = paths.vpk
    try:
        data = manifest.from_vpk(target)
        vpk.verify(target)
    except (OSError, vpk.VpkError) as exc:
        raise CommandError("cannot read %s: %s" % (target, exc))
    ctx.result["pack"] = data
    console = ctx.console
    if ctx.args.compare:
        try:
            other = manifest.from_vpk(ctx.args.compare)
        except (OSError, vpk.VpkError) as exc:
            raise CommandError("cannot read %s: %s" % (ctx.args.compare, exc))
        changes = manifest.diff(other, data)
        ctx.result["compare"] = {"vpk": str(ctx.args.compare), "diff": changes}
        for label, key in (("+", "added"), ("-", "removed"), ("~", "changed")):
            for path in changes[key]:
                console.line("%s %s" % (label, path))
        console.line("%d added, %d removed, %d changed, %d unchanged (compared with %s, %s)" % (
            len(changes["added"]), len(changes["removed"]), len(changes["changed"]), changes["unchanged"],
            ctx.args.compare, manifest.format_size(other["vpkSize"])))
        return EXIT_OK
    for path, info in data["files"].items():
        console.line("%10d  %s" % (info["size"], path))
    console.line("%d files, %s VPK" % (data["fileCount"], manifest.format_size(data["vpkSize"])))
    return EXIT_OK


# ---------------------------------------------------------------------------
# parser


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS, help="path to workshop.json")
    common.add_argument("--credentials-file", metavar="PATH", default=argparse.SUPPRESS,
                        help="KEY=VALUE file with STEAM_USERNAME/STEAM_PASSWORD (default: $WORKSHOP_CREDENTIALS_FILE)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print a machine-readable JSON result on stdout (logs go to stderr)")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="show commands, resolved paths and full tool output (secrets are redacted)")
    common.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS, help="disable colored output")

    parser = argparse.ArgumentParser(
        prog="workshop",
        parents=[common],
        description="Build, validate and publish Steam Workshop content from Linux or Windows.",
        epilog="Exit codes: 0 ok, 1 failure, 2 usage, 3 environment, 4 authentication, 5 upload, "
        "6 build, 7 verification, 8 locked.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("doctor", parents=[common], help="check the environment, SteamCMD, config and credentials")
    sub.add_parser("validate", parents=[common], help="validate config, content, preview and references (no upload)")
    p = sub.add_parser("build", parents=[common], help="run the build steps (compile and pack)")
    p.add_argument("--clean", action="store_true", help="ignore incremental state and rebuild everything")

    for name, help_text in (("update", "build and update the existing Workshop item"),
                            ("publish", "build and create a NEW Workshop item")):
        p = sub.add_parser(name, parents=[common], help=help_text)
        p.add_argument("--change-note", metavar="TEXT", help="change note shown on the Workshop page")
        p.add_argument("--change-note-file", metavar="PATH", help="read the change note from a file")
        p.add_argument("--visibility", metavar="LEVEL", help="set visibility: public, friends-only, private, unlisted")
        p.add_argument("--dry-run", action="store_true", help="build and validate everything, but do not log in or upload")
        p.add_argument("--no-build", action="store_true", help="upload the existing content without building")
        p.add_argument("--clean", action="store_true", help="full rebuild instead of incremental")
        p.add_argument("--no-verify", action="store_true", help="skip the Steam Web API check after uploading")
        p.add_argument("--interactive", action="store_true",
                       help="allow SteamCMD to prompt for a password or Steam Guard code")
        if name == "update":
            p.add_argument("--update-metadata", action="store_true",
                           help="also send title, description and visibility from workshop.json")
        else:
            p.add_argument("--yes", action="store_true", help="confirm that a new Workshop item should be created")
            p.add_argument("--no-save-id", action="store_true", help="do not write the new ID into workshop.json")

    sub.add_parser("login", parents=[common], help="log in interactively once to cache a SteamCMD session")
    sub.add_parser("status", parents=[common], help="show the Workshop item's details from the Steam Web API")

    p = sub.add_parser("tools", parents=[common], help="manage the CS2 compiler (Windows CS2 build + Wine)")
    tools_sub = p.add_subparsers(dest="tools_command", metavar="<action>")
    t = tools_sub.add_parser("install", parents=[common], help="download/update the Windows CS2 build with SteamCMD")
    t.add_argument("--interactive", action="store_true", help="allow SteamCMD prompts")
    tools_sub.add_parser("setup-wine", parents=[common], help="create the Wine prefix used by the compiler")
    tools_sub.add_parser("check", parents=[common], help="check that the compiler runtime is ready")

    p = sub.add_parser("pack-list", parents=[common], help="list the files and sizes inside the built VPK")
    p.add_argument("vpk", nargs="?", help="VPK to inspect (default: the configured addon VPK)")
    p.add_argument("--compare", metavar="OTHER_VPK",
                   help="show files added/removed/changed relative to another VPK (e.g. the live Workshop download)")
    return parser


COMMANDS = {
    "doctor": cmd_doctor,
    "validate": cmd_validate,
    "build": cmd_build,
    "update": cmd_upload,
    "publish": cmd_upload,
    "login": cmd_login,
    "status": cmd_status,
    "tools": cmd_tools,
    "pack-list": cmd_pack_list,
}


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, default in (("config", None), ("credentials_file", None), ("json", False), ("verbose", False),
                          ("no_color", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE
    if args.command == "tools" and not getattr(args, "tools_command", None):
        parser.parse_args(["tools", "--help"])
    if not hasattr(args, "interactive"):
        args.interactive = False

    if os.name != "nt":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    ctx = Context(args)
    try:
        code = COMMANDS[args.command](ctx)
        ctx.result["ok"] = code == EXIT_OK
    except CommandError as exc:
        ctx.console.error(str(exc))
        ctx.result.update({"ok": False, "error": ctx.redactor.redact(str(exc))})
        ctx.result.update(exc.data)
        code = exc.exit_code
    except KeyboardInterrupt:
        ctx.console.error("interrupted")
        ctx.result.update({"ok": False, "error": "interrupted"})
        code = 130
    ctx.result["exitCode"] = code
    if args.json:
        text = json.dumps(ctx.result, indent=2, default=str)
        sys.stdout.write(ctx.redactor.redact(text) + "\n")
        sys.stdout.flush()
    return code
