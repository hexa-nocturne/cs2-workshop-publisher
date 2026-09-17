import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.helpers import FAKE_STEAMCMD, ROOT, TempProject, clean_env, run_cli


def fake_env(scenario="success", **extra):
    record = Path(tempfile.mkdtemp(prefix="fake steamcmd ")) / "record.json"
    env = clean_env(
        STEAMCMD_PATH=str(FAKE_STEAMCMD),
        FAKE_STEAMCMD_SCENARIO=scenario,
        FAKE_STEAMCMD_RECORD=str(record),
        STEAM_USERNAME="uploader_account",
        **extra
    )
    return env, record


class CliTests(unittest.TestCase):
    def setUp(self):
        self.project = TempProject()

    def tearDown(self):
        self.project.cleanup()

    def cli(self, *args, env=None, cwd=None):
        return run_cli(["--config", self.project.config] + list(args), env=env, cwd=cwd)

    def json_cli(self, *args, env=None):
        result = self.cli("--json", *args, env=env)
        try:
            result.data = json.loads(result.out)
        except ValueError:
            self.fail("stdout is not JSON:\n%s\nstderr:\n%s" % (result.out, result.err))
        return result

    # --- basic commands -------------------------------------------------

    def test_help_and_version(self):
        result = run_cli(["--help"])
        self.assertEqual(result.returncode, 0)
        for command in ("doctor", "validate", "build", "update", "publish", "login", "tools"):
            self.assertIn(command, result.out)
        self.assertEqual(run_cli(["--version"]).returncode, 0)
        self.assertEqual(run_cli([]).returncode, 2)

    def test_validate_ok_from_other_working_directory(self):
        result = self.cli("validate", cwd=tempfile.mkdtemp())
        self.assertEqual(result.returncode, 0, result.out + result.err)
        self.assertIn("Validation passed", result.out)

    def test_doctor_reports_missing_steamcmd_with_install_help(self):
        result = self.cli("doctor")
        self.assertEqual(result.returncode, 1)
        self.assertIn("SteamCMD was not found", result.out)
        self.assertIn("STEAMCMD_PATH", result.out)

    def test_doctor_ready_with_fake_steamcmd(self):
        env, _ = fake_env()
        self.assertEqual(self.cli("build", env=env).returncode, 0)
        result = self.cli("doctor", env=env)
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn("Ready to update", result.out)

    def test_malformed_config(self):
        self.project.config.write_text("{ not json", encoding="utf-8")
        result = self.json_cli("validate")
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid JSON", result.data["error"])

    # --- validation failures ------------------------------------------

    def test_invalid_content_directory(self):
        self.project.edit(build=None, contentDirectory="./does not exist")
        result = self.cli("validate")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Workshop content directory does not exist", result.out)

    def test_missing_and_oversized_preview(self):
        self.project.edit(previewImage="./assets/missing.png")
        result = self.cli("validate")
        self.assertEqual(result.returncode, 1)
        self.assertIn("preview image does not exist", result.out)
        big = self.project.root / "assets" / "big.png"
        big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (1024 * 1024))
        self.project.edit(previewImage="./assets/big.png")
        result = self.cli("validate")
        self.assertIn("exceeds the supported size", result.out)

    def test_sensitive_file_in_content_is_refused(self):
        self.project.edit(build=None, contentDirectory="./content")
        (self.project.root / "content" / ".env").write_text("STEAM_PASSWORD=x")
        result = self.cli("validate")
        self.assertEqual(result.returncode, 1)
        self.assertIn("sensitive", result.out)

    # --- dry run ----------------------------------------------------------

    def test_update_dry_run_json(self):
        result = self.json_cli("update", "--dry-run", "--change-note", 'Linux "publishing" support')
        self.assertEqual(result.returncode, 0, result.err)
        data = result.data
        self.assertTrue(data["ok"])
        self.assertTrue(data["dryRun"])
        self.assertEqual(data["vdf"]["publishedfileid"], "1234567890")
        self.assertEqual(data["vdf"]["changenote"], 'Linux "publishing" support')
        self.assertTrue(os.path.isabs(data["vdf"]["contentfolder"]))
        self.assertIn("my project dir", data["vdf"]["contentfolder"])
        self.assertNotIn("title", data["vdf"])  # metadata only with --update-metadata
        self.assertTrue((self.project.root / "build" / "workshop" / "panorama").is_dir())  # build ran

    def test_publish_dry_run_without_id(self):
        self.project.edit(publishedFileId="")
        result = self.json_cli("publish", "--dry-run")
        self.assertEqual(result.returncode, 0, result.err)
        self.assertEqual(result.data["vdf"]["publishedfileid"], "0")
        self.assertEqual(result.data["vdf"]["visibility"], "2")
        self.assertIn("title", result.data["vdf"])

    # --- publish/update guards -------------------------------------------

    def test_update_without_id_explains_first_publish(self):
        self.project.edit(publishedFileId="")
        result = self.cli("update", "--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("first publish", result.out)

    def test_publish_refuses_when_id_exists(self):
        result = self.cli("publish", "--yes", "--dry-run")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Refusing to create a second Workshop item", result.out)

    def test_publish_requires_confirmation(self):
        self.project.edit(publishedFileId="")
        env, _ = fake_env()
        result = self.cli("publish", env=env)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--yes", result.out)

    def test_missing_steamcmd_exit_code(self):
        env = clean_env(STEAM_USERNAME="uploader_account")
        result = self.cli("update", "--no-verify", env=env)
        self.assertEqual(result.returncode, 3)
        self.assertIn("SteamCMD was not found", result.out)

    def test_wrong_steamcmd_path(self):
        env = clean_env(STEAM_USERNAME="u", STEAMCMD_PATH="/nonexistent/steamcmd.sh")
        result = self.cli("update", "--no-verify", env=env)
        self.assertEqual(result.returncode, 3)
        self.assertIn("STEAMCMD_PATH points to a missing file", result.out)

    def test_missing_username(self):
        env, _ = fake_env()
        del env["STEAM_USERNAME"]
        result = self.cli("update", "--no-verify", env=env)
        self.assertEqual(result.returncode, 4)
        self.assertIn("No Steam username", result.out)

    # --- uploads with the fake SteamCMD -------------------------------------

    def test_successful_update_keeps_secrets_out_of_argv_and_output(self):
        env, record = fake_env(STEAM_PASSWORD="Very-Secret-Pw1")
        result = self.json_cli("update", "--no-verify", "--change-note", "Update", "--verbose", env=env)
        self.assertEqual(result.returncode, 0, result.err)
        self.assertTrue(result.data["ok"])
        self.assertEqual(result.data["steamcmd"]["outcome"], "success")
        rec = json.loads(record.read_text())
        self.assertNotIn("Very-Secret-Pw1", " ".join(rec["argv"]))
        self.assertIn('login "uploader_account" "Very-Secret-Pw1"', rec["script"])
        self.assertIn('"publishedfileid"', rec["vdf"])
        self.assertIn("1234567890", rec["vdf"])
        self.assertNotIn("Very-Secret-Pw1", result.out + result.err)
        # the private runscript directory is removed after the run
        self.assertFalse(Path(rec["script_path"]).exists())
        self.assertFalse(Path(rec["script_path"]).parent.exists())
        if os.name != "nt":
            self.assertEqual(rec["script_mode"], "0o600")
            self.assertEqual(rec["dir_mode"], "0o700")

    def test_first_publish_saves_new_id(self):
        self.project.edit(publishedFileId="")
        env, _ = fake_env()
        result = self.json_cli("publish", "--yes", "--no-verify", "--change-note", "Initial release", env=env)
        self.assertEqual(result.returncode, 0, result.err)
        self.assertEqual(result.data["publishedFileId"], "3999999999")
        self.assertEqual(self.project.data()["publishedFileId"], "3999999999")

    def test_bad_password(self):
        env, _ = fake_env("bad_password", STEAM_PASSWORD="wrong-password")
        result = self.cli("update", "--no-verify", env=env)
        self.assertEqual(result.returncode, 4)
        self.assertIn("authentication was rejected", result.out)
        self.assertNotIn("wrong-password", result.out + result.err)

    def test_access_denied(self):
        env, _ = fake_env("access_denied")
        result = self.cli("update", "--no-verify", env=env)
        self.assertEqual(result.returncode, 5)
        self.assertIn("Access Denied", result.out)

    def test_steam_guard_prompt_fails_fast_when_unattended(self):
        env, _ = fake_env("guard")
        started = time.time()
        result = self.cli("update", "--no-verify", env=env)
        self.assertEqual(result.returncode, 4, result.out)
        self.assertIn("non-interactive", result.out)
        self.assertLess(time.time() - started, 25)

    def test_uncertain_outcome_is_reported(self):
        env, _ = fake_env("silent")
        result = self.cli("update", "--no-verify", env=env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("did not print a recognisable success line", result.out)

    def test_credentials_file_via_environment(self):
        creds = Path(tempfile.mkdtemp(prefix="etc project ")) / "steam-uploader.env"
        creds.write_text("STEAM_USERNAME=file_uploader\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(str(creds), 0o600)
        env, record = fake_env()
        del env["STEAM_USERNAME"]
        env["WORKSHOP_CREDENTIALS_FILE"] = str(creds)
        result = self.cli("update", "--no-verify", env=env)
        self.assertEqual(result.returncode, 0, result.out)
        self.assertIn('login "file_uploader"', json.loads(record.read_text())["script"])


class UnexpectedErrorTests(unittest.TestCase):
    def test_json_output_survives_unexpected_exceptions(self):
        import contextlib
        import io
        from unittest import mock

        from workshop_publisher import cli

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(cli.COMMANDS, {"validate": mock.Mock(side_effect=PermissionError("denied"))}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(["--json", "validate"])
        data = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertIn("PermissionError", data["error"])


class PackListTests(unittest.TestCase):
    def test_compare(self):
        from workshop_publisher import vpk

        base = Path(tempfile.mkdtemp(prefix="pack list "))
        a, b = base / "a.bin", base / "b.bin"
        a.write_bytes(b"a")
        b.write_bytes(b"b")
        old, new = base / "live_dir.vpk", base / "new.vpk"
        vpk.write(old, {"x/one.vjs_c": a, "x/two.vjs_c": a}, chunk_size=1)
        vpk.write(new, {"x/one.vjs_c": b, "x/three.vjs_c": a})
        result = run_cli(["--json", "pack-list", new, "--compare", old, "--verify"])
        self.assertEqual(result.returncode, 0, result.err)
        diff = json.loads(result.out)["compare"]["diff"]
        self.assertEqual((diff["added"], diff["removed"], diff["changed"]), (["x/three.vjs_c"], ["x/two.vjs_c"], ["x/one.vjs_c"]))


@unittest.skipIf(os.name == "nt" or shutil.which("bash") is None, "bash launcher is tested on POSIX")
class LauncherTests(unittest.TestCase):
    def test_bash_launcher_from_other_directory(self):
        launcher = str(ROOT / "workshop").replace("\\", "/")
        completed = subprocess.run(["bash", launcher, "--help"], cwd=tempfile.mkdtemp(), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(b"publish", completed.stdout)


if __name__ == "__main__":
    unittest.main()
