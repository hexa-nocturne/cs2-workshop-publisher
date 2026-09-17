"""Valve Pak (VPK) reading and writing, versions 1 and 2.

Two layouts are supported for both reading and writing:

* single file (``addon.vpk``): all data stored after the directory tree
  (archive index 0x7FFF);
* multi-part (``pak01_dir.vpk`` + ``pak01_000.vpk``, ``pak01_001.vpk``...): the
  directory file holds the tree and checksums, data lives in numbered chunks.
  This is what Valve's vpk tool produces for large packs.

Writing streams file data in blocks, so memory use does not grow with pack size.
"""

import hashlib
import os
import re
import struct
import zlib
from collections import OrderedDict
from pathlib import Path

SIGNATURE = 0x55AA1234
EMBEDDED_ARCHIVE = 0x7FFF
TERMINATOR = 0xFFFF
BLOCK = 1024 * 1024
ARCHIVE_MD5_ENTRY = struct.Struct("<III16s")
DEFAULT_CHUNK_BYTES = 100 * 1024 * 1024


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


class VpkDirectory:
    def __init__(self, path, version, header_size, tree_size, header, entries):
        self.path = Path(path)
        self.version = version
        self.header_size = header_size
        self.tree_size = tree_size
        self.header = header
        self.entries = entries

    @property
    def is_multipart(self):
        return is_multipart_name(self.path)

    def archive_path(self, index):
        if index == EMBEDDED_ARCHIVE:
            return self.path
        if not self.is_multipart:
            raise VpkError("%s references archive %d but is not a *_dir.vpk file" % (self.path, index))
        return chunk_path(self.path, index)

    def data_offset(self, entry):
        if entry.archive_index == EMBEDDED_ARCHIVE:
            return self.header_size + self.tree_size + entry.offset
        return entry.offset

    def chunk_files(self):
        indexes = sorted({e.archive_index for e in self.entries.values() if e.archive_index != EMBEDDED_ARCHIVE})
        return [self.archive_path(i) for i in indexes]

    def total_size(self):
        size = os.path.getsize(str(self.path))
        for chunk in self.chunk_files():
            if chunk.is_file():
                size += os.path.getsize(str(chunk))
        return size


def is_multipart_name(path):
    return Path(path).name.lower().endswith("_dir.vpk")


def chunk_path(dir_path, index):
    dir_path = Path(dir_path)
    return dir_path.with_name("%s_%03d.vpk" % (dir_path.name[: -len("_dir.vpk")], index))


def _split(path):
    directory, _, filename = path.rpartition("/")
    name, dot, ext = filename.rpartition(".")
    if not dot:
        name, ext = filename, ""
    return directory or " ", name, ext or " "


def _read_cstring(data, pos):
    end = data.index(b"\0", pos)
    return data[pos:end].decode("utf-8", "replace"), end + 1


def read_directory(path):
    path = Path(path)
    with open(str(path), "rb") as handle:
        head = handle.read(28)
        if len(head) < 12:
            raise VpkError("%s is too small to be a VPK" % path)
        signature, version, tree_size = struct.unpack_from("<III", head)
        if signature != SIGNATURE:
            raise VpkError("%s is not a VPK directory file (bad signature)" % path)
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
    try:
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
                        full = "%s/%s" % (directory.strip("/"), full)
                    entries[full.lower()] = VpkEntry(full.lower(), crc, preload, archive_index, offset, length)
    except (ValueError, struct.error):
        raise VpkError("%s has a truncated directory tree" % path)
    return VpkDirectory(path, version, header_size, tree_size, head[:header_size], entries)


def list_files(path):
    return list(read_directory(path).entries.values())


def iter_entry_data(directory, entry):
    """Yield the bytes of one entry in blocks (preload first)."""
    if entry.preload:
        yield entry.preload
    remaining = entry.length
    if not remaining:
        return
    archive = directory.archive_path(entry.archive_index)
    if not archive.is_file():
        raise VpkError("archive chunk %s is missing (needed for %s)" % (archive, entry.path))
    with open(str(archive), "rb") as handle:
        handle.seek(directory.data_offset(entry))
        while remaining:
            data = handle.read(min(BLOCK, remaining))
            if not data:
                raise VpkError("%s is truncated (while reading %s)" % (archive, entry.path))
            remaining -= len(data)
            yield data


