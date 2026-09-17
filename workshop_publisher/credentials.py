"""Steam credential resolution.

Sources, highest priority first:
  1. STEAM_USERNAME / STEAM_PASSWORD / STEAM_GUARD_CODE environment variables
  2. a credentials file named by WORKSHOP_CREDENTIALS_FILE (or --credentials-file),
     e.g. /etc/<project>/steam-uploader.env, in KEY=VALUE format
  3. a .env file next to workshop.json (developer convenience only)

The password is optional. The recommended unattended setup is a dedicated
uploader account whose SteamCMD session was cached once with `workshop login`;
SteamCMD then logs in with the username alone and Steam Guard never prompts.
"""

import os
import stat
from pathlib import Path

from .config import load_dotenv

CREDENTIAL_KEYS = ("STEAM_USERNAME", "STEAM_PASSWORD", "STEAM_GUARD_CODE")


class CredentialError(Exception):
    pass


class Credentials:
    def __init__(self, username=None, password=None, guard_code=None, source=None):
        self.username = username
        self.password = password
        self.guard_code = guard_code
        self.source = source

    @property
    def mode(self):
        if not self.username:
            return "missing"
        return "password" if self.password else "cached-session"

    def describe(self):
        if not self.username:
            return "no Steam username configured"
        if self.password:
            return "account %s with password from %s" % (self.username, self.source)
        return "account %s using SteamCMD's cached login session (%s)" % (self.username, self.source)


def check_file_permissions(path, uid=None):
    """Reject credential files that other users could read or modify (POSIX only)."""
    if os.name == "nt":
        return
    info = os.stat(str(path))
    uid = os.getuid() if uid is None else uid
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid not in (0, uid):
        raise CredentialError(
            "%s must be owned by root or by the current user (owner uid is %d)" % (path, info.st_uid)
        )
    if mode & 0o007:
        raise CredentialError(
            "%s is accessible by other users (mode %04o). Fix with: sudo chmod o-rwx '%s'" % (path, mode, path)
        )
    if mode & 0o020:
        raise CredentialError("%s is group-writable (mode %04o); remove group write access" % (path, mode))
    if info.st_uid == uid and uid != 0 and mode & 0o070:
        raise CredentialError(
            "%s is owned by you but readable by its group (mode %04o); use chmod 600" % (path, mode)
        )


def read_credentials_file(path):
    path = Path(path)
    if not path.exists():
        raise CredentialError("credentials file not found: %s" % path)
    if not path.is_file():
        raise CredentialError("credentials path is not a regular file: %s" % path)
    check_file_permissions(path)
    if not os.access(str(path), os.R_OK):
        raise CredentialError(
            "credentials file %s is not readable by this user. For a root-owned file use e.g.:\n"
            "  sudo chown root:<uploader-group> '%s' && sudo chmod 640 '%s'" % (path, path, path)
        )
    values = {}
    try:
        load_dotenv(path, values)
    except (OSError, UnicodeDecodeError) as exc:
        raise CredentialError("cannot read credentials file %s: %s" % (path, exc))
    unknown = sorted(set(values) - set(CREDENTIAL_KEYS))
    if unknown:
        raise CredentialError("credentials file %s contains unsupported keys: %s" % (path, ", ".join(unknown)))
    return values


def resolve_credentials(env, credentials_file=None, dotenv_path=None, redactor=None):
    values, source = {}, None
    credentials_file = credentials_file or env.get("WORKSHOP_CREDENTIALS_FILE")
    if credentials_file:
        values = read_credentials_file(credentials_file)
        source = str(credentials_file)
    elif dotenv_path and Path(dotenv_path).is_file():
        check_file_permissions(dotenv_path)
        load_dotenv(dotenv_path, values)
        values = {k: v for k, v in values.items() if k in CREDENTIAL_KEYS}
        source = str(dotenv_path) if values else None

    env_values = {k: env[k] for k in CREDENTIAL_KEYS if env.get(k)}
    if env_values:
        values.update(env_values)
        source = "environment" if not source else "%s + environment" % source

    creds = Credentials(
        username=values.get("STEAM_USERNAME"),
        password=values.get("STEAM_PASSWORD"),
        guard_code=values.get("STEAM_GUARD_CODE"),
        source=source,
    )
    if redactor is not None:
        redactor.add(creds.password)
        redactor.add(creds.guard_code)
    if creds.username and creds.username.strip().lower() == "anonymous":
        raise CredentialError(
            "anonymous SteamCMD logins cannot publish or update Workshop items; "
            "use a Steam account that has a license for the app"
        )
    for name, value in (("password", creds.password), ("username", creds.username)):
        if value and any(ch in value for ch in '"\r\n'):
            raise CredentialError(
                "the Steam %s contains a double quote or newline, which cannot be passed to SteamCMD safely" % name
            )
    return creds
