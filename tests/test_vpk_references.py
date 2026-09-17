import os
import struct
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT  # noqa: F401
from workshop_publisher import manifest, references, vpk


class VpkTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vpk test "))
        self.files = {
            "panorama/layout/rank.vxml_c": b"<xml/>" * 10,
            "panorama/images/ranks/rank_1_png.vtex_c": os.urandom(5000),
            "sounds/ui/levelup.vsnd_c": os.urandom(1234),
            "models/weapons/knife.vmdl_c": b"",
            "materials/big.vtex_c": os.urandom(3 * 1024 * 1024 + 17),
            "readme": b"no extension",
        }
        self.mapping = {}
        for rel, data in self.files.items():
            local = self.dir / "src" / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            self.mapping[rel] = local

    def check_contents(self, dir_file):
        self.assertEqual(vpk.verify(dir_file), len(self.files))
        directory = vpk.read_directory(dir_file)
        self.assertEqual(directory.version, 2)
        self.assertEqual(set(directory.entries), set(self.files))
        for rel, data in self.files.items():
            self.assertEqual(vpk.read_file(dir_file, rel), data)

    def test_single_file_round_trip(self):
        out = self.dir / "out dir" / "addon.vpk"
        info = vpk.write(out, self.mapping)
        self.assertEqual(info["chunks"], 0)
        self.check_contents(out)

    def test_multipart_round_trip_and_stale_chunk_cleanup(self):
        out = self.dir / "pak01_dir.vpk"
        info = vpk.write(out, self.mapping, chunk_size=1024 * 1024)
        self.assertEqual(info["chunks"], 2)  # the 3 MB file gets its own chunk
        self.assertTrue((self.dir / "pak01_000.vpk").is_file())
        self.assertTrue((self.dir / "pak01_001.vpk").is_file())
        self.check_contents(out)
        entries = vpk.read_directory(out).entries
        self.assertNotEqual(entries["readme"].archive_index, vpk.EMBEDDED_ARCHIVE)
        # A later build with fewer chunks removes the stale ones.
        (self.dir / "pak01_005.vpk").write_bytes(b"stale")
        info = vpk.write(out, {"readme": self.mapping["readme"]}, chunk_size=1024 * 1024)
        self.assertEqual(info["chunks"], 1)
        self.assertFalse((self.dir / "pak01_001.vpk").exists())
        self.assertFalse((self.dir / "pak01_005.vpk").exists())
        self.assertEqual(vpk.verify(out), 1)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.suffix == ".vpk"), ["pak01_000.vpk", "pak01_dir.vpk"])

    def test_extract_multipart(self):
        out = self.dir / "pak01_dir.vpk"
        vpk.write(out, self.mapping, chunk_size=1024 * 1024)
        dest = self.dir / "extracted here"
        self.assertEqual(vpk.extract(out, dest), len(self.files))
        for rel, data in self.files.items():
            self.assertEqual((dest / rel).read_bytes(), data)
        with self.assertRaises(vpk.VpkError):
            vpk.extract(out, dest)  # refuses to overwrite by default
        vpk.extract(out, dest, overwrite=True)

    def test_missing_chunk_reported(self):
        out = self.dir / "pak01_dir.vpk"
        vpk.write(out, self.mapping, chunk_size=1024 * 1024)
        (self.dir / "pak01_001.vpk").unlink()
        self.assertEqual(len(vpk.list_files(out)), len(self.files))  # listing needs only the directory
        with self.assertRaises(vpk.VpkError) as ctx:
            vpk.verify(out)
        self.assertIn("missing", str(ctx.exception))

    def test_valve_flagged_hash_entries_are_not_verifiable_but_not_errors(self):
        out = self.dir / "pak01_dir.vpk"
        vpk.write(out, self.mapping, chunk_size=1024 * 1024)
        directory = vpk.read_directory(out)
        header = struct.unpack("<IIIIIII", directory.header)
        start = directory.header_size + directory.tree_size + header[3]
        blob = bytearray(out.read_bytes())
        # Imitate Valve's newer format: flag 0x10000 on chunk numbers, non-MD5 hashes.
        for offset in range(start, start + header[4], vpk.ARCHIVE_MD5_ENTRY.size):
            index, begin, count, _ = vpk.ARCHIVE_MD5_ENTRY.unpack_from(blob, offset)
            vpk.ARCHIVE_MD5_ENTRY.pack_into(blob, offset, index | 0x10000, begin, count, b"\x01" * 16)
        out.write_bytes(bytes(blob))
        notes = []
        self.assertEqual(vpk.verify(out, notes), len(self.files))
        self.assertTrue(any("BLAKE3" in note for note in notes))
        # File data corruption is still detected through the CRCs.
        chunk = self.dir / "pak01_000.vpk"
        data = bytearray(chunk.read_bytes())
        data[10] ^= 0xFF
        chunk.write_bytes(bytes(data))
        with self.assertRaises(vpk.VpkError):
            vpk.verify(out, [])

    def test_corruption_detected(self):
        for name, chunk in (("addon.vpk", None), ("pak01_dir.vpk", 1024 * 1024)):
            out = self.dir / name
            vpk.write(out, self.mapping, chunk_size=chunk)
            target = out if chunk is None else self.dir / "pak01_000.vpk"
            blob = bytearray(target.read_bytes())
            blob[len(blob) // 2] ^= 0xFF
            target.write_bytes(bytes(blob))
            with self.assertRaises(vpk.VpkError):
                vpk.verify(out)

    def test_rejects_bad_files(self):
        bad = self.dir / "bad.vpk"
        bad.write_bytes(struct.pack("<III", 0x12345678, 2, 0))
        with self.assertRaises(vpk.VpkError):
            vpk.list_files(bad)
        with self.assertRaises(vpk.VpkError):
            vpk.write(self.dir / "x.vpk", {"../escape.txt": self.mapping["readme"]})
        self.assertFalse(list(self.dir.glob("*.tmp")))

    def test_manifest_diff(self):
        out = self.dir / "addon.vpk"
        vpk.write(out, self.mapping)
        first = manifest.from_vpk(out)
        mapping = dict(self.mapping)
        del mapping["readme"]
        changed = self.dir / "src" / "sounds" / "ui" / "levelup.vsnd_c"
        changed.write_bytes(b"different")
        new_file = self.dir / "src" / "new.vjs_c"
        new_file.write_bytes(b"js")
        mapping["panorama/scripts/new.vjs_c"] = new_file
        vpk.write(out, mapping)
        second = manifest.from_vpk(out)
        diff = manifest.diff(first, second)
        self.assertEqual(diff["added"], ["panorama/scripts/new.vjs_c"])
        self.assertEqual(diff["removed"], ["readme"])
        self.assertEqual(diff["changed"], ["sounds/ui/levelup.vsnd_c"])
        self.assertEqual(len(manifest.diff(None, second)["added"]), second["fileCount"])


class ReferenceTests(unittest.TestCase):
    def test_panorama_and_kv3_references(self):
        sources = {
            "panorama/layout/hud.xml": '<Image src="file://{images}/ranks/rank_1.png" />\n'
                                       '<include src="s2r://panorama/styles/hud.vcss_c" />',
            "panorama/styles/hud.css": ".x { background-image: url(\"file://{images}/icons/missing.svg\"); }",
            "panorama/scripts/hud.js": 'var p = "file://{images}/ranks/rank_" + n + ".png";',
            "materials/knife.vmat": 'TextureColor "materials/knife_color.png"',
            "soundevents/soundevents_addon.vsndevts": 'vsnd_files = "sounds/ui/levelup.vsnd"',
            "panorama/images/ranks/rank_1.png": None,
            "sounds/ui/levelup.wav": None,
        }
        texts = {k: v for k, v in sources.items() if v is not None}
        refs = references.scan_sources(texts)
        texts_found = sorted(r.text for r in refs)
        self.assertEqual(len(refs), 5, texts_found)  # the dynamic JS path is skipped
        missing = references.find_missing(refs, sources.keys(), base_files={"panorama/styles/hud.vcss_c"})
        self.assertEqual(sorted(m.candidates[0] for m in missing),
                         ["materials/knife_color.png", "panorama/images/icons/missing.svg"])
        # base game compiled texture satisfies a reference
        missing = references.find_missing(refs, sources.keys(), base_files={
            "panorama/styles/hud.vcss_c", "panorama/images/icons/missing_svg.vsvg_c", "materials/knife_color_png.vtex_c"})
        self.assertEqual(missing, [])
        ignored = references.find_missing(refs, sources.keys(), base_files={"panorama/styles/hud.vcss_c"},
                                          ignore=["materials/*", "panorama/images/icons/*"])
        self.assertEqual(ignored, [])

    def test_compiled_names_resolve_to_sources(self):
        refs = references.scan_sources({"panorama/layout/a.xml": "\n".join([
            '<include src="s2r://panorama/styles/x.vcss_c" />',
            '<include src="s2r://panorama/scripts/y.vjs_c" />',
            '<include src="s2r://panorama/layout/z.vxml_c" />',
            '<Image src="s2r://panorama/images/icon_png.vtex_c" />',
            '<Image src="s2r://panorama/images/logo_png.vtex" />',
        ])})
        sources = ["panorama/styles/x.css", "panorama/scripts/y.js", "panorama/layout/z.xml",
                   "panorama/images/icon.png", "panorama/images/logo.png"]
        self.assertEqual(references.find_missing(refs, sources), [])
        self.assertEqual(len(references.find_missing(refs, [])), 5)
        # Ignore patterns may name the source, the compiled file or the reference text.
        for pattern in ("panorama/styles/*.css", "*.vcss_c", "s2r://panorama/styles/*"):
            missing = references.find_missing(refs[:1], [], ignore=[pattern])
            self.assertEqual(missing, [], pattern)

    def test_dependents(self):
        refs = references.scan_sources({"panorama/layout/hud.xml": '<Image src="file://{images}/ranks/r.png"/>'})
        self.assertEqual(references.dependents(refs, ["panorama/images/ranks/r.png"]), {"panorama/layout/hud.xml"})
        self.assertEqual(references.dependents(refs, ["other.png"]), set())


if __name__ == "__main__":
    unittest.main()