def read_file(path, entry_path, directory=None):
    directory = directory or read_directory(path)
    entry = directory.entries.get(entry_path.replace("\\", "/").lower())
    if entry is None:
        raise VpkError("%s not found in %s" % (entry_path, path))
    data = b"".join(iter_entry_data(directory, entry))
    if zlib.crc32(data) & 0xFFFFFFFF != entry.crc:
        raise VpkError("CRC mismatch for %s in %s" % (entry_path, path))
    return data


def extract(path, destination, overwrite=False):
    """Extract every file to ``destination`` (streamed). Returns the file count."""
    directory = read_directory(path)
    destination = Path(os.path.abspath(str(destination)))
    count = 0
    for entry in directory.entries.values():
        target = Path(os.path.abspath(str(destination / entry.path)))
        if destination not in target.parents:
            raise VpkError("refusing to extract %s outside %s" % (entry.path, destination))
        if target.exists() and not overwrite:
            raise VpkError("%s already exists (use --overwrite)" % target)
        target.parent.mkdir(parents=True, exist_ok=True)
        crc = 0
        with open(str(target), "wb") as out:
            for block in iter_entry_data(directory, entry):
                crc = zlib.crc32(block, crc)
                out.write(block)
        if crc & 0xFFFFFFFF != entry.crc:
            raise VpkError("CRC mismatch for %s" % entry.path)
        count += 1
    return count


