"""Running SteamCMD and interpreting its output.

Credentials are never placed on the SteamCMD command line (where other local
users could read them from the process list). Instead a runscript is written to
a private temporary directory (mode 0700, file mode 0600; $XDG_RUNTIME_DIR is
preferred on Linux because it is a per-user tmpfs) and deleted as soon as
SteamCMD exits.

On Linux SteamCMD runs inside a pseudo-terminal. Without one its output is block
buffered, so prompts would never appear and progress could not be parsed.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import vdf

# Lines worth showing in normal (non-verbose) mode.
PROGRESS_RE = re.compile(
    r"(Logging in|Waiting for (user info|client config)|Preparing update|Creating item|Uploading content|"
    r"Uploading preview|Committing update|Success\.|ERROR|FAILED|Steam Guard|Two-factor|Please confirm|"
    r"Update state|Checking for available update|Downloading update|Verifying installation|Unloading Steam API)",
    re.I,
)
PROMPT_RE = re.compile(r"(Steam Guard code|Two-factor code|password)\s*:\s*$", re.I)
PUBLISHED_ID_RES = (
    re.compile(r"PublishFileID\s*[:=]?\s*(\d{6,20})", re.I),
    re.compile(r"published\s*file\s*id\D{0,5}(\d{6,20})", re.I),
    re.compile(r"(?:created|updated) (?:new )?item\D{0,5}(\d{6,20})", re.I),
)

ERROR_HINTS = [
    (re.compile(r"Invalid Password|InvalidPassword", re.I), "auth",
     "Steam authentication was rejected: the username or password is wrong."),
    (re.compile(r"Rate Limit Exceeded|RateLimitExceeded", re.I), "auth",
     "Steam refused the login because of too many recent attempts. Wait (often 30+ minutes) and retry."),
    (re.compile(r"Two-factor code mismatch|Invalid Login Auth Code|InvalidLoginAuthCode|TwoFactorCodeMismatch", re.I),
     "steam_guard", "The Steam Guard code was rejected. Codes expire quickly; use a fresh one."),
    (re.compile(r"Account Logon Denied|AccountLogonDenied|AccountLoginDeniedNeedTwoFactor|need two-factor", re.I),
     "steam_guard", "Steam Guard authentication is required for this login."),
    (re.compile(r"Cached credentials not found|No cached credentials", re.I), "steam_guard",
     "SteamCMD has no cached login session for this account."),
    (re.compile(r"No subscription", re.I), "license",
     "The Steam account has no license for this app. Add the game to the account (CS2 is free) and retry."),
    (re.compile(r"error while loading shared libraries|linux32/steamcmd: No such file", re.I), "environment",
     "SteamCMD's 32-bit runtime is missing. On Ubuntu/Debian install it with: sudo apt install lib32gcc-s1"),
]

RESULT_HINTS = {
    "access denied": "Steam denied access. The account must own this Workshop item (or be a contributor), "
    "must have accepted the Steam Workshop legal agreement, and must not be a limited account.",
    "limit exceeded": "A Steam limit was exceeded. The most common cause is a preview image of 1 MB or more.",
    "file not found": "Steam could not find the Workshop item or a file. Check publishedFileId, "
    "contentfolder and previewfile.",
    "invalid parameter": "Steam rejected a parameter. Check the title/description length, visibility and paths.",
    "timeout": "Steam timed out. Retry later.",
    "service unavailable": "The Steam Workshop service is unavailable. Retry later.",
    "busy": "Steam is busy. Retry later.",
    "insufficient privilege": "The account is not allowed to publish (limited, locked or community-banned account).",
    "not logged on": "SteamCMD was not logged on when the upload started.",
}
WORKSHOP_ERROR_RE = re.compile(r"ERROR!?\s*(?:Failed to [^(\n]*?)?\((?P<result>[^)\n]+)\)", re.I)
SUCCESS_RE = re.compile(r"Committing update\.*\s*Success|^\s*Success\.\s*$", re.I | re.M)


class Outcome:
    SUCCESS = "success"
    UNCERTAIN = "uncertain"
    AUTH = "auth"
    STEAM_GUARD = "steam_guard"
    LICENSE = "license"
    ENVIRONMENT = "environment"
    UPLOAD = "upload"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class SteamCmdResult:
    def __init__(self, outcome, message, exit_code, output, published_file_id=None):
        self.outcome = outcome
        self.message = message
        self.exit_code = exit_code
        self.output = output
        self.published_file_id = published_file_id

    @property
    def ok(self):
        return self.outcome in (Outcome.SUCCESS, Outcome.UNCERTAIN)


def analyze_output(output, exit_code, prompt_blocked=None):
    """Classify SteamCMD output. ``prompt_blocked`` names a prompt we refused to answer."""
    published = None
    for regex in PUBLISHED_ID_RES:
        match = regex.search(output)
        if match:
            published = match.group(1)
            break

    if prompt_blocked:
        what = "Steam Guard code" if "guard" in prompt_blocked.lower() or "factor" in prompt_blocked.lower() else "password"
        return SteamCmdResult(
            Outcome.STEAM_GUARD,
            "SteamCMD asked for a %s but this run is non-interactive. The cached login session is missing or "
            "expired. Refresh it once from a terminal with: ./workshop login" % what,
            exit_code,
            output,
            published,
        )
    for regex, outcome, message in ERROR_HINTS:
        if regex.search(output):
            return SteamCmdResult(outcome, message, exit_code, output, published)
    match = WORKSHOP_ERROR_RE.search(output)
    if match:
        result = match.group("result").strip()
        hint = RESULT_HINTS.get(result.lower(), "")
        message = "Workshop upload failed (%s). %s" % (result, hint)
        return SteamCmdResult(Outcome.UPLOAD, message.strip(), exit_code, output, published)
    if SUCCESS_RE.search(output):
        return SteamCmdResult(Outcome.SUCCESS, "Workshop upload succeeded.", exit_code, output, published)
    if exit_code == 0:
        return SteamCmdResult(
            Outcome.UNCERTAIN,
            "SteamCMD exited successfully but did not print a recognisable success line. "
            "Verify the item on its Workshop page.",
            exit_code,
            output,
            published,
        )
    return SteamCmdResult(
        Outcome.FAILED,
        "SteamCMD exited with code %s without a recognisable error. Re-run with --verbose to see its output." % exit_code,
        exit_code,
        output,
        published,
    )


def _quote_arg(value):
    return '"%s"' % value


def build_runscript(credentials, vdf_path=None, non_interactive=True, extra_commands=(), pre_login=()):
    lines = ["@ShutdownOnFailedCommand 1"]
    if non_interactive:
        lines.append("@NoPromptForPassword 1")
    lines.extend(pre_login)
    if credentials.guard_code:
        lines.append("set_steam_guard_code %s" % _quote_arg(credentials.guard_code))
    login = "login %s" % _quote_arg(credentials.username)
    if credentials.password:
        login += " %s" % _quote_arg(credentials.password)
    lines.append(login)
    if vdf_path is not None:
        lines.append("workshop_build_item %s" % _quote_arg(vdf_path))
    lines.extend(extra_commands)
    lines.append("quit")
    return "\n".join(lines) + "\n"


class PrivateTempDir:
    """A 0700 temporary directory that is always removed."""

    def __init__(self, prefix="workshop-"):
        base = None
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if os.name != "nt" and runtime and os.path.isdir(runtime) and os.access(runtime, os.W_OK):
            base = runtime
        self.path = Path(tempfile.mkdtemp(prefix=prefix, dir=base))

    def write(self, name, text, mode=0o600):
        target = self.path / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(str(target), flags, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        return target

    def cleanup(self):
        shutil.rmtree(str(self.path), ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()


class OutputRelay:
    """Receives SteamCMD output, keeps a redacted transcript and shows the useful parts."""

    def __init__(self, console, redactor, show_all):
        self.console = console
        self.redactor = redactor
        self.show_all = show_all
        self.transcript = []
        self.buf = ""
        self.printed = 0
        self.prompt = None

    def feed(self, text):
        text = text.replace("\r\n", "\n")
        self.transcript.append(text)
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if self.printed:
                self.console.write(self.redactor.redact(line[self.printed:]) + "\n")
                self.printed = 0
            elif self.show_all or PROGRESS_RE.search(line):
                self.console.write("  " + self.redactor.redact(line.strip("\r")) + "\n")
        if self.buf and PROMPT_RE.search(self.buf):
            self.prompt = self.buf.strip()
        if self.buf and (self.printed or PROMPT_RE.search(self.buf)):
            if not self.printed:
                self.console.write("  ")
            self.console.write(self.redactor.redact(self.buf[self.printed:]))
            self.printed = len(self.buf)

    def flush(self):
        if self.buf and not self.printed and self.show_all:
            self.console.write("  " + self.redactor.redact(self.buf) + "\n")
        elif self.printed:
            self.console.write("\n")
        self.buf, self.printed = "", 0

    @property
    def output(self):
        return self.redactor.redact("".join(self.transcript))


def _exit_code_from_status(status):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


def _run_pty(argv, relay, interactive, cwd):
    import pty
    import select
    import termios
    import tty

    stdin_fd = sys.stdin.fileno() if interactive else None
    pid, master = pty.fork()
    if pid == 0:  # child
        try:
            if cwd:
                os.chdir(cwd)
            os.execv(argv[0], argv)
        except OSError as exc:
            sys.stderr.write("failed to execute %s: %s\n" % (argv[0], exc))
        finally:
            os._exit(127)

    saved_attrs = None
    if interactive:
        try:
            import fcntl
            import struct

            size = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
            saved_attrs = termios.tcgetattr(stdin_fd)
            # No local echo: the pseudo-terminal echoes exactly as SteamCMD requests,
            # so passwords typed at its prompt stay hidden.
            tty.setcbreak(stdin_fd)
        except (OSError, termios.error):
            saved_attrs = None

    prompt_blocked = None
    status = None
    try:
        while True:
            fds = [master] + ([stdin_fd] if interactive else [])
            ready, _, _ = select.select(fds, [], [], 0.2)
            if master in ready:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                relay.feed(data.decode("utf-8", "replace"))
                if relay.prompt and not interactive:
                    prompt_blocked = relay.prompt
                    _signal_group(pid, signal.SIGTERM)
                    break
            if interactive and stdin_fd in ready:
                data = os.read(stdin_fd, 1024)
                if data:
                    os.write(master, data)
                    relay.prompt = None
        status = _wait_child(pid, timeout=10 if prompt_blocked else None)
    except BaseException:
        try:
            _signal_group(pid, signal.SIGTERM)
            _wait_child(pid, timeout=10)
        except OSError:
            pass
        raise
    finally:
        if saved_attrs is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved_attrs)
        os.close(master)
        relay.flush()
    return _exit_code_from_status(status), prompt_blocked


def _signal_group(pid, sig):
    """Signal SteamCMD's whole session (steamcmd.sh starts the real binary as a child)."""
    try:
        os.killpg(pid, sig)  # pty.fork() makes the child a session and process-group leader
    except OSError:
        os.kill(pid, sig)


