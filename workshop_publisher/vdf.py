"""Valve KeyValues (VDF) text generation and a small parser for round-trip checks.

Steam's own writer escapes backslashes and double quotes inside quoted values
(for example libraryfolders.vdf stores "C:\\Program Files (x86)\\Steam"), so we
do the same. Newlines are kept literally, which the parser accepts inside quotes.
"""

import ntpath
import posixpath
from collections import OrderedDict

VISIBILITY_NAMES = {0: "public", 1: "friends-only", 2: "private", 3: "unlisted"}

TITLE_MAX = 128
TEXT_MAX = 8000


class VdfError(ValueError):
    pass


def escape(value):
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\\", "\\\\").replace('"', '\\"')


def dumps(root_key, fields):
    """Serialise a flat mapping as a single VDF block."""
    width = max([len(k) for k in fields] + [0]) + 2
    lines = ['"%s"' % escape(root_key), "{"]
    for key, value in fields.items():
        quoted_key = '"%s"' % escape(key)
        lines.append('\t%s\t"%s"' % (quoted_key.ljust(width), escape(value)))
    lines.append("}")
    return "\n".join(lines) + "\n"


def native_path(path, os_name):
    """Absolute, normalised path string for the given target OS."""
    module = ntpath if os_name == "windows" else posixpath
    text = str(path)
    if not module.isabs(text):
        raise VdfError("VDF paths must be absolute, got: %s" % text)
    return module.normpath(text)


def build_workshop_item(
    app_id,
    content_folder,
    os_name,
    published_file_id=None,
    preview_file=None,
    title=None,
    description=None,
    visibility=None,
    change_note=None,
):
    """Return the ordered fields of a "workshopitem" block."""
    fields = OrderedDict()
    fields["appid"] = str(int(app_id))
    # "0" tells SteamCMD to create a new item; a real ID updates that item.
    fields["publishedfileid"] = str(published_file_id) if published_file_id else "0"
    fields["contentfolder"] = native_path(content_folder, os_name)
    if preview_file:
        fields["previewfile"] = native_path(preview_file, os_name)
    if visibility is not None:
        if int(visibility) not in VISIBILITY_NAMES:
            raise VdfError("visibility must be 0-3, got %r" % visibility)
        fields["visibility"] = str(int(visibility))
    if title is not None:
        if len(title) > TITLE_MAX:
            raise VdfError("title is longer than %d characters" % TITLE_MAX)
        fields["title"] = title
    if description is not None:
        if len(description) > TEXT_MAX:
            raise VdfError("description is longer than %d characters" % TEXT_MAX)
        fields["description"] = description
    if change_note is not None:
        if len(change_note) > TEXT_MAX:
            raise VdfError("change note is longer than %d characters" % TEXT_MAX)
        fields["changenote"] = change_note
    return fields


def loads(text):
    """Parse VDF text into nested OrderedDicts (strings only)."""
    tokens = list(_tokenize(text))
    pos = 0

    def parse_block(depth):
        nonlocal pos
        result = OrderedDict()
        while pos < len(tokens):
            kind, value = tokens[pos]
            if kind == "}":
                if depth == 0:
                    raise VdfError("unexpected '}'")
                pos += 1
                return result
            if kind != "str":
                raise VdfError("expected key, got %r" % value)
            key = value
            pos += 1
            if pos >= len(tokens):
                raise VdfError("missing value for key %r" % key)
            kind, value = tokens[pos]
            pos += 1
            if kind == "{":
                result[key] = parse_block(depth + 1)
            elif kind == "str":
                result[key] = value
            else:
                raise VdfError("unexpected %r after key %r" % (value, key))
        if depth != 0:
            raise VdfError("unterminated block")
        return result

    return parse_block(0)


def _tokenize(text):
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif ch in "{}":
            yield ch, ch
            i += 1
        elif ch == '"':
            i += 1
            out = []
            while True:
                if i >= n:
                    raise VdfError("unterminated string")
                ch = text[i]
                if ch == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    out.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
                    i += 2
                elif ch == '"':
                    i += 1
                    break
                else:
                    out.append(ch)
                    i += 1
            yield "str", "".join(out)
        else:
            start = i
            while i < n and not text[i].isspace() and text[i] not in '{}"':
                i += 1
            yield "str", text[start:i]
