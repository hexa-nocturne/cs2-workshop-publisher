import unittest

from tests.helpers import ROOT  # noqa: F401  (sets sys.path)
from workshop_publisher import vdf


class VdfTests(unittest.TestCase):
    def test_escaping_round_trip(self):
        fields = vdf.build_workshop_item(
            730, "/srv/work shop/content", "linux", published_file_id="2000000001",
            title='Quote " and backslash \\', description="line1\r\nline2\t\"x\"", change_note="C:\\temp",
        )
        text = vdf.dumps("workshopitem", fields)
        self.assertIn('\\"', text)
        parsed = vdf.loads(text)["workshopitem"]
        self.assertEqual(parsed["title"], 'Quote " and backslash \\')
        self.assertEqual(parsed["description"], "line1\nline2\t\"x\"")
        self.assertEqual(parsed["changenote"], "C:\\temp")
        self.assertEqual(parsed["publishedfileid"], "2000000001")
        self.assertEqual(parsed["contentfolder"], "/srv/work shop/content")

    def test_linux_paths(self):
        fields = vdf.build_workshop_item(730, "/home/user/a b/../build/workshop", "linux", preview_file="/x/p.png")
        self.assertEqual(fields["contentfolder"], "/home/user/build/workshop")
        self.assertEqual(fields["previewfile"], "/x/p.png")
        self.assertNotIn("\\", vdf.dumps("workshopitem", fields))

    def test_windows_paths(self):
        fields = vdf.build_workshop_item(730, "C:/Users/me/My Addon/build/workshop", "windows")
        self.assertEqual(fields["contentfolder"], "C:\\Users\\me\\My Addon\\build\\workshop")
        text = vdf.dumps("workshopitem", fields)
        self.assertIn("C:\\\\Users\\\\me", text)
        self.assertEqual(vdf.loads(text)["workshopitem"]["contentfolder"], fields["contentfolder"])

    def test_new_item_uses_zero_id(self):
        self.assertEqual(vdf.build_workshop_item(730, "/c", "linux")["publishedfileid"], "0")

    def test_rejects_relative_and_invalid(self):
        with self.assertRaises(vdf.VdfError):
            vdf.build_workshop_item(730, "relative/path", "linux")
        with self.assertRaises(vdf.VdfError):
            vdf.build_workshop_item(730, "/c", "linux", visibility=7)
        with self.assertRaises(vdf.VdfError):
            vdf.build_workshop_item(730, "/c", "linux", title="x" * 129)

    def test_parser_errors(self):
        for bad in ('"a" { "b" "c"', '"a" "unterminated', "}"):
            with self.assertRaises(vdf.VdfError):
                vdf.loads(bad)


if __name__ == "__main__":
    unittest.main()