def _wait_child(pid, timeout=None):
    """waitpid with an optional timeout after which the child's process group is killed."""
    if timeout is None:
        return os.waitpid(pid, 0)[1]
    deadline = time.time() + timeout
    while time.time() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return status
        time.sleep(0.1)
    _signal_group(pid, signal.SIGKILL)
    return os.waitpid(pid, 0)[1]


def _run_pipe(argv, relay, cwd):
    """Non-interactive capture used on Windows (and as a fallback)."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    chunks = []
    lock = threading.Condition()

    def reader():
        while True:
            data = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(1)
            with lock:
                chunks.append(data)
                lock.notify()
            if not data:
                return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    prompt_blocked = None
    try:
        finished = False
        while not finished:
            with lock:
                if not chunks:
                    lock.wait(0.2)
                pending, chunks[:] = list(chunks), []
            for data in pending:
                if not data:
                    finished = True
                    break
                relay.feed(data.decode("utf-8", "replace"))
                if relay.prompt:
                    prompt_blocked = relay.prompt
                    proc.kill()
                    finished = True
                    break
        exit_code = proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        relay.flush()
    return exit_code, prompt_blocked


def _run_inherit(argv, cwd, stdout):
    """Interactive run on Windows: SteamCMD talks to the console directly."""
    return subprocess.call(argv, cwd=cwd, stdout=stdout), None


def _steamcmd_log_offsets(steamcmd_path):
    logs = Path(steamcmd_path).parent / "logs"
    offsets = {}
    if logs.is_dir():
        for log in logs.glob("*.txt"):
            try:
                offsets[log] = log.stat().st_size
            except OSError:
                pass
    return offsets


def _read_new_log_text(steamcmd_path, before):
    text = []
    for log in sorted((Path(steamcmd_path).parent / "logs").glob("*.txt")):
        try:
            with open(str(log), "rb") as handle:
                handle.seek(before.get(log, 0))
                text.append(handle.read().decode("utf-8", "replace"))
        except OSError:
            pass
    return "".join(text)


def command_for(steamcmd_path, script_path):
    return [str(steamcmd_path), "+runscript", str(script_path)]


def run_steamcmd(steamcmd_path, script_text, console, redactor, interactive=False, show_output=False):
    """Run SteamCMD with a private runscript. Returns (exit_code, output, prompt_blocked)."""
    steamcmd_path = Path(steamcmd_path)
    with PrivateTempDir() as tmp:
        script = tmp.write("steamcmd-script.txt", script_text)
        argv = command_for(steamcmd_path, script)
        console.debug("executing: %s" % subprocess.list2cmdline(argv))
        relay = OutputRelay(console, redactor, show_all=show_output)
        cwd = str(steamcmd_path.parent)
        if os.name == "nt":
            if interactive:
                before = _steamcmd_log_offsets(steamcmd_path)
                # In --json mode the console writes to stderr; keep stdout clean for JSON.
                stdout = sys.stderr if console.stream is sys.stderr else None
                exit_code, blocked = _run_inherit(argv, cwd, stdout)
                relay.feed(_read_new_log_text(steamcmd_path, before))
            else:
                exit_code, blocked = _run_pipe(argv, relay, cwd)
        else:
            exit_code, blocked = _run_pty(argv, relay, interactive, cwd)
        return exit_code, relay.output, blocked


def upload(steamcmd_path, credentials, item_fields, console, redactor, interactive=False, show_output=False):
    """Write the item VDF, run workshop_build_item and analyse the result."""
    with PrivateTempDir(prefix="workshop-item-") as tmp:
        vdf_text = vdf.dumps("workshopitem", item_fields)
        vdf_path = tmp.write("workshop-upload.vdf", vdf_text)
        console.debug("generated VDF:\n" + vdf_text)
        script = build_runscript(credentials, vdf_path=vdf_path, non_interactive=not interactive)
        exit_code, output, blocked = run_steamcmd(
            steamcmd_path, script, console, redactor, interactive=interactive, show_output=show_output
        )
        result = analyze_output(output, exit_code, blocked)
        # SteamCMD writes the new publishedfileid back into the VDF after creating an item.
        try:
            written = vdf.loads(vdf_path.read_text(encoding="utf-8", errors="replace")).get("workshopitem", {})
            written_id = str(written.get("publishedfileid", "")).strip()
            if written_id.isdigit() and written_id != "0":
                result.published_file_id = written_id
        except (OSError, vdf.VdfError):
            pass
        return result


def login(steamcmd_path, credentials, console, redactor):
    """Interactive login that caches a SteamCMD session for later unattended runs."""
    script = build_runscript(credentials, non_interactive=False)
    exit_code, output, blocked = run_steamcmd(
        steamcmd_path, script, console, redactor, interactive=True, show_output=True
    )
    if re.search(r"Logging in user .*OK|Waiting for user info\.*OK", output, re.I):
        return SteamCmdResult(Outcome.SUCCESS, "Login succeeded; the session is cached for this user.", exit_code, output)
    result = analyze_output(output, exit_code, blocked)
    if result.outcome in (Outcome.SUCCESS, Outcome.UNCERTAIN) and os.name == "nt" and exit_code == 0:
        return SteamCmdResult(Outcome.UNCERTAIN, "SteamCMD exited normally; the login was probably cached.", exit_code, output)
    if result.outcome in (Outcome.SUCCESS, Outcome.UNCERTAIN):
        result.outcome = Outcome.FAILED
        result.message = "Login did not report success. Re-run with --verbose to inspect SteamCMD output."
    return result


APP_STATE_HINTS = {
    "0x202": "not enough free disk space in the install directory",
    "0x206": "not enough free disk space in the install directory",
    "0x402": "a network or content server error occurred; retry",
    "0x602": "the update was interrupted or files are corrupt; retry with validate",
    "0x10502": "a write error occurred (check permissions and disk space)",
}


def throttle_commands(max_kbps):
    """SteamCMD's download limit for this session, in kilobits per second (not persisted)."""
    return ["set_download_throttle %d" % int(max_kbps)] if max_kbps else []


