"""Terminal output helpers and secret redaction."""

import os
import re
import sys

REDACTED = "********"

# Environment variables whose values are always treated as secrets.
SECRET_ENV_NAMES = ("STEAM_PASSWORD", "STEAM_GUARD_CODE")
SECRET_ENV_PATTERN = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|AUTH_?CODE|GUARD_?CODE)", re.I)


class Redactor:
    """Replaces known secret values in any text before it is displayed or logged."""

    def __init__(self):
        self._secrets = set()

    def add(self, value):
        if value and len(value) >= 3:
            self._secrets.add(value)

    def add_from_env(self, env):
        for name, value in env.items():
            if name in SECRET_ENV_NAMES or SECRET_ENV_PATTERN.search(name):
                self.add(value)

    def redact(self, text):
        if not text:
            return text
        # Longest first so a secret that contains another secret is fully hidden.
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return text


def _enable_windows_ansi():
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class Console:
    def __init__(self, verbose=False, color=None, stream=None, redactor=None):
        self.verbose = verbose
        self.stream = stream or sys.stdout
        self.redactor = redactor or Redactor()
        if color is None:
            color = (
                hasattr(self.stream, "isatty")
                and self.stream.isatty()
                and "NO_COLOR" not in os.environ
                and os.environ.get("TERM") != "dumb"
            )
            if color and os.name == "nt":
                color = _enable_windows_ansi()
        self.color = bool(color)

    def _paint(self, code, text):
        return "\033[%sm%s\033[0m" % (code, text) if self.color else text

    def write(self, text):
        self.stream.write(self.redactor.redact(text))
        self.stream.flush()

    def line(self, text=""):
        self.write(text + "\n")

    def heading(self, text):
        self.line(self._paint("1", text))

    def ok(self, text):
        self.line(self._paint("32", text))

    def info(self, text):
        self.line(text)

    def warn(self, text):
        self.line(self._paint("33", "Warning: ") + text)

    def error(self, text):
        self.line(self._paint("31", "Error: ") + text)

    def debug(self, text):
        if self.verbose:
            self.line(self._paint("2", "[debug] " + text))

    def status(self, label, state, detail="", width=16):
        colors = {"OK": "32", "WARN": "33", "FAIL": "31", "INFO": "36", "SKIP": "2"}
        tag = self._paint(colors.get(state, "0"), state)
        self.line(("%-*s %s %s" % (width, label + ":", tag, detail)).rstrip())
