import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import ROOT
from workshop_publisher import cs2, vpk
from workshop_publisher.build import BuildError, run_build
from workshop_publisher.config import load_config
from workshop_publisher.console import Console

FAKE_COMPILER = ROOT / "tests" / "fake_compiler.py"


class Cs2PipelineTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="cs2 test "))
        self.project = self.base / "addon content"
        self.tools = self.base / "cs2 windows"
        (self.tools / "game" / "bin" / "win64").mkdir(parents=True)
        (self.tools / "game" / "bin" / "win64" / "resourcecompiler.exe").write_bytes(b"MZ")
        src = self.project / "content"
        self.write(src / "panorama/layout/ranks.xml", '<Image src="file://{images}/ranks/rank_1.png" />')
        self.write(src / "panorama/styles/ranks.css", ".rank { width: 10px; }")
        self.write(src / "panorama/images/ranks/rank_1.png", "PNGDATA")
        self.write(src / "sounds/ui/levelup.wav", "RIFF")
        self.compile_log = self.base / "compiled.log"
        self.env = dict(os.environ, CS2_TOOLS_DIR=str(self.tools), FAKE_COMPILER_LOG=str(self.compile_log))
        self.env.pop("FAKE_COMPILER_FAIL", None)
        self.config = {
            "appId": 730,
            "publishedFileId": "2000000001",
            "contentDirectory": "./build/workshop",
            "build": {"steps": [{"name": "CS2", "cs2": {
                "addon": "my_addon",
                "source": "./content",
                "vpk": "{content}/{publishedFileId}.vpk",
                "runtime": "native",
                "compile": [sys.executable, str(FAKE_COMPILER), "{input}", "{addon_content}", "{addon_game}"],
                "resources": {"nice": 0, "ioniceIdle": False},
                "referenceCheck": {"mode": "error"},
            }}]},
        }
        self.save_config()

    def tearDown(self):
        shutil.rmtree(str(self.base), ignore_errors=True)

    def write(self, path, text):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def save_config(self):
        self.write(self.project / "workshop.json", json.dumps(self.config, indent=2))

    def build(self, clean=False):
        if self.compile_log.exists():
            self.compile_log.unlink()
        cfg = load_config(self.project / "workshop.json")
        results = run_build(cfg, Console(stream=io.StringIO(), color=False), self.env, clean=clean)
        compiled = self.compile_log.read_text().split() if self.compile_log.exists() else []
        return results[0], compiled

    def test_full_then_incremental_builds(self):
        result, compiled = self.build()
        self.assertTrue(result["fullRebuild"])
        self.assertEqual(sorted(compiled), ["panorama/layout/ranks.xml", "panorama/styles/ranks.css", "sounds/ui/levelup.wav"])
        vpk_path = self.project / "build" / "workshop" / "2000000001.vpk"
        self.assertTrue(vpk_path.is_file())
        names = sorted(e.path for e in vpk.list_files(vpk_path))
        self.assertEqual(names, ["panorama/layout/ranks.vxml_c", "panorama/styles/ranks.vcss_c", "sounds/ui/levelup.vsnd_c"])
        self.assertEqual(len(result["diff"]["added"]), 3)
        self.assertTrue((self.project / "build-cache" / "manifests" / "latest.json").is_file())

        result, compiled = self.build()
        self.assertFalse(result["fullRebuild"])
        self.assertEqual(compiled, [])
        self.assertEqual(result["diff"]["added"], [])

        # Changing an image recompiles only the layout that references it.
        self.write(self.project / "content/panorama/images/ranks/rank_1.png", "NEWPNG")
        result, compiled = self.build()
        self.assertEqual(compiled, ["panorama/layout/ranks.xml"])

        # Editing a sound recompiles only that sound; the manifest diff shows the change.
        self.write(self.project / "content/sounds/ui/levelup.wav", "RIFF2")
        result, compiled = self.build()
        self.assertEqual(compiled, ["sounds/ui/levelup.wav"])
        self.assertEqual(result["diff"]["changed"], ["sounds/ui/levelup.vsnd_c"])

    def test_deleted_source_triggers_clean_rebuild(self):
        self.build()
        (self.project / "content/panorama/styles/ranks.css").unlink()
        result, compiled = self.build()
        self.assertTrue(result["fullRebuild"])
        self.assertIn("removed", result["reason"])
        self.assertEqual(result["diff"]["removed"], ["panorama/styles/ranks.vcss_c"])
        self.assertFalse((self.tools / "content/csgo_addons/my_addon/panorama/styles/ranks.css").exists())

    def test_compile_failure_is_retried_next_time(self):
        self.env["FAKE_COMPILER_FAIL"] = "sounds/ui/levelup.wav"
        with self.assertRaises(BuildError) as ctx:
            self.build()
        self.assertIn("failed to compile", str(ctx.exception))
        self.env.pop("FAKE_COMPILER_FAIL")
        result, compiled = self.build()
        self.assertEqual(len(compiled), 3)  # no state was saved after the failure

    def test_missing_reference_stops_before_compiling(self):
        self.write(self.project / "content/panorama/layout/ranks.xml", '<Image src="file://{images}/ranks/nope.png" />')
        with self.assertRaises(BuildError) as ctx:
            self.build()
        self.assertIn("reference", str(ctx.exception))
        self.assertFalse(self.compile_log.exists())

    def test_missing_compiler_reports_install_command(self):
        shutil.rmtree(str(self.tools / "game"))
        with self.assertRaises(BuildError) as ctx:
            self.build()
        self.assertIn("workshop tools install", str(ctx.exception))

    def step(self, **changes):
        self.config["build"]["steps"][0]["cs2"].update(changes)
        self.save_config()

    def test_prebuilt_files_are_merged_and_compiled_files_win(self):
        prebuilt = self.project / "prebuilt"
        self.write(prebuilt / "models/agents/agent.vmdl_c", "THIRD-PARTY")
        self.write(prebuilt / "panorama/styles/ranks.vcss_c", "OLD PREBUILT")
        self.step(prebuilt=["./prebuilt"], vpk="{content}/pak01_dir.vpk", vpkChunkMB=1)
        result, _ = self.build()
        dir_file = self.project / "build" / "workshop" / "pak01_dir.vpk"
        self.assertEqual(result["prebuiltFiles"], 1)
        self.assertEqual(result["overriddenPrebuilt"], ["panorama/styles/ranks.vcss_c"])
        self.assertEqual(vpk.read_file(dir_file, "models/agents/agent.vmdl_c"), b"THIRD-PARTY")
        self.assertTrue(vpk.read_file(dir_file, "panorama/styles/ranks.vcss_c").startswith(b"compiled:"))
        self.assertGreaterEqual(result["vpkChunks"], 1)

    def test_prebuilt_only_pack_needs_no_compiler(self):
        shutil.rmtree(str(self.tools / "game"))
        shutil.rmtree(str(self.project / "content"))
        self.write(self.project / "prebuilt/materials/a.vmat_c", "A")
        self.step(prebuilt=["./prebuilt"], source="./content")
        result, compiled = self.build()
        self.assertEqual(compiled, [])
        self.assertEqual(result["fileCount"], 1)

    def test_prebuilt_satisfies_references(self):
        self.write(self.project / "content/panorama/layout/ranks.xml", '<Image src="file://{images}/agents/icon.png" />')
        with self.assertRaises(BuildError):
            self.build()
        self.write(self.project / "prebuilt/panorama/images/agents/icon_png.vtex_c", "ICON")
        self.step(prebuilt=["./prebuilt"])
        self.build()

    def test_missing_prebuilt_directory(self):
        self.step(prebuilt=["./does-not-exist"])
        with self.assertRaises(BuildError) as ctx:
            self.build()
        self.assertIn("prebuilt directory does not exist", str(ctx.exception))

    def test_parallel_jobs(self):
        for i in range(12):
            self.write(self.project / ("content/sounds/ui/s%02d.wav" % i), "RIFF%d" % i)
        self.step(jobs=4)
        result, _ = self.build()  # the fake compiler's shared log is not safe for concurrent appends
        self.assertEqual(len(result["compiled"]), 15)
        self.assertEqual(result["fileCount"], 15)

    def test_batch_mode(self):
        self.step(compileMode="batch", batchSize=2,
                  compileBatch=[sys.executable, str(FAKE_COMPILER), "--filelist", "{filelist}", "{addon_content}", "{addon_game}"])
        result, compiled = self.build()
        self.assertEqual(sorted(compiled), ["panorama/layout/ranks.xml", "panorama/styles/ranks.css", "sounds/ui/levelup.wav"])
        self.assertEqual(result["fileCount"], 3)

    def test_compile_setting_change_forces_full_build(self):
        self.build()
        self.config["build"]["steps"][0]["cs2"]["compile"].append("--extra")
        self.save_config()
        result, compiled = self.build()
        self.assertTrue(result["fullRebuild"])