def install_app(steamcmd_path, credentials, app_id, install_dir, console, redactor,
                platform_type="windows", interactive=False, show_output=False, max_kbps=None, validate=False):
    """Download/update an app (e.g. the Windows build of CS2 for its compiler) with SteamCMD."""
    pre_login = [
        "@sSteamCmdForcePlatformType %s" % platform_type,
        "force_install_dir %s" % _quote_arg(install_dir),
    ]
    script = build_runscript(
        credentials,
        non_interactive=not interactive,
        extra_commands=throttle_commands(max_kbps) + ["app_update %d%s" % (int(app_id), " validate" if validate else "")],
        pre_login=pre_login,
    )
    exit_code, output, blocked = run_steamcmd(
        steamcmd_path, script, console, redactor, interactive=interactive, show_output=show_output
    )
    if re.search(r"Success! App '%d' (fully installed|already up to date)" % int(app_id), output, re.I):
        return SteamCmdResult(Outcome.SUCCESS, "App %d is installed and up to date." % int(app_id), exit_code, output)
    state = re.search(r"Error! App '\d+' state is (0x[0-9a-fA-F]+)", output)
    if state and not blocked:
        hint = APP_STATE_HINTS.get(state.group(1).lower(), "see the SteamCMD output with --verbose")
        return SteamCmdResult(Outcome.FAILED, "App download failed with state %s: %s." % (state.group(1), hint), exit_code, output)
    result = analyze_output(output, exit_code, blocked)
    if result.outcome in (Outcome.SUCCESS, Outcome.UNCERTAIN):
        result.outcome = Outcome.FAILED
        result.message = "SteamCMD did not confirm the app installation. Re-run with --verbose."
    return result


