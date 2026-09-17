"""Loading, validating and updating workshop.json."""

import json
import os
import re
import tempfile
from collections import OrderedDict
from pathlib import Path

CONFIG_FILENAME = "workshop.json"

VISIBILITY_ALIASES = {
    "public": 0,
    "friends": 1,
    "friends-only": 1,
    "friendsonly": 1,
    "private": 2,
    "unlisted": 3,
}

KNOWN_KEYS = {
    "$schema",
    "$comment",
    "appId",
    "publishedFileId",
    "contentDirectory",
    "previewImage",
    "title",
    "description",
    "descriptionFile",
    "visibility",
    "build",
}

WORKSHOP_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")


class ConfigError(Exception):
    def __init__(self, path, problems):
        self.path = path
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class WorkshopConfig:
    def __init__(self, path, data):
        self.path = Path(path)
        self.root = self.path.parent
        self.data = data
        self.warnings = []
        self.app_id = None
        self.published_file_id = None
        self.content_dir = None
        self.preview_image = None
        self.title = None
        self.description = None
        self.visibility = None
        self.build = None

    @property
    def is_first_publish(self):
        return not self.published_file_id


def default_config_path(env=None, home_dir=None):
    env = os.environ if env is None else env
    if env.get("WORKSHOP_CONFIG"):
        return Path(env["WORKSHOP_CONFIG"]).expanduser()
    cwd_candidate = Path.cwd() / CONFIG_FILENAME
    if cwd_candidate.is_file():
        return cwd_candidate
    # Fall back to the repository that contains the launcher, so the tool works
    # from any working directory.
    if home_dir:
        return Path(home_dir) / CONFIG_FILENAME
    return cwd_candidate


def resolve_config_path(value, base):
    """Resolve a path from the config relative to the config file's directory."""
    text = str(value).strip()
    if not text:
        raise ValueError("path is empty")
    if os.name != "nt":
        if re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"):
            raise ValueError("'%s' is a Windows-only absolute path; use a path relative to workshop.json" % text)
        # Configs written on Windows may use backslashes; they are separators, not filename characters.
        text = text.replace("\\", "/")
    text = os.path.expandvars(os.path.expanduser(text))
    path = Path(text)
    if not path.is_absolute():
        path = Path(base) / path
    return Path(os.path.abspath(str(path)))


def _format_json_error(text, error):
    lines = text.splitlines()
    snippet = lines[error.lineno - 1] if 0 < error.lineno <= len(lines) else ""
    return "invalid JSON at line %d, column %d: %s\n    %s\n    %s^" % (
        error.lineno,
        error.colno,
        error.msg,
        snippet,
        " " * max(error.colno - 1, 0),
    )


