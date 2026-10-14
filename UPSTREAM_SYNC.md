# Keeping ElegooSlicer MCP in sync with upstream

ElegooSlicer MCP is ElegooSlicer plus the Remote API from the OrcaSlicer MCP fork
(MaxEllis/OrcaSlicer) and a few Elegoo-specific fixes. This file explains how to:

1. [move to a new ElegooSlicer version](#1-moving-to-a-new-elegooslicer-version);
2. [pull new Remote API fixes from MaxEllis](#2-pulling-new-remote-api-fixes-from-maxellis).

Elegoo publishes a new version every few weeks, so the diff against Elegoo is kept small
on purpose: new code lives in `src/slic3r/GUI/RemoteAPI/`, and changes to Elegoo files are
short hooks.

## Current state

Update this table every time you sync.

| What | Value |
|---|---|
| Elegoo base tag | `v1.5.3.5` = `3f1042e` |
| Remote API source | `MaxEllis/OrcaSlicer@fa3c9ee` (tag `v2.4.2-mcp.13`, branch `remote-api-242`) |
| `elegoo-link` pin (`deps/elegoolink/elegoolink.cmake`) | `46c7b814e055cf9675d58482d79f43d0bd2280da` |
| `ELEGOO_MCP_RELEASE` (`version.inc`) | `1` |
| Upstream fixes cherry-picked on top | ADMesh `stl_read` overflow fix (Elegoo `main` `2d507e3`, after `v1.5.3.5`) |
| MCP settings schema | extracted from `src/libslic3r/PrintConfig.cpp` of `v1.5.3.5` (858 keys) |

Remotes used below (one-time setup):

```bash
git remote add elegoo  https://github.com/elegooofficial/ElegooSlicer.git
git remote add orcamcp https://github.com/MaxEllis/OrcaSlicer.git
```

## 1. Moving to a new ElegooSlicer version

The examples use `OLD=v1.5.3.5` (the base in the table above) and `NEW=v1.5.3.6`.
Commands are for Git Bash.

### 1.1 Fetch and look before you rebase

```bash
OLD=v1.5.3.5
NEW=v1.5.3.6
git fetch elegoo --tags
git rev-parse "$NEW^{commit}"
git merge-base --is-ancestor $OLD $NEW && echo "linear upgrade"

# Elegoo files we touch, and how much Elegoo changed each of them:
TOUCHED=$(git diff --name-only $OLD remote-api -- . ':!src/slic3r/GUI/RemoteAPI' ':!docs' ':!.github' ':!*.md')
git diff --stat $OLD $NEW -- $TOUCHED
```

Big changes in `GUI_App.cpp`, `Plater.cpp` or `Preferences.cpp` mean manual work ahead.

### 1.2 Rebase on a new branch

Never rewrite `remote-api` in place: that needs a force-push, and force-pushes are agreed
with Victor first.

```bash
git switch -c remote-api-${NEW#v} remote-api
git rebase --onto $NEW $OLD
```

`--onto $NEW $OLD` replays exactly our commits (everything after `$OLD`), even if Elegoo's
tags are not on one straight line.

### 1.3 Where conflicts will show up

**A. The 10 hunks that were resolved by hand** when the port was first applied (rows marked
R in the hook map of the port notes). They sit next to Elegoo branding/version code, which
Elegoo edits often:

| File | Where | Our change |
|---|---|---|
| `src/libslic3r/libslic3r_version.h.in` | after `#define ELEGOOSLICER_VERSION` | `ELEGOO_MCP_RELEASE`, `ELEGOO_MCP_RELEASE_STR`, `ELEGOO_MCP_VERSION` |
| `src/slic3r/GUI/AboutDialog.cpp` | the version label (`_L("ElegooSlicer ") + " " + ...`) | `GUI_App::format_display_version()` instead of `SLIC3R_BUILD_ID` |
| `src/slic3r/GUI/GUI_App.cpp` | constructor `GUI_App::GUI_App()` | `m_remote_api_server = std::make_unique<RemoteAPI::Server>();` **(required: crash on exit without it)** |
| `src/slic3r/GUI/GUI_App.cpp` | `check_new_version_sf`, right after the Linux early `return` | update check against the GitHub releases of `ShiftSad/ElegooSlicer-MCP`, comparing the `-mcp.N` ordinal; Elegoo's own code stays below it, unreachable (do not delete it) |
| `src/slic3r/GUI/GUI_App.cpp` | `format_display_version()` | `version_display = ELEGOO_MCP_VERSION;` |
| `src/slic3r/GUI/GUI_App.cpp` | after `stop_sync_user_preset()` | definitions of `remote_api_server()`, `start_remote_api()`, `stop_remote_api()` **(required: link error without them)** |
| `src/slic3r/GUI/GUI_App.hpp` | after `#include "slic3r/GUI/HttpServer.hpp"` | `namespace Slic3r { namespace GUI { namespace RemoteAPI { class Server; class Controller; } } }` **(required)** |
| `src/slic3r/GUI/Plater.cpp` | `set_project_name` (non-Apple branch) | title `m_project_name + " - " SLIC3R_APP_NAME " MCP"` |
| `src/slic3r/GUI/Plater.cpp` | `update_title_dirty_status` | same suffix (keep both titles in step) |
| `version.inc` | next to `ORCA_COMPATIBLE_VERSION` | `set(ELEGOO_MCP_RELEASE "N")` |

**B. Every `// elegoo-mcp:` hook.** Code we wrote inside Elegoo files is marked:

```bash
git grep -n "elegoo-mcp:"
```

**C. Hooks copied verbatim from the fork.** They keep the fork's own comments (easier
to re-sync with MaxEllis), so they have no marker. All of them are in the files listed by:

```bash
git diff --stat $OLD remote-api -- . ':!src/slic3r/GUI/RemoteAPI' ':!docs' ':!.github' ':!*.md'
```

Today that is `bbs_3mf.{cpp,hpp}`, `src/slic3r/CMakeLists.txt` (the RemoteAPI sources),
`GCodeViewer.{cpp,hpp}`, `GLCanvas3D.{cpp,hpp}`, `GUI_App.{cpp,hpp}`, `MainFrame.cpp`,
`NotificationManager.hpp`, `Plater.{cpp,hpp}`, `Preferences.{cpp,hpp}` and `Tab.cpp`.

**D. The ADMesh cherry-pick** (`deps_src/admesh/stlinit.cpp`). If the new Elegoo version
already contains it, git drops it as already applied or stops on an empty commit:
`git rebase --skip`, then remove it from the table above.

**E. The `elegoo-link` pin.** If Elegoo changed `deps/elegoolink/elegoolink.cmake`, take
their version and pin it again to an exact SHA (they used the floating `origin/main`
before). Do not keep `46c7b81` blindly: Elegoo's new code may need a newer SDK. Record the
new SHA in the table.

Rules while resolving:

- Take Elegoo's new code and put our hook back on top. Keep hooks small.
- Never append `-mcp` to `ELEGOOSLICER_VERSION` or `SLIC3R_VERSION`: Elegoo uses them for
  3MF metadata, profile updates, user agents and the G-code `version` placeholder.
- When replacing Elegoo behaviour (the updater), leave their code unreachable instead of
  deleting it.

### 1.4 Check what changed under the API

Compiling catches changed signatures. It does not catch changed behaviour, such as a new
modal dialog on a path the API uses (model load, tab switch, arrange/orient jobs, project
save, preset selection). Look at the headers the controller uses and at new dialogs:

```bash
git diff $OLD $NEW -- src/slic3r/GUI/Plater.hpp src/slic3r/GUI/GUI_App.hpp \
    src/slic3r/GUI/GLCanvas3D.hpp src/slic3r/GUI/GCodeViewer.hpp src/slic3r/GUI/Tab.hpp \
    src/slic3r/GUI/NotificationManager.hpp src/libslic3r/Model.hpp src/libslic3r/Preset.hpp \
    src/libslic3r/PresetBundle.hpp src/libslic3r/Format/bbs_3mf.hpp
git diff $OLD $NEW -- src/slic3r/GUI | grep -nE "^\+.*(ShowModal|MessageDialog)"
```

### 1.5 Version number

`ELEGOOSLICER_VERSION` comes from Elegoo's `version.inc`; do not touch it. Bump
`ELEGOO_MCP_RELEASE` to the next N. N is global and only goes up, also across Elegoo
versions (the in-app update check compares only N). The release tag will be
`v<new Elegoo version>-mcp.<N>`, created only after Victor confirms.

### 1.6 Redo the `PrintConfig.cpp` key diff

New or removed config keys matter for the API's key policy and for the MCP server's
settings schema.

```bash
keys() { git show "$1:src/libslic3r/PrintConfig.cpp" \
           | grep -oE 'this->add(_nullable)?\("[A-Za-z0-9_]+"' \
           | sed -E 's/^.*\("//; s/"$//' | sort -u; }
keys $OLD > keys-old.txt
keys $NEW > keys-new.txt
comm -13 keys-old.txt keys-new.txt    # keys Elegoo added
comm -23 keys-old.txt keys-new.txt    # keys Elegoo removed
keys v2.4.2 | comm -13 - keys-new.txt # Elegoo-only keys vs OrcaSlicer 2.4.2 (needs the Orca tag)
```

(`v1.5.3.5` has 867 `add(...)` keys; the Elegoo-only ones are `bed_texture_area`,
`support_mms` and `support_wan_network`.)

For each new key, decide:

- **Sensitive?** Anything that holds G-code or scripts, post-processing, connection or
  host settings, printer identity, or decides where prints go (cloud/LAN, multi-material
  unit) must be refused unless the user allows sensitive edits. Keys ending in `_gcode`
  and physical-printer options are covered automatically; add others to
  `is_sensitive_config_key()` in `src/slic3r/GUI/RemoteAPI/RemoteAPIController.cpp` **and**
  to the MCP server's `guard.py` (the two lists must stay in step).
- **Read-only?** Nothing to do: keys Elegoo marks `readonly` are refused with
  `422 readonly_key` automatically.

### 1.7 Regenerate the MCP settings schema

In the MCP server (`mcp/`):

```bash
uv run python scripts/extract_settings_schema.py --src <this repo>/src/libslic3r/PrintConfig.cpp
```

It rewrites `src/orcaslicer_mcp/data/print_settings_schema.json`. Commit it together with
any `guard.py` change.

### 1.8 Build and test

- **Build.** Locally with the wrapper (`tools\remote-api\build.cmd onlydeps` only if
  `git diff --quiet $OLD $NEW -- deps` reports changes, then
  `tools\remote-api\build.cmd dlweb slicer`),
  or push the branch as `ci-<name>` and let `.github/workflows/build_elegoo_mcp.yml` build
  it. Check the Device-tab web UI URL in `scripts/download_web_dep.bat` too: Elegoo pins a
  release date there.
- **Run** with a separate data directory (`elegoo-slicer.exe --datadir <test dir>`, or the
  portable zip, which has its own `data_dir`), never against the real
  `%APPDATA%\ElegooSlicer`.
- **Identity:** `GET /status` shows the new `app_version` and `mcp_build` = N; the window
  title ends in "- ElegooSlicer MCP"; About shows `<version>-mcp.N`; the update check only
  contacts `api.github.com/repos/ShiftSad/ElegooSlicer-MCP`.
- **API runtime check** (from the MCP server; its default port is the fork's 13131):
  `ORCA_BASE=http://127.0.0.1:13130 ORCA_TOKEN=<token> python scripts/remote-api-runtime-check.py`
- **MCP unit tests:** `uv run --extra dev pytest`. On Windows 15 tests fail on the stock
  0.1.17 suite (Windows-only test assumptions); anything beyond those is new.
- **MCP smoke** on the "Elegoo Centauri Carbon 2 0.4 nozzle" profile: `get_status`,
  `load_model`, `slice_and_wait`, `get_slice_breakdown`, `render_plate` (editor and
  preview), `list_plates`, `set_config`, `select_preset`, `describe_plate`, plus the newer
  routes (orient one object, view, volumes, project save, paint), and the printer routes
  against the fake CC2. `tools/remote-api/run_all_checks.sh` runs every runtime check
  (see `tools/remote-api/README.md`).
- **GUI unchanged** for people who do not use the API: sending to the printer, CANVAS sync
  and the Device tab work as in the official ElegooSlicer.

### 1.9 Finish

Update the table at the top, push the new branch, and agree with Victor before moving
`remote-api` to it (force-push) or publishing a release.

## 2. Pulling new Remote API fixes from MaxEllis

The API was ported from `MaxEllis/OrcaSlicer@fa3c9ee`. New commits after that:

```bash
git fetch orcamcp --tags
git ls-remote --heads orcamcp          # the branch name follows the Orca base (remote-api-242 today)
git log --oneline fa3c9ee..orcamcp/remote-api-242 -- src/slic3r/GUI/RemoteAPI
# hooks in other files and the API docs:
git log --oneline fa3c9ee..orcamcp/remote-api-242 -- src version.inc docs/remote-api ':!src/slic3r/GUI/RemoteAPI'
```

For each commit:

1. Read it (`git show <sha>`). Skip what this repo does not port: the fork's `.github/`,
   `README.md`, `docs/research/`, Orca splash SVGs, `tests/libslic3r/test_placeholder_parser.cpp`
   and `ORCA_MCP_RELEASE` bumps.
2. Apply it with `git cherry-pick -x <sha>` (records the source commit). For a commit that
   mixes wanted and unwanted files:
   `git show <sha> -- src/slic3r/GUI/RemoteAPI | git apply -3`, then commit with
   "cherry picked from MaxEllis/OrcaSlicer@<sha>" in the message.
3. Expect conflicts where this repo already changed the fork's code:
   - `/status`: `ELEGOOSLICER_VERSION` instead of `SoftFever_VERSION`, plus
     `orca_compatible_version`, `mcp_build`, `view` and the extra capabilities;
   - `is_sensitive_config_key()` (`support_mms`, `support_wan_network`) and the
     `422 readonly_key` refusals;
   - `RemoteAPIServer.cpp`: exclusive bind on Windows (`SO_EXCLUSIVEADDRUSE`) and the
     socket/`io_context` destruction order;
   - arrange/orient resetting Elegoo's sticky "outside bed" arrange state, and no Beginner
     Guide during API calls;
   - `POST /model` `404 file_not_found`, transform rotations composed about the world axes
     plus the drop onto the bed, plates marked unsliced after config and object edits;
   - the routes in `RemoteAPIModelOps.cpp` (orient one object, view, volumes, project
     save, paint).
4. Doc changes (`docs/remote-api/README.md`, `openapi.yaml`) are merged by hand: ours have
   extra routes and ElegooSlicer naming. `docs/` is in Elegoo's `.gitignore`, so stage
   files there with `git add -f <file>`.

Then build and run the tests from 1.8, and update the fork SHA in the table at the top and
in the introduction of `docs/remote-api/README.md`, so the next `git log` starts from there.

If MaxEllis moved to a newer OrcaSlicer base (a new `remote-api-*` branch), the hooks may
have changed shape as well:
`git diff fa3c9ee orcamcp/<new branch> -- src version.inc ':!src/slic3r/GUI/RemoteAPI'`.

The MCP server lives in `mcp/`, added with `git subtree` from `MaxEllis/orcaslicer-mcp`
(0.1.17 plus our `elegoo` branch, full history kept, module name unchanged). New upstream
versions merge in with:

```bash
git remote add mcpupstream https://github.com/MaxEllis/orcaslicer-mcp.git   # once
git fetch mcpupstream
git log --oneline v0.1.17..mcpupstream/main          # what is new upstream
git subtree pull --prefix=mcp mcpupstream main       # merge it into mcp/
```

Expect conflicts in `server.py`, `client.py`, `README.md`, `lhm.plugin.json` and the
tests. Afterwards run `uv run pytest` and `uv run python scripts/gen_lhm_tools.py --check`
in `mcp/`. Our root `.gitignore` ignores every `docs/` folder; `mcp/.gitignore` re-includes
`mcp/docs/`.

## CI notes

- `.github/workflows/build_elegoo_mcp.yml` builds every push to `ci`, `ci-*` and
  `remote-api` (docs-only pushes are skipped) and uploads a portable zip with
  `SHA256SUMS` as a workflow artifact.
- The dependencies are cached under a key made from `deps/**`, `cmake/modules/**` and the
  workflow file. An Elegoo upgrade that touches `deps/` (or any edit of the workflow)
  rebuilds them once, which adds roughly 40-75 minutes.
- GitHub caches are per branch: a run can use caches from its own branch and from the
  default branch. Making `remote-api` the default branch of the GitHub repository lets
  every branch reuse its dependency cache.
