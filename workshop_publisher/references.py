"""Asset reference scanning for CS2 addon sources.

Used for two things:
  * reporting references that resolve to neither a file in the addon sources nor a
    compiled file in the base game (e.g. a layout pointing at a missing image);
  * incremental builds: when a dependency (an image, a .wav, an .fbx...) changes,
    every resource that references it is recompiled.
"""

import fnmatch
import posixpath
import re

PANORAMA_TEXT = (".xml", ".css", ".js")
KV3_TEXT = (".vmat", ".vmdl", ".vsndevts", ".vpcf", ".vtex", ".vmdl_prefab", ".vsmart", ".vdata")
IMAGE_EXTS = (".png", ".tga", ".jpg", ".jpeg", ".psd", ".svg", ".exr", ".hdr")

DEFAULT_PANORAMA_ALIASES = {
    "images": "panorama/images",
    "resources": "panorama",
}

PANORAMA_REF_RE = re.compile(r"file://\{(?P<alias>[A-Za-z_]+)\}/(?P<path>[^\"'\)\s<>`]+)")
S2R_REF_RE = re.compile(r"s2r://(?P<path>[^\"'\)\s<>`]+)")
KV3_REF_RE = re.compile(
    r"\"(?P<path>[A-Za-z0-9_\-./]+\.(?:vmat|vmdl|vtex|vsnd|vpcf|png|tga|jpg|jpeg|psd|svg|fbx|dmx|obj|wav|mp3))\"",
    re.I,
)
COMPILED_IMAGE_RE = re.compile(r"^(?P<stem>.+)_(?P<ext>png|tga|jpg|jpeg|psd|svg)\.(?P<kind>vtex|vsvg)$", re.I)


class Reference:
    __slots__ = ("source", "line", "text", "candidates", "compiled")

    def __init__(self, source, line, text, candidates, compiled):
        self.source = source
        self.line = line
        self.text = text
        self.candidates = candidates  # possible source paths inside the addon
        self.compiled = compiled  # possible compiled paths inside a VPK

    def to_dict(self):
        return {"file": self.source, "line": self.line, "reference": self.text, "expected": self.candidates}


def _norm(path):
    path = path.replace("\\", "/").strip()
    path = path.split("?", 1)[0].split("#", 1)[0]
    return posixpath.normpath(path).lstrip("/").lower()


# Compiled resource type -> source extensions it is compiled from.
COMPILED_SOURCES = {
    ".vxml": (".xml",),
    ".vcss": (".css",),
    ".vjs": (".js",),
    ".vts": (".ts",),
    ".vsnd": (".wav", ".mp3"),
}


def _variants(path):
    """Source candidates and compiled names for a normalised resource path."""
    if path.endswith("_c") and "." in posixpath.basename(path):
        # "s2r://panorama/styles/x.vcss_c" names the compiled file; resolve it like "x.vcss".
        return _variants(path[:-2])
    stem, ext = posixpath.splitext(path)
    ext = ext.lower()
    if ext in COMPILED_SOURCES and not COMPILED_IMAGE_RE.match(path):
        sources = [stem + source_ext for source_ext in COMPILED_SOURCES[ext]] + [path]
        return sources, [path + "_c"]
    compiled_image = COMPILED_IMAGE_RE.match(path)
    if compiled_image:
        base = "%s.%s" % (compiled_image.group("stem"), compiled_image.group("ext"))
        return [base], [path + "_c"]
    if ext in IMAGE_EXTS:
        tag = ext[1:]
        return [path], ["%s_%s.vtex_c" % (stem, tag), "%s_%s.vsvg_c" % (stem, tag)]
    if ext in (".wav", ".mp3"):
        return [path], [stem + ".vsnd_c"]
    compiled_ext = {".xml": ".vxml_c", ".css": ".vcss_c", ".js": ".vjs_c"}.get(ext)
    if compiled_ext:
        return [path], [stem + compiled_ext]
    return [path], [path + "_c"]


def _is_dynamic(path):
    """Paths built at runtime (e.g. "file://{images}/rank_" + n + ".png") cannot be checked statically."""
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return path.endswith("/") or "." not in last or any(token in path for token in ("+", "${", "{", "}", "*"))


def scan_text(rel_path, text, aliases=None):
    aliases = dict(DEFAULT_PANORAMA_ALIASES, **(aliases or {}))
    refs = []
    lower = rel_path.lower()
    for number, line in enumerate(text.splitlines(), 1):
        if lower.endswith(PANORAMA_TEXT):
            for match in PANORAMA_REF_RE.finditer(line):
                raw = match.group(0)
                path = match.group("path")
                alias = match.group("alias").lower()
                if _is_dynamic(path) or alias not in aliases:
                    continue
                full = _norm(posixpath.join(aliases[alias], path))
                refs.append(Reference(rel_path, number, raw, *_variants(full)))
            for match in S2R_REF_RE.finditer(line):
                path = match.group("path")
                if _is_dynamic(path):
                    continue
                refs.append(Reference(rel_path, number, match.group(0), *_variants(_norm(path))))
        elif lower.endswith(KV3_TEXT):
            for match in KV3_REF_RE.finditer(line):
                path = match.group("path")
                if _is_dynamic(path):
                    continue
                refs.append(Reference(rel_path, number, path, *_variants(_norm(path))))
    return refs


def scan_sources(source_texts, aliases=None):
    """``source_texts`` maps relative source path -> text. Returns all references."""
    refs = []
    for rel, text in source_texts.items():
        refs.extend(scan_text(rel, text, aliases))
    return refs


def find_missing(refs, source_files, base_files=None, ignore=()):
    """References resolving to neither an addon source nor a base-game compiled file."""
    sources = {s.lower() for s in source_files}
    base = base_files or set()
    missing = []
    for ref in refs:
        if any(c in sources for c in ref.candidates):
            continue
        if any(c in base for c in ref.compiled):
            continue
        forms = list(ref.candidates) + list(ref.compiled) + [ref.text.lower()]
        if any(fnmatch.fnmatch(form, pat.lower()) for pat in ignore for form in forms):
            continue
        missing.append(ref)
    return missing


def dependents(refs, changed_files):
    """Source files whose references include any of ``changed_files``."""
    changed = {c.lower() for c in changed_files}
    result = set()
    for ref in refs:
        if any(c in changed for c in ref.candidates):
            result.add(ref.source)
    return result
