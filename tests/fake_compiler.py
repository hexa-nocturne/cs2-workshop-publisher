"""Stand-in for resourcecompiler.exe used by tests.

Usage: fake_compiler.py <input> <addon_content_dir> <addon_game_dir>
Writes the compiled file(s) the real compiler would produce and appends the
input to $FAKE_COMPILER_LOG. $FAKE_COMPILER_FAIL names a relative path to fail.
"""

import os
import sys

COMPILED = {".xml": ".vxml_c", ".css": ".vcss_c", ".js": ".vjs_c", ".wav": ".vsnd_c", ".mp3": ".vsnd_c"}


def main():
    source, content, game = sys.argv[1:4]
    rel = os.path.relpath(source, content).replace("\\", "/")
    if os.environ.get("FAKE_COMPILER_LOG"):
        with open(os.environ["FAKE_COMPILER_LOG"], "a", encoding="utf-8") as log:
            log.write(rel + "\n")
    if os.environ.get("FAKE_COMPILER_FAIL") == rel:
        print("ERROR: failed to compile %s" % rel)
        return 1
    stem, ext = os.path.splitext(rel)
    out = stem + COMPILED.get(ext, ext + "_c")
    target = os.path.join(game, out)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(source, "rb") as src, open(target, "wb") as dst:
        dst.write(b"compiled:" + src.read())
    print("1 file compiled, 0 errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