def _md5_file_range(path, start, length):
    digest = hashlib.md5()
    with open(str(path), "rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining:
            data = handle.read(min(BLOCK, remaining))
            if not data:
                break
            digest.update(data)
            remaining -= len(data)
    return digest.digest()


def verify(path):
    """Verify every file's CRC and, for v2, the MD5 sections. Returns the file count."""
    directory = read_directory(path)
    for entry in directory.entries.values():
        crc = 0
        for block in iter_entry_data(directory, entry):
            crc = zlib.crc32(block, crc)
        if crc & 0xFFFFFFFF != entry.crc:
            raise VpkError("CRC mismatch for %s in %s" % (entry.path, path))
    if directory.version != 2:
        return len(directory.entries)

    _, _, _, data_size, archive_md5_size, other_md5_size, _ = struct.unpack("<IIIIIII", directory.header)
    base = directory.header_size + directory.tree_size + data_size
    with open(str(directory.path), "rb") as handle:
        handle.seek(base)
        archive_section = handle.read(archive_md5_size)
        other = handle.read(other_md5_size)
    if len(archive_section) % ARCHIVE_MD5_ENTRY.size == 0:
        for offset in range(0, len(archive_section), ARCHIVE_MD5_ENTRY.size):
            index, start, count, expected = ARCHIVE_MD5_ENTRY.unpack_from(archive_section, offset)
            if _md5_file_range(directory.archive_path(index), start, count) != expected:
                raise VpkError("MD5 mismatch in archive %d at offset %d of %s" % (index, start, path))
    if other_md5_size == 48:
        tree_md5, archive_md5, whole_md5 = other[:16], other[16:32], other[32:48]
        if _md5_file_range(directory.path, directory.header_size, directory.tree_size) != tree_md5:
            raise VpkError("tree checksum mismatch in %s" % path)
        if hashlib.md5(archive_section).digest() != archive_md5:
            raise VpkError("archive MD5 section checksum mismatch in %s" % path)
        if _md5_file_range(directory.path, 0, base + archive_md5_size + 32) != whole_md5:
            raise VpkError("whole-file checksum mismatch in %s" % path)
    return len(directory.entries)


def _copy_with_crc(source, out):
    crc = 0
    length = 0
    with open(str(source), "rb") as handle:
        while True:
            data = handle.read(BLOCK)
            if not data:
                break
            crc = zlib.crc32(data, crc)
            out.write(data)
            length += len(data)
    return crc & 0xFFFFFFFF, length


def _normalise(files):
    normalised = {}
    for archive_path, local in files.items():
        clean = archive_path.replace("\\", "/").strip("/").lower()
        if not clean or ".." in clean.split("/"):
            raise VpkError("invalid archive path: %r" % archive_path)
        if clean in normalised:
            raise VpkError("duplicate archive path: %s" % clean)
        normalised[clean] = local
    return normalised


def _build_tree(records):
    grouped = OrderedDict()
    for path in sorted(records):
        directory, name, ext = _split(path)
        grouped.setdefault(ext, OrderedDict()).setdefault(directory, []).append((name, records[path]))
    tree = bytearray()
    for ext, directories in grouped.items():
        tree += ext.encode("utf-8") + b"\0"
        for directory, names in directories.items():
            tree += directory.encode("utf-8") + b"\0"
            for name, (crc, index, offset, length) in names:
                tree += name.encode("utf-8") + b"\0"
                tree += struct.pack("<IHHIIH", crc, 0, index, offset, length, TERMINATOR)
            tree += b"\0"
        tree += b"\0"
    tree += b"\0"
    return bytes(tree)


def write(output_path, files, chunk_size=None):
    """Write a VPK v2 from ``files`` (archive path -> local file).

    If ``output_path`` ends in ``_dir.vpk`` a multi-part pack is written with
    data chunks of roughly ``chunk_size`` bytes (a file larger than that gets a
    chunk of its own). Stale chunks from earlier, larger builds are removed.
    Returns a dict describing the written files.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    files = _normalise(files)
    multipart = is_multipart_name(output_path)
    chunk_size = chunk_size or DEFAULT_CHUNK_BYTES
    records = {}
    temp_files = []

    try:
        if multipart:
            chunks = []
            index, offset, out = -1, 0, None
            for path in sorted(files):
                size = os.path.getsize(str(files[path]))
                if out is None or (offset and offset + size > chunk_size):
                    if out:
                        out.close()
                    index += 1
                    if index >= EMBEDDED_ARCHIVE:
                        raise VpkError("too many archive chunks")
                    tmp = chunk_path(output_path, index).with_suffix(".vpk.tmp")
                    temp_files.append(tmp)
                    chunks.append(tmp)
                    out, offset = open(str(tmp), "wb"), 0
                crc, length = _copy_with_crc(files[path], out)
                records[path] = (crc, index, offset, length)
                offset += length
            if out:
                out.close()
            data_tmp, data_size = None, 0
            archive_section = bytearray()
            for chunk_index, tmp in enumerate(chunks):
                chunk_len = os.path.getsize(str(tmp))
                for start in range(0, chunk_len, BLOCK):
                    count = min(BLOCK, chunk_len - start)
                    archive_section += ARCHIVE_MD5_ENTRY.pack(chunk_index, start, count, _md5_file_range(tmp, start, count))
            archive_section = bytes(archive_section)
        else:
            chunks = []
            data_tmp = output_path.with_name(output_path.name + ".data.tmp")
            temp_files.append(data_tmp)
            offset = 0
            with open(str(data_tmp), "wb") as out:
                for path in sorted(files):
                    crc, length = _copy_with_crc(files[path], out)
                    records[path] = (crc, EMBEDDED_ARCHIVE, offset, length)
                    offset += length
            data_size = offset
            archive_section = b""

        tree = _build_tree(records)
        header = struct.pack("<IIIIIII", SIGNATURE, 2, len(tree), data_size, len(archive_section), 48, 0)
        tree_md5 = hashlib.md5(tree).digest()
        archive_md5 = hashlib.md5(archive_section).digest()
        whole = hashlib.md5()
        dir_tmp = output_path.with_name(output_path.name + ".tmp")
        temp_files.append(dir_tmp)
        with open(str(dir_tmp), "wb") as out:
            for part in (header, tree):
                out.write(part)
                whole.update(part)
            if data_tmp is not None:
                with open(str(data_tmp), "rb") as data:
                    while True:
                        block = data.read(BLOCK)
                        if not block:
                            break
                        out.write(block)
                        whole.update(block)
            for part in (archive_section, tree_md5, archive_md5):
                out.write(part)
                whole.update(part)
            out.write(whole.digest())

        written = []
        for index, tmp in enumerate(chunks):
            final = chunk_path(output_path, index)
            os.replace(str(tmp), str(final))
            written.append(final)
        os.replace(str(dir_tmp), str(output_path))
        written.insert(0, output_path)
        if data_tmp is not None and data_tmp.exists():
            data_tmp.unlink()
    except BaseException:
        for tmp in temp_files:
            if tmp.exists():
                tmp.unlink()
        raise

    if multipart:
        stem = re.escape(output_path.name[: -len("_dir.vpk")])
        pattern = re.compile(r"^%s_(\d{3})\.vpk$" % stem, re.I)
        for candidate in output_path.parent.iterdir():
            match = pattern.match(candidate.name)
            if match and int(match.group(1)) >= len(chunks):
                candidate.unlink()
    return {
        "files": [str(p) for p in written],
        "chunks": len(chunks),
        "size": sum(os.path.getsize(str(p)) for p in written),
    }