def load_config(path):
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            path,
            [
                "configuration file not found: %s" % path,
                "create one with: cp workshop.example.json workshop.json",
            ],
        )
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(path, ["cannot read %s: %s" % (path, exc)])
    try:
        data = json.loads(text, object_pairs_hook=OrderedDict)
    except json.JSONDecodeError as exc:
        raise ConfigError(path, [_format_json_error(text, exc)])
    if not isinstance(data, dict):
        raise ConfigError(path, ["top level of %s must be a JSON object" % path.name])

    cfg = WorkshopConfig(path.resolve(), data)
    problems = []

    for key in data:
        if key not in KNOWN_KEYS:
            cfg.warnings.append("unknown key '%s' is ignored (check spelling)" % key)

    app_id = data.get("appId")
    if isinstance(app_id, str) and app_id.isdigit():
        app_id = int(app_id)
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
        problems.append("'appId' must be a positive integer (e.g. 730 for Counter-Strike 2)")
    else:
        cfg.app_id = app_id

    pfid = data.get("publishedFileId")
    if pfid in (None, "", 0, "0"):
        cfg.published_file_id = None
    elif isinstance(pfid, bool):
        problems.append("'publishedFileId' must be a string of digits")
    elif isinstance(pfid, int):
        cfg.warnings.append("'publishedFileId' should be a JSON string to avoid number precision issues")
        cfg.published_file_id = str(pfid)
    elif isinstance(pfid, str) and WORKSHOP_ID_RE.match(pfid.strip()):
        cfg.published_file_id = pfid.strip()
    else:
        problems.append("'publishedFileId' must be empty or a numeric Workshop ID, got %r" % (pfid,))

    for key, attr, required in (("contentDirectory", "content_dir", True), ("previewImage", "preview_image", False)):
        value = data.get(key)
        if value in (None, ""):
            if required:
                problems.append("'%s' is required" % key)
            continue
        if not isinstance(value, str):
            problems.append("'%s' must be a string path" % key)
            continue
        try:
            setattr(cfg, attr, resolve_config_path(value, cfg.root))
        except ValueError as exc:
            problems.append("'%s': %s" % (key, exc))

    for key in ("title", "description"):
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            problems.append("'%s' must be a string" % key)
        elif value is not None:
            setattr(cfg, key, value)

    description_file = data.get("descriptionFile")
    if description_file:
        if data.get("description"):
            problems.append("use either 'description' or 'descriptionFile', not both")
        else:
            try:
                desc_path = resolve_config_path(description_file, cfg.root)
                cfg.description = desc_path.read_text(encoding="utf-8-sig")
            except ValueError as exc:
                problems.append("'descriptionFile': %s" % exc)
            except (OSError, UnicodeDecodeError) as exc:
                problems.append("'descriptionFile' cannot be read: %s" % exc)

    visibility = data.get("visibility")
    if visibility is not None:
        if isinstance(visibility, str) and visibility.strip().lower() in VISIBILITY_ALIASES:
            cfg.visibility = VISIBILITY_ALIASES[visibility.strip().lower()]
        elif isinstance(visibility, int) and not isinstance(visibility, bool) and 0 <= visibility <= 3:
            cfg.visibility = visibility
        else:
            problems.append("'visibility' must be one of public, friends-only, private, unlisted (or 0-3)")

    build = data.get("build")
    if build is not None:
        from .build import parse_build_config

        try:
            cfg.build = parse_build_config(build)
        except ValueError as exc:
            problems.append("'build': %s" % exc)

    if problems:
        raise ConfigError(path, problems)
    return cfg


def save_published_file_id(cfg, published_file_id):
    """Write a newly created Workshop ID into workshop.json.

    Refuses to replace a different, already-configured ID. The file is replaced
    atomically so an interrupted write cannot corrupt it.
    """
    path = cfg.path
    text = path.read_text(encoding="utf-8-sig")
    data = json.loads(text, object_pairs_hook=OrderedDict)
    existing = str(data.get("publishedFileId") or "").strip()
    if existing and existing not in ("0", str(published_file_id)):
        raise ConfigError(
            path,
            ["refusing to overwrite existing publishedFileId %s with %s" % (existing, published_file_id)],
        )
    if "publishedFileId" in data:
        data["publishedFileId"] = str(published_file_id)
    else:
        updated = OrderedDict()
        for key, value in data.items():
            updated[key] = value
            if key == "appId":
                updated["publishedFileId"] = str(published_file_id)
        if "publishedFileId" not in updated:
            updated["publishedFileId"] = str(published_file_id)
        data = updated

    newline = "\r\n" if "\r\n" in text else "\n"
    output = json.dumps(data, indent=2, ensure_ascii=False).replace("\n", newline) + newline
    fd, tmp_name = tempfile.mkstemp(prefix=".workshop-", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(output)
        if os.name != "nt":
            os.chmod(tmp_name, os.stat(str(path)).st_mode & 0o777)
        os.replace(tmp_name, str(path))
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


def load_dotenv(path, env):
    """Load KEY=VALUE pairs from a .env file without overriding existing variables.

    Returns the list of variable names that were loaded (values are never returned).
    """
    path = Path(path)
    if not path.is_file():
        return []
    loaded = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = ENV_LINE_RE.match(raw)
        if not match:
            continue
        name, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if value == "" or name in env:
            continue
        env[name] = value
        loaded.append(name)
    return loaded
