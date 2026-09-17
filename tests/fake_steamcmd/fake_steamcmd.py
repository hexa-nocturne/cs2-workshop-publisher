"""Test double for SteamCMD. Never contacts Steam.

Behaviour is selected with FAKE_STEAMCMD_SCENARIO:
  success, bad_password, guard, access_denied, limit_exceeded, silent
Every invocation is recorded to FAKE_STEAMCMD_RECORD (JSON) when set.
"""

import json
import os
import re
import sys
import time


def out(text, end="\n"):
    sys.stdout.write(text + end)
    sys.stdout.flush()


def main():
    argv = sys.argv[1:]
    scenario = os.environ.get("FAKE_STEAMCMD_SCENARIO", "success")
    script_path = argv[argv.index("+runscript") + 1] if "+runscript" in argv else None
    script = open(script_path, encoding="utf-8").read() if script_path else ""
    record = {"argv": argv, "script": script, "script_path": script_path}
    if os.name != "nt" and script_path:
        record["script_mode"] = oct(os.stat(script_path).st_mode & 0o777)
        record["dir_mode"] = oct(os.stat(os.path.dirname(script_path)).st_mode & 0o777)
    vdf_match = re.search(r'^workshop_build_item "(.+)"$', script, re.M)
    if vdf_match:
        record["vdf"] = open(vdf_match.group(1), encoding="utf-8").read()
    if os.environ.get("FAKE_STEAMCMD_RECORD"):
        with open(os.environ["FAKE_STEAMCMD_RECORD"], "w", encoding="utf-8") as handle:
            json.dump(record, handle)

    out("Redirecting stderr to 'logs/stderr.txt'")
    out("[  0%] Checking for available updates...")
    out("Loading Steam API...OK")
    user = re.search(r'^login "([^"]+)"', script, re.M)
    out("Logging in user '%s' to Steam Public..." % (user.group(1) if user else "?"), end="")

    if scenario == "bad_password":
        out("FAILED (Invalid Password)")
        return 5
    if scenario == "guard":
        out("\nThis computer has not been authenticated for your account using Steam Guard.")
        out("Please check your email for the message from Steam, and enter the Steam Guard")
        out(" code from that message.")
        out("Steam Guard code:", end="")
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.2)
        return 5
    out("OK")
    out("Waiting for client config...OK")
    out("Waiting for user info...OK")

    install = re.search(r'^force_install_dir "(.+)"$', script, re.M)
    if install and "app_update 730" in script:
        if os.environ.get("FAKE_CREATE_COMPILER") == "1":
            target = os.path.join(install.group(1), "game", "bin", "win64")
            os.makedirs(target, exist_ok=True)
            open(os.path.join(target, "resourcecompiler.exe"), "wb").close()
        out("Success! App '730' fully installed.")
        return 0

    if vdf_match:
        vdf_path = vdf_match.group(1)
        text = open(vdf_path, encoding="utf-8").read()
        out("Preparing update...")
        out("Preparing content...")
        if scenario == "access_denied":
            out("ERROR! Failed to update workshop item (Access Denied).")
            return 0
        if scenario == "limit_exceeded":
            out("Uploading preview image...")
            out("ERROR! Failed to update workshop item (Limit Exceeded).")
            return 0
        if re.search(r'"publishedfileid"\s+"0"', text):
            out("Creating item...")
            text = re.sub(r'("publishedfileid"\s+)"0"', r'\1"3999999999"', text)
            with open(vdf_path, "w", encoding="utf-8") as handle:
                handle.write(text)
        out("Uploading content...")
        out("Uploading preview image...")
        if scenario == "silent":
            return 0
        out("Committing update...Success.")
    out("Unloading Steam API...OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
