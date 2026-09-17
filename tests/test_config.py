import os
import tempfile
import unittest
from pathlib import Path

from tests.helpers import TempProject
from workshop_publisher.config import ConfigError, load_config, load_dotenv, save_published_file_id


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.project = TempProject()

    def tearDown(self):
        self.project.cleanup()

    def test_demo_config_loads_with_spaces_in_path(self):
        cfg = load_config(self.project.config)
        self.assertEqual(cfg.app_id, 730)
        self.assertEqual(cfg.published_file_id, "1234567890")
        self.assertTrue(cfg.content_dir.is_absolute())
        self.assertIn("my project dir", str(cfg.content_dir))
        self.assertEqual(cfg.visibility, 2)

    def test_malformed_json_reports_location(self):
        self.project.config.write_text('{\n  "appId": 730,\n  "contentDirectory": ./x\n}', encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.project.config)
        self.assertIn("line 3", ctx.exception.problems[0])

    def test_missing_file(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.project.root / "nope.json")
        self.assertIn("not found", ctx.exception.problems[0])

    def test_collects_all_problems(self):
        self.project.edit(appId="abc", publishedFileId="12ab", contentDirectory=None, visibility="everyone")
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.project.config)
        text = "\n".join(ctx.exception.problems)
        for fragment in ("appId", "publishedFileId", "contentDirectory", "visibility"):
            self.assertIn(fragment, text)

    def test_numeric_id_warning_and_unknown_key(self):
        self.project.edit(publishedFileId=2000000001, contnetDirectory="typo")
        cfg = load_config(self.project.config)
        self.assertEqual(cfg.published_file_id, "2000000001")
        self.assertTrue(any("string" in w for w in cfg.warnings))
        self.assertTrue(any("contnetDirectory" in w for w in cfg.warnings))

    def test_backslash_relative_paths_are_portable(self):
        self.project.edit(contentDirectory=".\\build\\workshop")
        cfg = load_config(self.project.config)
        self.assertTrue(str(cfg.content_dir).replace("\\", "/").endswith("build/workshop"))

    @unittest.skipIf(os.name == "nt", "POSIX-only behaviour")
    def test_windows_absolute_path_rejected_on_posix(self):
        self.project.edit(contentDirectory="C:\\Users\\someone\\workshop")
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.project.config)
        self.assertIn("Windows-only", ctx.exception.problems[0])

    def test_save_published_file_id(self):
        self.project.edit(publishedFileId="")
        cfg = load_config(self.project.config)
        save_published_file_id(cfg, "3999999999")
        data = self.project.data()
        self.assertEqual(data["publishedFileId"], "3999999999")
        self.assertEqual(list(data)[:3], ["$comment", "appId", "publishedFileId"])
        with self.assertRaises(ConfigError):
            save_published_file_id(load_config(self.project.config), "4000000000")

    def test_invalid_build_step(self):
        self.project.edit(build={"steps": [{"run": "rm -rf /"}]})
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.project.config)
        self.assertIn("list of strings", ctx.exception.problems[0])

    def test_example_config_is_valid(self):
        from tests.helpers import ROOT

        cfg = load_config(ROOT / "workshop.example.json")
        self.assertEqual(cfg.app_id, 730)
        self.assertEqual(cfg.build.steps[0].kind, "cs2")

    def test_dotenv_does_not_override_environment(self):
        env_file = Path(tempfile.mkdtemp()) / ".env"
        env_file.write_text('# comment\nSTEAM_USERNAME="uploader"\nexport STEAMCMD_PATH=/opt/s # note\nEMPTY=\n', encoding="utf-8")
        env = {"STEAM_USERNAME": "already-set"}
        loaded = load_dotenv(env_file, env)
        self.assertEqual(env["STEAM_USERNAME"], "already-set")
        self.assertEqual(env["STEAMCMD_PATH"], "/opt/s")
        self.assertEqual(loaded, ["STEAMCMD_PATH"])


if __name__ == "__main__":
    unittest.main()
