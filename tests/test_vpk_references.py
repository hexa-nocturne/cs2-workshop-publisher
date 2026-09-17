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
            "readme": b"no extension",
        }
        self.mapping = {}
        for rel, data in self.files.items():
            local = self.dir / "src" / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            self.mapping[rel] = local

    def test_round_trip(self):
        out = vpk.write(self.dir / "out dir" / "addon.vpk", self.mapping)
        self.assertEqual(vpk.verify(out), len(self.files))
        version, _, _, entries = vpk.read_directory(out)
        self.assertEqual(version, 2)
        self.assertEqual(set(entries), set(self.files))
        for rel, data in self.files.items():
            self.assertEqual(vpk.read_file(out, rel), data)

    def test_corruption_detected(self):
        out = vpk.write(self.dir / "addon.vpk", self.mapping)
        blob = bytearray(out.read_bytes())
        blob[-60] ^= 0xFF  # inside the file data
        out.write_bytes(bytes(blob))
        with self.assertRaises(vpk.VpkError):
            vpk.verify(out)

    def test_rejects_bad_files(self):
        bad = self.dir / "bad.vpk"
        bad.write_bytes(struct.pack("<III", 0x12345678, 2, 0))
        with self.assertRaises(vpk.VpkError):
            vpk.list_files(bad)
        with self.assertRaises(vpk.VpkError):
            vpk.write(self.dir / "x.vpk", {"../escape.txt": self.mapping["readme"]})

    def test_manifest_diff(self):
        out = vpk.write(self.dir / "addon.vpk", self.mapping)
        first = manifest.from_vpk(out)
        mapping = dict(self.mapping)
        del mapping["readme"]
        changed = self.dir / "src" / "sounds" / "ui" / "levelup.vsnd_c"
        changed.write_bytes(b"different")
        new_file = self.dir / "src" / "new.vjs_c"
        new_file.write_bytes(b"js")
        mapping["panorama/scripts/new.vjs_c"] = new_file
        second = manifest.from_vpk(vpk.write(self.dir / "addon.vpk", mapping))
        diff = manifest.diff(first, second)
        self.assertEqual(diff["added"], ["panorama/scripts/new.vjs_c"])
        self.assertEqual(diff["removed"], ["readme"])
        self.assertEqual(diff["changed"], ["sounds/ui/levelup.vsnd_c"])
        self.assertEqual(manifest.diff(None, second)["added"].__len__(), second["fileCount"])


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

    def test_dependents(self):
        refs = references.scan_sources({"panorama/layout/hud.xml": '<Image src="file://{images}/ranks/r.png"/>'})
        self.assertEqual(references.dependents(refs, ["panorama/images/ranks/r.png"]), {"panorama/layout/hud.xml"})
        self.assertEqual(references.dependents(refs, ["other.png"]), set())


if __name__ == "__main__":
    unittest.main()
