import io
import json
import unittest

from tests.helpers import ROOT  # noqa: F401
from workshop_publisher import steamapi, steamcmd
from workshop_publisher.console import Console, Redactor
from workshop_publisher.credentials import Credentials


class AnalyzeOutputTests(unittest.TestCase):
    def outcome(self, text, code=0, blocked=None):
        return steamcmd.analyze_output(text, code, blocked)

    def test_success_update(self):
        result = self.outcome("Preparing update...\nUploading content...\nCommitting update...Success.\n")
        self.assertEqual(result.outcome, steamcmd.Outcome.SUCCESS)

    def test_invalid_password(self):
        result = self.outcome("Logging in user 'x' to Steam Public...FAILED (Invalid Password)\n", 5)
        self.assertEqual(result.outcome, steamcmd.Outcome.AUTH)
        self.assertIn("rejected", result.message)

    def test_rate_limited(self):
        self.assertIn("too many", self.outcome("FAILED (Rate Limit Exceeded)", 5).message)

    def test_steam_guard_required(self):
        result = self.outcome("FAILED (Account Logon Denied)\n", 5)
        self.assertEqual(result.outcome, steamcmd.Outcome.STEAM_GUARD)

    def test_blocked_prompt(self):
        result = self.outcome("Steam Guard code:", 1, blocked="Steam Guard code:")
        self.assertEqual(result.outcome, steamcmd.Outcome.STEAM_GUARD)
        self.assertIn("workshop login", result.message)

    def test_workshop_errors(self):
        denied = self.outcome("ERROR! Failed to update workshop item (Access Denied).\n")
        self.assertEqual(denied.outcome, steamcmd.Outcome.UPLOAD)
        self.assertIn("legal agreement", denied.message)
        limit = self.outcome("ERROR! Failed to update workshop item (Limit Exceeded).\n")
        self.assertIn("preview image", limit.message)

    def test_missing_32bit_runtime(self):
        result = self.outcome("steamcmd: error while loading shared libraries: libstdc++.so.6", 127)
        self.assertEqual(result.outcome, steamcmd.Outcome.ENVIRONMENT)

    def test_no_success_line(self):
        self.assertEqual(self.outcome("Uploading content...\n", 0).outcome, steamcmd.Outcome.UNCERTAIN)
        self.assertEqual(self.outcome("Uploading content...\n", 8).outcome, steamcmd.Outcome.FAILED)


class RunscriptTests(unittest.TestCase):
    def test_password_only_in_script_never_in_argv(self):
        creds = Credentials("uploader", "p@ss word", "AB12C", "env")
        script = steamcmd.build_runscript(creds, vdf_path="/run/user/1000/x/workshop-upload.vdf")
        self.assertIn('login "uploader" "p@ss word"', script)
        self.assertIn('set_steam_guard_code "AB12C"', script)
        self.assertIn("@NoPromptForPassword 1", script)
        self.assertTrue(script.strip().endswith("quit"))
        argv = steamcmd.command_for("/usr/games/steamcmd", "/run/user/1000/x/steamcmd-script.txt")
        self.assertNotIn("p@ss word", " ".join(argv))
        self.assertNotIn("uploader", " ".join(argv))

    def test_cached_session_script_has_no_password(self):
        script = steamcmd.build_runscript(Credentials("uploader"), vdf_path="/x.vdf")
        self.assertIn('login "uploader"\n', script)

    def test_tools_install_script_order(self):
        script = steamcmd.build_runscript(
            Credentials("uploader"), extra_commands=["app_update 730 validate"],
            pre_login=["@sSteamCmdForcePlatformType windows", 'force_install_dir "/srv/cs2"'],
        )
        lines = script.splitlines()
        self.assertLess(lines.index('force_install_dir "/srv/cs2"'), lines.index('login "uploader"'))
        self.assertLess(lines.index('login "uploader"'), lines.index("app_update 730 validate"))

    def test_private_temp_dir_is_removed(self):
        with steamcmd.PrivateTempDir() as tmp:
            path = tmp.write("secret.txt", "x")
            self.assertTrue(path.exists())
        self.assertFalse(tmp.path.exists())


class OutputRelayTests(unittest.TestCase):
    def test_normal_mode_filters_and_redacts(self):
        stream = io.StringIO()
        redactor = Redactor()
        redactor.add("topsecret")
        relay = steamcmd.OutputRelay(Console(stream=stream, color=False, redactor=redactor), redactor, show_all=False)
        relay.feed("noise line\nLogging in user 'u' pass topsecret...OK\nUploading con")
        relay.feed("tent...\n")
        relay.flush()
        shown = stream.getvalue()
        self.assertNotIn("noise", shown)
        self.assertIn("Uploading content...", shown)
        self.assertNotIn("topsecret", shown)
        self.assertNotIn("topsecret", relay.output)

    def test_prompt_detected_without_newline(self):
        stream = io.StringIO()
        relay = steamcmd.OutputRelay(Console(stream=stream, color=False), Redactor(), show_all=False)
        relay.feed("Steam Guard code:")
        self.assertEqual(relay.prompt, "Steam Guard code:")
        self.assertIn("Steam Guard code:", stream.getvalue())


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SteamApiTests(unittest.TestCase):
    def opener(self, details):
        payload = json.dumps({"response": {"result": 1, "publishedfiledetails": [details]}}).encode()
        return lambda request, timeout: FakeResponse(payload)

    def test_verify_success(self):
        details = {"result": 1, "publishedfileid": "2000000001", "consumer_app_id": 730, "file_size": "100000",
                   "time_updated": 2000, "title": "x", "visibility": 0, "banned": 0}
        ok, info, problems = steamapi.verify_update("2000000001", 730, 1990, 100000, opener=self.opener(details))
        self.assertTrue(ok, problems)
        self.assertEqual(info["fileSize"], 100000)

    def test_verify_detects_stale_and_wrong_app(self):
        details = {"result": 1, "publishedfileid": "1", "consumer_app_id": 440, "file_size": "5", "time_updated": 10}
        ok, _, problems = steamapi.verify_update("1", 730, 1000, 100000, opener=self.opener(details))
        self.assertFalse(ok)
        self.assertEqual(len(problems), 3)

    def test_private_item(self):
        with self.assertRaises(steamapi.SteamApiError):
            steamapi.get_published_file_details("1", opener=self.opener({"result": 9}))


if __name__ == "__main__":
    unittest.main()
