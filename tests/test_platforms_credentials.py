import os
import tempfile
import unittest
from pathlib import Path

from tests.helpers import ROOT  # noqa: F401
from workshop_publisher import platforms
from workshop_publisher.console import Redactor
from workshop_publisher.credentials import CredentialError, check_file_permissions, resolve_credentials


def no_which(_name):
    return None


class SteamCmdDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="home with space "))

    def test_explicit_path_file_and_directory(self):
        target = self.home / "steam cmd" / "steamcmd.sh"
        target.parent.mkdir()
        target.write_text("#!/bin/sh\n")
        for value in (str(target), str(target.parent)):
            loc = platforms.find_steamcmd({"STEAMCMD_PATH": value}, "linux", self.home, which=no_which)
            self.assertEqual(loc.path, target)
            self.assertEqual(loc.source, "STEAMCMD_PATH")

    def test_explicit_missing_path_is_an_error_not_a_fallback(self):
        (self.home / "steamcmd").mkdir()
        (self.home / "steamcmd" / "steamcmd.sh").write_text("")
        loc = platforms.find_steamcmd(
            {"STEAMCMD_PATH": "/nonexistent/steamcmd.sh"}, "linux", self.home, which=lambda _: "/usr/games/steamcmd"
        )
        self.assertFalse(loc.found)
        self.assertIn("missing", loc.error)

    def test_linux_common_locations(self):
        (self.home / "Steam").mkdir()
        (self.home / "Steam" / "steamcmd.sh").write_text("")
        loc = platforms.find_steamcmd({}, "linux", self.home, which=no_which)
        self.assertEqual(loc.path, self.home / "Steam" / "steamcmd.sh")

    def test_path_lookup_wins(self):
        loc = platforms.find_steamcmd(
            {}, "linux", self.home, which=lambda name: "/usr/games/steamcmd" if name == "steamcmd" else None
        )
        self.assertEqual(str(loc.path).replace("\\", "/"), "/usr/games/steamcmd")

    def test_not_found(self):
        loc = platforms.find_steamcmd({}, "linux", self.home, which=no_which)
        self.assertFalse(loc.found)
        self.assertIsNone(loc.error)
        self.assertIn("/usr/games/steamcmd", " ".join(loc.tried).replace("\\", "/"))

    def test_windows_candidates(self):
        names = [str(c).replace("\\", "/") for c in platforms.steamcmd_candidates("windows", {}, self.home)]
        self.assertIn("C:/steamcmd/steamcmd.exe", names)

    def test_install_help(self):
        text = platforms.install_help("linux")
        self.assertIn("sudo apt install steamcmd", text)
        self.assertIn("STEAMCMD_PATH", text)
        self.assertNotIn("| bash", text)


class CredentialTests(unittest.TestCase):
    def test_environment_credentials_are_redacted(self):
        redactor = Redactor()
        creds = resolve_credentials({"STEAM_USERNAME": "uploader", "STEAM_PASSWORD": "hunter2!"}, redactor=redactor)
        self.assertEqual(creds.mode, "password")
        self.assertEqual(redactor.redact("pw=hunter2!"), "pw=********")
        self.assertNotIn("hunter2", creds.describe())

    def test_cached_session_mode(self):
        self.assertEqual(resolve_credentials({"STEAM_USERNAME": "uploader"}).mode, "cached-session")

    def test_anonymous_rejected(self):
        with self.assertRaises(CredentialError):
            resolve_credentials({"STEAM_USERNAME": "anonymous"})

    def test_quote_in_password_rejected(self):
        with self.assertRaises(CredentialError):
            resolve_credentials({"STEAM_USERNAME": "u", "STEAM_PASSWORD": 'a"b'})

    def _write(self, text, mode=0o600):
        path = Path(tempfile.mkdtemp()) / "steam uploader.env"
        path.write_text(text, encoding="utf-8")
        if os.name != "nt":
            os.chmod(str(path), mode)
        return path

    def test_credentials_file(self):
        path = self._write("STEAM_USERNAME=uploader_account\nSTEAM_PASSWORD='s3cret pass'\n")
        creds = resolve_credentials({}, credentials_file=str(path))
        self.assertEqual(creds.username, "uploader_account")
        self.assertEqual(creds.password, "s3cret pass")
        self.assertEqual(resolve_credentials({"STEAM_USERNAME": "override"}, credentials_file=str(path)).username, "override")

    def test_credentials_file_from_environment_variable(self):
        path = self._write("STEAM_USERNAME=uploader_account\n")
        creds = resolve_credentials({"WORKSHOP_CREDENTIALS_FILE": str(path)})
        self.assertEqual(creds.mode, "cached-session")

    def test_credentials_file_unknown_key(self):
        path = self._write("STEAM_USERNAME=a\nAPI_TOKEN=x\n")
        with self.assertRaises(CredentialError):
            resolve_credentials({}, credentials_file=str(path))

    def test_missing_credentials_file(self):
        with self.assertRaises(CredentialError):
            resolve_credentials({}, credentials_file="/nonexistent/steam.env")

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_permission_checks(self):
        path = self._write("STEAM_USERNAME=a\n", 0o644)
        with self.assertRaises(CredentialError):
            check_file_permissions(path)
        os.chmod(str(path), 0o640)
        with self.assertRaises(CredentialError):
            check_file_permissions(path)  # owned by the current user but group-readable
        os.chmod(str(path), 0o620)
        with self.assertRaises(CredentialError):
            check_file_permissions(path, uid=12345)  # neither root nor current user
        os.chmod(str(path), 0o600)
        check_file_permissions(path)


if __name__ == "__main__":
    unittest.main()