class Cs2UnitTests(unittest.TestCase):
    def settings(self, **extra):
        spec = {"addon": "my_addon", "vpk": "{content}/x.vpk"}
        spec.update(extra)
        return cs2.Cs2Settings(spec)

    def test_settings_validation(self):
        for bad in ({"addon": "Bad Name", "vpk": "a.vpk"}, {"addon": "ok"}, {"addon": "ok", "vpk": "a.vpk", "compile": ["x"]},
                    {"addon": "ok", "vpk": "a.vpk", "typo": 1}):
            with self.assertRaises(ValueError):
                cs2.Cs2Settings(bad)

    def test_wine_path(self):
        self.assertEqual(cs2.to_wine_path("/home/builder/cs2 tools/game"), "Z:\\home\\builder\\cs2 tools\\game")

    def test_wine_command_with_limits_and_xvfb(self):
        settings = self.settings(resources={"nice": 10, "ioniceIdle": True, "memoryMax": "6G", "cpuQuota": "200%"})
        paths = mock.Mock(compiler=Path("/opt/cs2/game/bin/win64/resourcecompiler.exe"), addon_content=Path("/opt/cs2/content/csgo_addons/my_addon"),
                          addon_game=Path("/opt/cs2/game/csgo_addons/my_addon"), tools=Path("/opt/cs2"))

        def which(name):
            return "/usr/bin/" + name

        with mock.patch.object(cs2, "current_os", return_value="linux"):
            argv = cs2.compiler_argv(settings, paths, {}, {"{input}": Path("/opt/cs2/content/csgo_addons/my_addon/a.xml")}, which=which)
        text = " ".join(argv).replace("\\\\", "\\")
        self.assertEqual(argv[:4], ["systemd-run", "--user", "--scope", "--quiet"])
        self.assertIn("MemoryMax=6G", argv)
        self.assertIn("CPUQuota=200%", argv)
        self.assertIn("nice", argv)
        self.assertIn("ionice", argv)
        self.assertIn("/usr/bin/xvfb-run", argv)
        self.assertIn("/usr/bin/wine", argv)
        self.assertIn("Z:\\opt\\cs2\\content\\csgo_addons\\my_addon\\a.xml", text)
        self.assertLess(argv.index("/usr/bin/xvfb-run"), argv.index("/usr/bin/wine"))

    def test_plan_changes(self):
        full, reason, changed, deleted = cs2.plan_changes({"a": "1"}, None, "fp")
        self.assertTrue(full)
        state = {"fingerprint": "fp", "files": {"a": "1", "b": "2"}}
        self.assertEqual(cs2.plan_changes({"a": "1", "b": "3"}, state, "fp")[2], ["b"])
        self.assertTrue(cs2.plan_changes({"a": "1"}, state, "fp")[0])
        self.assertTrue(cs2.plan_changes({"a": "1", "b": "2"}, state, "other")[0])


if __name__ == "__main__":
    unittest.main()
