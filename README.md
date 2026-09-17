# Workshop Publisher

Build, validate, pack and publish Steam Workshop content from a **Linux server**, unattended.
It was built for Counter-Strike 2 community-server addons (Panorama UI, sounds, models,
materials, textures), and the publishing half works for any Workshop app.

```text
source files (plain folder, your own Git repo)
    ↓  validate       config, preview image, asset references
    ↓  compile        CS2 resource compiler (incremental, low priority)
    ↓  package        addon VPK + file list, sizes and diff against the last build
    ↓  generate VDF   absolute paths for the current OS, escaped values
    ↓  SteamCMD       login from a cached session or credentials file, never on the command line
    ↓  upload         update the existing Workshop item
    ↓  verify         Steam Web API confirms the new timestamp and size
```

One Python CLI (standard library only, Python 3.8+) does all of this. `./workshop` is the
Linux entry point. `workshop.ps1` and `workshop.cmd` call the same code on Windows.

## Contents

- [Requirements](#requirements)
- [Quick start on Ubuntu](#quick-start-on-ubuntu)
- [Publishing to Steam Workshop](#publishing-to-steam-workshop)
- [Unattended server setup](#unattended-server-setup)
- [Compiling CS2 assets on Linux](#compiling-cs2-assets-on-linux)
- [Configuration reference](#configuration-reference)
- [Commands, exit codes and JSON output](#commands-exit-codes-and-json-output)
- [Windows](#windows)
- [Continuous integration](#continuous-integration)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)

## Requirements

| Needed for | Ubuntu 22.04 / 24.04 (x86_64) |
|---|---|
| everything | `python3` (3.8+, preinstalled) |
| uploading | `steamcmd` (multiverse, needs the i386 architecture) and `lib32gcc-s1` |
| compiling CS2 assets | `wine`, `wine64`, `xvfb`, about 60 GB of disk for the Windows CS2 build |

No root access is needed after the packages are installed. `scripts/setup-ubuntu.sh` checks
all of this and prints the exact `apt` commands to run. It never runs `sudo` itself.

## Quick start on Ubuntu

```bash
sudo add-apt-repository multiverse
sudo dpkg --add-architecture i386
sudo apt update
sudo apt install steamcmd lib32gcc-s1 wine wine64 xvfb

git clone <this repository> workshop-publisher
cd workshop-publisher
cp workshop.example.json workshop.json      # or keep workshop.json in your content repo

export STEAM_USERNAME="your_username"
export CS2_TOOLS_DIR="$HOME/cs2-windows"

./workshop doctor
./workshop validate
./workshop update --change-note "Updated HUD assets"
```

The tool runs from any working directory. It uses `--config`, then `$WORKSHOP_CONFIG`, then
`./workshop.json` in the current directory, then `workshop.json` next to the launcher.

## Publishing to Steam Workshop

### App ID and Workshop ID

- `appId` is the game's Steam App ID: **730** for Counter-Strike 2.
- `publishedFileId` is the Workshop item's ID, the number in
  `https://steamcommunity.com/sharedfiles/filedetails/?id=<ID>`. Store it as a JSON string.

### First publish vs update

| Situation | Command | What happens |
|---|---|---|
| Item already exists | `./workshop update --change-note "..."` | Updates that item. Fails if `publishedFileId` is empty. |
| Brand new item | `./workshop publish --yes --change-note "Initial release"` | Creates a new item. Refuses to run if `publishedFileId` is already set, so an existing item is never duplicated. |

After a successful first publish, SteamCMD writes the new ID back into the generated VDF. The
tool saves it into `workshop.json` if the field is still empty; it never overwrites a different
ID. New items are created **private** unless `visibility` says otherwise.

By default `update` sends only the content, the preview image (if configured) and the change
note. Title, description and visibility you edited on the Workshop page are left alone. To push
them from `workshop.json` too, add `--update-metadata`. To change visibility alone, add
`--visibility public|friends-only|private|unlisted`.

### Dry run

```bash
./workshop update --dry-run --change-note "test"
```

A dry run builds and checks everything: it compiles, packs, validates references, and
generates and re-parses the VDF. It does **not** log in or upload.

### Steam Guard and authentication

Anonymous SteamCMD logins cannot upload Workshop items, so a real Steam account is needed.
Credentials are read from, in order of priority:

1. `STEAM_USERNAME`, `STEAM_PASSWORD` (optional) and `STEAM_GUARD_CODE` (optional) environment variables
2. the file named by `WORKSHOP_CREDENTIALS_FILE` or `--credentials-file` (KEY=VALUE lines)
3. a `.env` file next to `workshop.json` (local development only)

Commands are **non-interactive by default**. If SteamCMD asks for a password or Steam Guard
code during an unattended run, the tool stops SteamCMD at once and exits with code 4 and a
message explaining how to fix it. It never hangs waiting for input.

To avoid Steam Guard prompts altogether, log in once interactively. SteamCMD then caches a
session for that Linux user, and later runs need only the username:

```bash
STEAM_USERNAME=my_uploader ./workshop login     # enter password and Steam Guard code once
STEAM_USERNAME=my_uploader ./workshop update --change-note "..."   # no prompts from now on
```

If the cached session expires, the next unattended run fails with exit code 4 and tells you to
run `./workshop login` again. Add `--interactive` to `update` or `publish` if you want to type
a code during that run.

### Using your own SteamCMD

SteamCMD is found automatically on `PATH` (for example `/usr/games/steamcmd`) or in
`~/steamcmd/steamcmd.sh`, `~/Steam/steamcmd.sh`, `~/.steam/steamcmd/steamcmd.sh`,
`/usr/bin/steamcmd`, `/usr/local/bin/steamcmd` or `/opt/steamcmd/steamcmd.sh`. To use a
specific copy:

```bash
STEAMCMD_PATH=/path/to/steamcmd.sh ./workshop update
```

When `STEAMCMD_PATH` is set but wrong, the tool reports that instead of silently using
another copy. Set `WORKSHOP_STEAMCMD_NO_AUTODETECT=1` to use only `STEAMCMD_PATH` and never
search elsewhere (the test suite sets this, so an installed SteamCMD doesn't affect it). Without `sudo` you can also install SteamCMD from Valve's own download:

```bash
mkdir -p ~/steamcmd && cd ~/steamcmd
curl -fsSLO https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz
tar -xzf steamcmd_linux.tar.gz
```

## Unattended server setup

This setup lets one Linux server build and publish with no human in the loop, whether started
from a shell, a cron job, or an agent told to "build and publish the latest update".

1. **Create a dedicated uploader Steam account.** Add the game to it (CS2 is free) and the
   *Counter-Strike 2 Workshop Tools* DLC. The account must be able to update the item: it
   must be the item's owner or a contributor Steam allows to update it. Limited accounts
   (no purchases on the account) may be refused by Steam. If the first real update fails
   with `Access Denied`, check these points first.

2. **Store the username in a root-owned file** that only the build user's group can read:

   ```bash
   sudo install -d -m 0750 -o root -g <build-user> /etc/<project>
   sudo install -m 0640 -o root -g <build-user> /dev/null /etc/<project>/steam-uploader.env
   sudoedit /etc/<project>/steam-uploader.env
   ```

   ```ini
   STEAM_USERNAME=my_uploader
   # STEAM_PASSWORD is optional and not recommended; the cached session is enough.
   ```

   The tool refuses to use a credentials file that other users can read, that is
   group-writable, or that isn't owned by root or the current user. Allowed keys are
   `STEAM_USERNAME`, `STEAM_PASSWORD` and `STEAM_GUARD_CODE`.

3. **Set the environment for the build user**, for example in `~/.profile`:

   ```bash
   export WORKSHOP_CREDENTIALS_FILE=/etc/<project>/steam-uploader.env
   export WORKSHOP_CONFIG=$HOME/<content-repo>/workshop.json
   export CS2_TOOLS_DIR=$HOME/cs2-windows
   ```

4. **One-time preparation as the build user** (no root needed):

   ```bash
   scripts/setup-ubuntu.sh        # checks packages and prints any apt commands you still need
   ./workshop login               # caches the Steam session (interactive, once)
   ./workshop tools install       # downloads the Windows CS2 build into $CS2_TOOLS_DIR
   ./workshop tools setup-wine    # creates the Wine prefix
   ./workshop doctor              # must print "Ready to update."
   ```

5. **Every release** is then a single command:

   ```bash
   ./workshop --json update --change-note-file CHANGELOG-latest.txt
   ```

## Compiling CS2 assets on Linux

Valve's Source 2 resource compiler (`resourcecompiler.exe`) only exists for Windows, and no
open-source tool can compile Panorama, sounds, models, materials and textures. Wine exists to
run Windows programs, so the pipeline runs the compiler under Wine, fully scripted and headless:

- `workshop tools install` has SteamCMD download the **Windows** build of CS2
  (`@sSteamCmdForcePlatformType windows`) into `$CS2_TOOLS_DIR`. It uses the same uploader
  login and needs no Windows machine. Run it again after CS2 updates.
- `app_update` does not install the **optional Workshop Tools depot**, which contains
  `resourcecompiler.exe`. Put its depot ID in the `cs2` step's `toolsDepots`, or pass
  `--depot ID`. The tool fetches it with SteamCMD's `download_depot` and merges it into
  `$CS2_TOOLS_DIR`. Look up the depot ID in the app 730 depot list (for example on SteamDB).
  The uploader account must own the Workshop Tools DLC.

  ```bash
  ./workshop tools install --max-kbps 4000             # cap SteamCMD at ~0.5 MB/s
  ./workshop tools install --depots-only --depot <ID>  # only (re)fetch the tools depot
  ./workshop tools install --validate                  # re-check every installed file (slow)
  ```

  `--max-kbps` uses SteamCMD's `set_download_throttle` for that session only.
- The build copies your source folder into `$CS2_TOOLS_DIR/content/csgo_addons/<addon>/`. It
  runs `wine resourcecompiler.exe -nop4 -i <file>` under `xvfb-run` for each resource that
  needs compiling, and collects the `*_c` outputs from `game/csgo_addons/<addon>/`.
- Everything else is native Python: incremental state, reference checks, VPK packing
  (VPK v2 with checksums), manifests and diffs.

**Source layout.** `source` is a plain folder that mirrors the addon's content root, for example:

```text
content/
  panorama/layout/*.xml   panorama/styles/*.css   panorama/scripts/*.js   panorama/images/**
  sounds/**/*.wav|mp3     soundevents/*.vsndevts
  models/**/*.vmdl (+ .fbx/.dmx sources)   materials/**/*.vmat (+ textures)
```

Commit the source folder and `workshop.json` to your content repository. Compiled output
(`build/`, `build-cache/`, `*.vpk`) is ignored by Git.

**Incremental builds.** The tool keeps SHA-256 hashes of every source file in
`build-cache/cs2/<addon>-state.json`. On each build:

- a changed `.xml`, `.css`, `.js`, `.vsndevts`, `.wav`, `.mp3`, `.vmdl`, `.vmat`, `.vtex` or `.vpcf` file is recompiled;
- a changed dependency (an image, `.fbx`, `.tga` and so on) recompiles every resource that references it;
- a deleted source file, a changed compiler, or changed compile settings triggers a clean
  rebuild, so stale compiled files can never end up in the pack;
- if any file fails to compile, no state is saved, so the next run retries it.

Use `--clean` to force a full rebuild.

**Reference checks.** Before compiling, the tool scans layouts, styles, scripts and KV3 files
for `file://{images}/…`, `file://{resources}/…`, `s2r://…` and quoted asset paths. Each
reference must resolve to a file in your sources or to a compiled file in the base game's
`pak01_dir.vpk`. Unresolved references fail the build (`referenceCheck.mode: "error"`), or
only warn with `"warn"`. Paths built at runtime in JavaScript, such as
`"rank_" + n + ".png"`, cannot be checked statically and are skipped. Use `extraRoots` to
compile images that are only referenced that way, for example
`["panorama/images/ranks/*.png"]`.

**Pack report.** Every build writes `build-cache/manifests/latest.json` plus a timestamped
copy. It records each file's size and CRC and the pack size, and prints what was added,
removed or changed since the previous build. To inspect a pack or compare it with the version
players currently download:

```bash
./workshop pack-list
./workshop pack-list --compare /path/to/steamapps/workshop/content/730/<id>/<name>_dir.vpk
./workshop pack-list /path/to/pack_dir.vpk --verify     # also check every CRC/MD5 (reads all data)
```

Listing and comparing read only the directory tree, so they are fast even for large packs.

**Prebuilt files.** Packs often contain files you cannot compile on the server, such as
third-party agents and models that have no sources. List folders of already-compiled files
in `prebuilt`, and they are merged into the pack as-is, keeping their paths. When a compiled
file has the same path as a prebuilt one, the compiled file wins. Every such replacement is
listed in the build result (`overriddenPrebuilt`). Prebuilt files also satisfy the reference
check.

**Large and multi-part packs.** If `vpk` ends in `_dir.vpk` (e.g. `{content}/pak01_dir.vpk`),
the pack is written the way Valve's tool does it: a directory file plus numbered data chunks
(`pak01_000.vpk`, `pak01_001.vpk`, …) of about `vpkChunkMB` (default 100) each. Chunks left
over from an earlier, larger build are deleted. Packing streams data in 1 MB blocks, so
memory use stays small regardless of pack size.

**Migrating an existing live pack.** To take over a pack that was built elsewhere without
losing anything players currently download:

```bash
LIVE=/path/to/steamapps/workshop/content/730/<id>
./workshop pack-list "$LIVE/<name>_dir.vpk"                       # layout, size, chunk count
./workshop pack-extract "$LIVE/<name>_dir.vpk" ~/<content-repo-data>/prebuilt
# set "prebuilt": ["~/<content-repo-data>/prebuilt"] and "vpk": "{content}/<name>_dir.vpk"
./workshop build                                                 # no sources needed yet
./workshop pack-list --compare "$LIVE/<name>_dir.vpk"            # expect: 0 added, 0 removed, 0 changed
```

A prebuilt-only build needs no Wine or compiler, so it proves the packing and layout on
Linux before anything is recompiled. After that, add sources for the parts you maintain
(HUD, rank icons, sounds). Their compiled output replaces the extracted copies, and the
compare shows exactly which files changed.

**Resource limits.** Compiler processes run under `nice -n 10` and `ionice -c 3` by default.
If a systemd user manager is available, `resources.memoryMax` and `resources.cpuQuota` also
place each compiler run in a transient scope with those limits, for example `"6G"` and
`"200%"`. Only one build or upload runs per project at a time; a second one exits with code 8.

**Compile speed.** Starting Wine has a fixed cost per process. `jobs` runs several compiler
processes in parallel. Each process gets its own `memoryMax`/`cpuQuota` scope, so the total
can reach `jobs` × `memoryMax`. `compileMode: "batch"` passes up to `batchSize` files per
compiler call through a file list, using the `compileBatch` template (it must contain
`{filelist}`). Batch mode is **untested with the real compiler**: check the list flag that
`resourcecompiler.exe` accepts on a small test addon before relying on it.

## Configuration reference

`workshop.json`: relative paths are resolved from the file's own directory. Forward slashes
are recommended; backslashes are accepted.

| Key | Required | Description |
|---|---|---|
| `appId` | yes | Steam App ID (730 for CS2) |
| `publishedFileId` | for `update` | Existing Workshop item ID, as a string. Empty means not published yet. |
| `contentDirectory` | yes | Folder uploaded to the Workshop, usually the folder the VPK is written into |
| `previewImage` | for `publish` | `.png`, `.jpg` or `.gif`, **under 1 MB** |
| `title` | for `publish` | Up to 128 characters |
| `description` / `descriptionFile` | no | Up to 8000 characters (inline or from a file) |
| `visibility` | no | `public`, `friends-only`, `private`, `unlisted` (new items default to `private`) |
| `build.steps` | no | Ordered build steps (below) |

Build step types:

```jsonc
{ "name": "…", "cs2":   { … } }                                       // compile + pack a CS2 addon
{ "name": "…", "copy":  { "from": "./content", "to": "{content}", "exclude": ["*.psd"] } }
{ "name": "…", "clean": "{content}" }                                 // only inside the project
{ "name": "…", "run":   ["program", "arg"], "platforms": ["linux"] }   // no shell is involved
```

Placeholders: `{root}` is the directory of `workshop.json` and `{content}` is
`contentDirectory`. `${ENV_VAR}` inserts an environment variable; an unset variable is an
error, not an empty string.

`cs2` step options:

| Key | Default | Description |
|---|---|---|
| `addon` | required | Addon name, `[a-z0-9_]` |
| `vpk` | required | Output VPK path, e.g. `{content}/{publishedFileId}.vpk` |
| `source` | `./content` | Uncompiled addon sources |
| `tools` | `${CS2_TOOLS_DIR}` | Windows CS2 install containing `game/bin/win64/resourcecompiler.exe` |
| `runtime` | `auto` | `wine` on Linux, `native` on Windows |
| `wine.binary` / `wine.prefix` / `wine.xvfb` | `wine` / `~/.local/share/workshop-publisher/wineprefix` / `auto` | Wine settings |
| `compile` | `["{compiler}", "-nop4", "-i", "{input}"]` | Compiler command template (`{compiler}`, `{input}`, `{addon_content}`, `{addon_game}`, `{tools}`) |
| `rootExtensions` / `extraRoots` | see above / `[]` | Which sources are compiled directly |
| `packExclude` | `tools_*`, `*.vpk`, … | Compiled files left out of the VPK |
| `referenceCheck` | `{"mode": "error"}` | `mode`, `ignore` globs, `panoramaAliases`, `baseVpks` |
| `resources` | `nice 10`, `ioniceIdle true` | plus optional `memoryMax`, `cpuQuota` |
| `timeoutSeconds` | `1800` | Per-call compile timeout |
| `toolsDepots` | `[]` | Extra depot IDs of app 730 for `tools install` (the Workshop Tools depot) |
| `prebuilt` | `[]` | Folders of already-compiled files packed as-is (compiled files win on conflicts) |
| `vpkChunkMB` | `100` | Chunk size when `vpk` ends in `_dir.vpk` |
| `jobs` | `1` | Parallel compiler processes |
| `compileMode` / `compileBatch` / `batchSize` | `per-file` / – / `200` | Optional batch compilation through a file list |

## Commands, exit codes and JSON output

```text
./workshop doctor                    check OS, SteamCMD, config, content, compiler, credentials
./workshop validate                  validate without building, logging in or uploading
./workshop build [--clean]           compile and pack
./workshop update  [--change-note T | --change-note-file F] [--visibility V] [--update-metadata]
                   [--dry-run] [--no-build] [--clean] [--no-verify] [--interactive]
./workshop publish --yes [same options] [--no-save-id]
./workshop login                     interactive, once: cache the SteamCMD session
./workshop status                    item details from the Steam Web API
./workshop tools install [--max-kbps N] [--depot ID] [--depots-only] [--validate]
./workshop tools setup-wine|check
./workshop pack-list [VPK] [--compare OTHER.vpk] [--verify]
./workshop pack-extract VPK DESTINATION [--overwrite]
```

Global options: `--config PATH`, `--credentials-file PATH`, `--json`, `--verbose`, `--no-color`.

With `--json`, stdout carries exactly one JSON object with `ok`, `exitCode`, `error` and
command-specific data (checks, build results, VDF fields, pack diff, verification). All
human-readable logs go to stderr.

| Exit code | Meaning |
|---|---|
| 0 | success |
| 1 | validation or general failure |
| 2 | usage error (e.g. `update` without an ID, `publish` with one) |
| 3 | environment: SteamCMD, Wine or compiler missing |
| 4 | authentication: rejected login, Steam Guard needed, no license |
| 5 | Workshop upload rejected by Steam |
| 6 | build/compile failed |
| 7 | upload finished but Steam Web API verification failed |
| 8 | another build/upload is already running |

`--verbose` shows the commands being run, resolved paths, the generated VDF and the full
SteamCMD and compiler output. Passwords, Steam Guard codes and any environment variable whose
name contains `PASSWORD`, `SECRET`, `TOKEN` or `API_KEY` are replaced by `********` everywhere,
in normal output, verbose output and JSON alike.

## Windows

The same CLI runs on Windows with Python 3.8+:

```powershell
.\workshop.ps1 doctor
.\workshop.ps1 update --change-note "Updated HUD assets"
```

In `cmd.exe`, use `workshop update …`. SteamCMD is found at `C:\steamcmd\steamcmd.exe`,
`%USERPROFILE%\steamcmd\steamcmd.exe`, on `PATH`, or through `$env:STEAMCMD_PATH`. The `cs2`
step runs `resourcecompiler.exe` natively on Windows. Linux is the primary platform; Windows
support exists so contributors on Windows can use the same commands.

## Continuous integration

`.github/workflows/ci.yml` runs on Ubuntu 22.04, Ubuntu 24.04 and Windows. It runs the test
suite, ShellCheck, `./workshop validate`, `./workshop update --dry-run`, and checks that
`doctor` explains a missing SteamCMD. **It never logs in to Steam or uploads anything.**
Publishing from CI would require storing Steam credentials as repository secrets and
handling Steam Guard, so it is intentionally not provided. Publish from the build server.

## Security

- Credentials are never written to the repository, never passed on the SteamCMD command line
  (other users could read that from the process list), and never printed.
- SteamCMD receives the login through a `+runscript` file in a private temporary directory
  (mode 0700, file 0600, under `$XDG_RUNTIME_DIR` when available). The directory is deleted as
  soon as SteamCMD exits, including after errors and Ctrl+C.
- Credential files that other users can read, or that are group-writable, are rejected.
- The content directory is scanned before upload, and uploads containing `.env`, `.git`,
  `config.vdf`, `ssfn*` or `workshop.json` are refused.
- Downloads come only from Valve (SteamCMD, CS2 depots). HTTPS certificate checks are never
  disabled. Nothing runs `sudo` or pipes a download into a shell.
- If you commit from Windows, keep the launchers executable:
  `git update-index --chmod=+x workshop scripts/setup-ubuntu.sh tests/fake_steamcmd/steamcmd.sh`.

## Troubleshooting

| Message | Fix |
|---|---|
| `SteamCMD was not found` | Install it (see above) or set `STEAMCMD_PATH`. |
| `SteamCMD's 32-bit runtime is missing` | `sudo apt install lib32gcc-s1` |
| `SteamCMD asked for a Steam Guard code but this run is non-interactive` | Run `./workshop login` once from a terminal as the same Linux user. |
| `Steam authentication was rejected` | Wrong username or password; check the credentials file. |
| `too many recent attempts` | Steam rate-limited logins. Wait 30+ minutes; don't retry in a loop. |
| `Workshop upload failed (Access Denied)` | The account doesn't own the item and isn't a contributor, hasn't accepted the Workshop legal agreement, or is a limited account. |
| `Workshop upload failed (Limit Exceeded)` | The preview image is 1 MB or larger. |
| `No subscription` | Add CS2 (free) to the uploader account. |
| `resourcecompiler.exe not found` | `./workshop tools install` with the Workshop Tools depot in `toolsDepots`. If the depot download is refused, the account lacks the *Counter-Strike 2 Workshop Tools* DLC. |
| `Wine prefix … is not initialised` | `./workshop tools setup-wine` |
| `… reference(s) point at files that are not in the addon or the base game` | Fix the path, add the missing file, or add an `ignore` pattern. |
| `no … was produced` for a compiled file | Run with `--verbose` or read `build-cache/logs/compile-*.log` for the compiler's own error. |
| `another workshop build or upload is already running` | Wait for the other run to finish. The lock is released automatically when it exits. |
| `verification failed` | The Steam Web API can lag a few minutes; check `./workshop status`. Private items cannot be verified anonymously. |

## Known limitations

- **Wine compilation is not yet proven end to end.** The Wine and `resourcecompiler.exe`
  integration (command line, headless Wine, output locations) follows the documented Source 2
  addon layout. The automated tests cover it with a stand-in compiler, not the real one. The
  first real run on the server is the actual test. If Valve's compiler needs different flags,
  change the `compile` template in `workshop.json`; no code changes are needed.
- The Workshop Tools depot ID is not built in; you provide it. Merging `download_depot`
  output into the tools folder has only been tested against a simulated SteamCMD.
- Map compiling is not supported.
- The Python VPK writer (single-file and multi-part VPK v2) is verified by reading its output
  back with CRC and MD5 validation. The game loading a pack written by it has not been tested
  yet. Prove it with a small private test item before updating a live pack.
- `pack-list --verify` always checks every file's CRC32. Newer Valve-written packs hash their
  chunks with truncated BLAKE3, which Python's standard library can't compute, so those
  sections are reported as "not verifiable" instead of being checked.
- Packs written by this tool have no signature block. Valve's own packs carry a 20-byte one.
  Whether CS2 requires it for Workshop addons is untested.
- Batch compile mode and the `-filelist`-style flag it needs are unverified with the real compiler.
- SteamCMD's output format is not a stable API. Error messages are recognised by known
  phrases, and anything unrecognised is reported with the raw output (`--verbose`).
- On Windows, interactive SteamCMD sessions (`--interactive`, `login`) talk to the console
  directly. Their output is read back from SteamCMD's log files, so error detection there is
  best effort.

## License

MIT. See [LICENSE](LICENSE).
