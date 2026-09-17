"""Valve Pak (VPK) reading and writing, versions 1 and 2.

Writing produces a single self-contained VPK version 2 file (all data stored in
the directory file, archive index 0x7FFF) with the MD5 checksum section filled
in, which is the layout Valve's vpk tool produces for small addon paks.
"""

import hashlib
import os
import struct
import zlib
from collections import OrderedDict
from pathlib import Path

SIGNATURE = 0x55AA1234
EMBEDDED_ARCHIVE = 0x7FFF
TERMINATOR = 0xFFFF


class VpkError(Exception):
    pass


class VpkEntry:
    __slots__ = ("path", "crc", "preload", "archive_index", "offset", "length")

    def __init__(self, path, crc, preload, archive_index, offset, length):
        self.path = path
        self.crc = crc
        self.preload = preload
        self.archive_index = archive_index
        self.offset = offset
        self.length = length

    @property
    def size(self):
        return len(self.preload) + self.length


def _split(path):
    path = path.replace("\\", "/").strip("/")
    directory, _, filename = path.rpartition("/")
    name, dot, ext = filename.rpartition(".")
    if not dot:
        name, ext = filename, ""
    return directory or " ", name, ext or " "


def _read_cstring(data, pos):
    end = data.index(b"\0", pos)
    return data[pos:end].decode("utf-8", "replace"), end + 1


def read_directory(path):
    """Return (version, header_size, tree_size, entries) for a VPK directory file."""
    with open(str(path), "rb") as handle:
        head = handle.read(28)
        if len(head) < 12:
            raise VpkError("%s is too small to be a VPK" % path)
        signature, version, tree_size = struct.unpack_from("<III", head)
        if signature != SIGNATURE:
            raise VpkError("%s is not a VPK file (bad signature)" % path)
        if version == 1:
            header_size = 12
        elif version == 2:
            header_size = 28
        else:
            raise VpkError("%s uses unsupported VPK version %d" % (path, version))
        handle.seek(header_size)
        tree = handle.read(tree_size)
    if len(tree) != tree_size:
        raise VpkError("%s is truncated" % path)

    entries = OrderedDict()
    pos = 0
    while True:
        ext, pos = _read_cstring(tree, pos)
        if not ext:
            break
        while True:
            directory, pos = _read_cstring(tree, pos)
            if not directory:
                break
            while True:
                name, pos = _read_cstring(tree, pos)
                if not name:
                    break
                crc, preload_len, archive_index, offset, length, terminator = struct.unpack_from("<IHHIIH", tree, pos)
                pos += 18
                if terminator != TERMINATOR:
                    raise VpkError("%s has a corrupt directory tree" % path)
                preload = tree[pos:pos + preload_len]
                pos += preload_len
                full = name if ext == " " else "%s.%s" % (name, ext)
                if directory != " ":
                    full = "%s/%s" % (directory, full)
                entries[full] = VpkEntry(full, crc, preload, archive_index, offset, length)
    return version, header_size, tree_size, entries


def list_files(path):
    return list(read_directory(path)[3].values())


def read_file(path, entry_path):
    version, header_size, tree_size, entries = read_directory(path)
    entry = entries.get(entry_path.replace("\\", "/"))
    if entry is None:
        raise VpkError("%s not found in %s" % (entry_path, path))
    data = entry.preload
    if entry.length:
        if entry.archive_index != EMBEDDED_ARCHIVE:
            raise VpkError("%s is stored in an external archive, which is not supported here" % entry_path)
        with open(str(path), "rb") as handle:
            handle.seek(header_size + tree_size + entry.offset)
            data += handle.read(entry.length)
    if zlib.crc32(data) & 0xFFFFFFFF != entry.crc:
        raise VpkError("CRC mismatch for %s in %s" % (entry_path, path))
    return data


def verify(path):
    """Verify CRCs of all files and the MD5 section of a self-contained v2 VPK."""
    version, header_size, tree_size, entries = read_directory(path)
    for entry in entries.values():
        read_file(path, entry.path)
    if version == 2:
        with open(str(path), "rb") as handle:
            blob = handle.read()
        _, _, _, data_size, archive_md5_size, other_md5_size, _ = struct.unpack_from("<IIIIIII", blob)
        if other_md5_size == 48:
            md5_start = header_size + tree_size + data_size + archive_md5_size
            tree_md5, archive_md5, whole_md5 = blob[md5_start:md5_start + 16], blob[md5_start + 16:md5_start + 32], blob[md5_start + 32:md5_start + 48]
            tree = blob[header_size:header_size + tree_size]
            archive_section = blob[header_size + tree_size + data_size:md5_start]
            if hashlib.md5(tree).digest() != tree_md5:
                raise VpkError("tree checksum mismatch in %s" % path)
            if hashlib.md5(archive_section).digest() != archive_md5:
                raise VpkError("archive MD5 section checksum mismatch in %s" % path)
            if hashlib.md5(blob[:md5_start + 32]).digest() != whole_md5:
                raise VpkError("whole-file checksum mismatch in %s" % path)
    return len(entries)


def write(output_path, files):
    """Write a VPK v2. ``files`` maps archive paths (forward slashes) to local file paths."""
    grouped = OrderedDict()
    for archive_path in sorted(files, key=lambda p: p.lower()):
        normalised = archive_path.replace("\\", "/").strip("/")
        if not normalised or ".." in normalised.split("/"):
            raise VpkError("invalid archive path: %r" % archive_path)
        directory, name, ext = _split(normalised)
        grouped.setdefault(ext, OrderedDict()).setdefault(directory, []).append((name, files[archive_path]))

    tree = bytearray()
    data_chunks = []
    offset = 0
    for ext, directories in grouped.items():
        tree += ext.encode("utf-8") + b"\0"
        for directory, names in directories.items():
            tree += directory.encode("utf-8") + b"\0"
            for name, local in names:
                with open(str(local), "rb") as handle:
                    content = handle.read()
                tree += name.encode("utf-8") + b"\0"
                tree += struct.pack(
                    "<IHHIIH", zlib.crc32(content) & 0xFFFFFFFF, 0, EMBEDDED_ARCHIVE, offset, len(content), TERMINATOR
                )
                data_chunks.append(content)
                offset += len(content)
            tree += b"\0"
        tree += b"\0"
    tree += b"\0"

    archive_md5_section = b""
    header = struct.pack("<IIIIIII", SIGNATURE, 2, len(tree), offset, len(archive_md5_section), 48, 0)
    body = hashlib.md5()
    body.update(header)
    body.update(tree)
    for chunk in data_chunks:
        body.update(chunk)
    body.update(archive_md5_section)
    tree_md5 = hashlib.md5(bytes(tree)).digest()
    archive_md5 = hashlib.md5(archive_md5_section).digest()
    body.update(tree_md5 + archive_md5)
    whole_md5 = body.digest()

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    with open(str(tmp_path), "wb") as handle:
        handle.write(header)
        handle.write(tree)
        for chunk in data_chunks:
            handle.write(chunk)
        handle.write(archive_md5_section)
        handle.write(tree_md5 + archive_md5 + whole_md5)
    os.replace(str(tmp_path), str(output_path))
    return output_path