DEPOT_DONE_RE = re.compile(r'Depot download complete\s*:\s*"(?P<path>[^"]+)"', re.I)


def download_depot(steamcmd_path, credentials, app_id, depot_id, console, redactor,
                   platform_type="windows", interactive=False, show_output=False, max_kbps=None):
    """Download one depot with SteamCMD's download_depot. Returns (result, download_directory)."""
    script = build_runscript(
        credentials,
        non_interactive=not interactive,
        extra_commands=throttle_commands(max_kbps) + ["download_depot %d %d" % (int(app_id), int(depot_id))],
        pre_login=["@sSteamCmdForcePlatformType %s" % platform_type],
    )
    exit_code, output, blocked = run_steamcmd(
        steamcmd_path, script, console, redactor, interactive=interactive, show_output=show_output
    )
    match = DEPOT_DONE_RE.search(output)
    if match and not blocked:
        return SteamCmdResult(Outcome.SUCCESS, "Depot %d downloaded." % int(depot_id), exit_code, output), match.group("path")
    result = analyze_output(output, exit_code, blocked)
    if re.search(r"Unknown command", output, re.I):
        result.outcome = Outcome.FAILED
        result.message = "SteamCMD rejected a command (see --verbose); this SteamCMD may not support it."
    elif re.search(r"Depot download failed|Invalid depot|No subscription|Access Denied", output, re.I):
        result.outcome = Outcome.LICENSE
        result.message = ("Depot %d could not be downloaded. Check the depot ID and that the account owns the "
                          "content it belongs to (e.g. the Workshop Tools DLC)." % int(depot_id))
    elif result.outcome in (Outcome.SUCCESS, Outcome.UNCERTAIN):
        result.outcome = Outcome.FAILED
        result.message = "SteamCMD did not confirm the depot download. Re-run with --verbose."
    return result, None
