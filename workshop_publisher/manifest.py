"""Pack manifests: file list, sizes and diffs against the previous build."""

import json
import os
import time
from pathlib import Path

from . import vpk


def from_vpk(path):
    """Manifest from the directory tree only (no file data is read)."""
    directory = vpk.read_directory(path)
    files = {e.path: {"size": e.size, "crc32": "%08x" % e.crc} for e in directory.entries.values()}
    return {
        "vpk": str(path),
        "vpkSize": directory.total_size(),
        "chunks": len(directory.chunk_files()),
        "fileCount": len(files),
        "contentSize": sum(f["size"] for f in files.values()),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": dict(sorted(files.items())),
    }


def diff(previous, current):
    old = (previous or {}).get("files", {})
    new = current.get("files", {})
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(p for p in set(new) & set(old) if new[p] != old[p])
    return {
        "previousVpkSize": (previous or {}).get("vpkSize"),
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged": len(set(new) & set(old)) - len(changed),
    }


def load(path):
    try:
        with open(str(path), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save(manifest, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    text = json.dumps(manifest, indent=2) + "\n"
    (directory / ("manifest-%s.json" % stamp)).write_text(text, encoding="utf-8")
    latest = directory / "latest.json"
    tmp = latest.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(latest))
    return latest


def format_size(size):
    size = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return ("%d %s" % (size, unit)) if unit == "B" else ("%.1f %s" % (size, unit))
        size /= 1024
