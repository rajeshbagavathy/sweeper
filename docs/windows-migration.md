# Running this project on Windows too

This project runs from the same shared codebase on both a MacBook and a
Windows machine - this is not a one-way move away from macOS, and nothing
here should change how the existing macOS setup works. Written for whichever
Claude session (or person) sets up the Windows side. Short version: **the
application code itself needs no changes** - it was already written in an
OS-agnostic way. What needs translating is the *workflow* around it: how a
session on the Mac has been starting/stopping the server, checking sweep
status, and verifying logins, all done from a POSIX shell (`bash`) with
POSIX-only tools (`lsof`, `kill`, `ps`, `curl`, `nohup ... &`) that don't exist
on Windows. See "Workflow translation" below for the PowerShell equivalents.

Anything Windows-*only* (starting with the MTQuant integration) belongs in its
own isolated module, never mixed into the files below that the macOS setup
already relies on - see `docs/mtquant-integration.md` and CLAUDE.md's own
note on this.

## Portability audit (already done, results below)

Checked specifically for the classes of bug that actually break a POSIX ->
Windows port:

| Risk | Found? | Notes |
|---|---|---|
| `fcntl` (POSIX-only file locking) | No | not used anywhere |
| `os.fork`/`os.setsid`/`resource`/`pwd`/`grp` (POSIX-only process/user APIs) | No | not used anywhere |
| `signal.SIGTERM`/`SIGKILL`/etc. handled explicitly in app code | No | process control is left to the OS/uvicorn defaults |
| `shell=True` subprocess calls | No | none |
| Hardcoded `/tmp` | No | none |
| Raw string path concatenation with `"/"` (instead of `pathlib.Path`) | No | `pathlib.Path` used consistently everywhere paths are built |
| CSV files opened without `newline=""` (Windows mangles line endings otherwise) | No | every CSV read/write already uses `newline=""` |
| `multiprocessing` start method | Fine | no explicit `set_start_method`/`get_context` call - this codebase already runs under `'spawn'` semantics on macOS (Python's default there since 3.8) and Windows also defaults to `'spawn'`, so this is already exercised, not a new code path |

**Net result: no source changes are needed to run this on Windows.** The one
genuine environment-level risk (not a code bug) is long path limits - see
below.

## Environment-level risks (not code bugs, still worth knowing)

- **Windows `MAX_PATH` (260 chars) on older/default configurations.** Each
  sweep worker gets its own copied Chromium profile directory
  (`.browser-profiles/<account>/worker-<N>/...`), and Chromium profiles nest
  fairly deep internally (cache shards, IndexedDB, etc.). Combined with a long
  Windows username or a deeply-nested project folder, this can theoretically
  exceed 260 characters. Two independent mitigations, either is enough:
  - Enable long path support (Windows 10 1607+): Group Policy `Computer
    Configuration > Administrative Templates > System > Filesystem > Enable
    Win32 long paths`, or the equivalent registry key
    (`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1`).
  - Keep the project path itself short (e.g. `C:\algosweeper\` rather than
    something nested under `Documents\...\Downloads\...`).
- **Login sessions do not travel with the files.** Copying
  `.browser-profile*/` directories across machines/OSes is not a reliable way
  to bring your AlgoTest login along - Chromium profile format compatibility
  across platforms isn't guaranteed, and even if the files open fine, a fresh
  device fingerprint may not honor an old session anyway. **Plan to log in
  fresh on Windows** (`uv run python tools/record.py`, then just do the login
  step - see CLAUDE.md's own note on this tool). Don't spend time trying to
  transplant `.browser-profile/`.
- **`.env` is not committed** (correctly - it holds `ALGOTEST_EMAIL`/
  `ALGOTEST_PASSWORD` and any second/third account credentials). Copy it
  manually, or recreate it from `.env.example`, on the new machine - it will
  NOT come across in a `git clone`/`git pull`.
- **Line endings**: git may be configured to normalize `LF`/`CRLF` on
  checkout depending on `core.autocrlf`. This only affects source files (not
  the CSVs, which are read/written with explicit `newline=""` regardless of
  what git does to line endings in the working tree at rest) - a difference
  here would show up as a large, noisy diff on first commit from Windows, not
  as a functional bug. If that happens, it's a git config mismatch, not a
  reason to touch the Python.

## Setup on Windows

Same steps as `README.md`'s "Setup" section, translated:

```powershell
uv venv
uv sync
uv run playwright install chromium
copy .env.example .env      # then fill in credentials with your own editor
```

Then:
```powershell
uv run python webapp.py            # http://127.0.0.1:8765
```
Playwright itself is fully cross-platform (it manages its own bundled
Chromium builds per-OS under the hood) - no Playwright-specific Windows setup
beyond the normal `playwright install`.

## Workflow translation (macOS/Linux shell -> PowerShell)

These are commands a Claude session has been running throughout this
project's history for verification/debugging - not part of the application,
but needed to do the same kind of work on Windows.

| Task | macOS/Linux (bash) | Windows (PowerShell) |
|---|---|---|
| Check what's listening on 8765 | `lsof -iTCP:8765 -sTCP:LISTEN -P` | `Get-NetTCPConnection -LocalPort 8765 -State Listen` (or `netstat -ano \| findstr :8765`, then look up the PID) |
| Stop the webapp process | `kill <pid>` | `Stop-Process -Id <pid>` |
| Force-stop if it won't respond | `kill -9 <pid>` | `Stop-Process -Id <pid> -Force` |
| Start it in the background, capture logs | `nohup .venv/bin/python webapp.py > /tmp/webapp.log 2>&1 &` | `Start-Process -FilePath .venv\Scripts\python.exe -ArgumentList webapp.py -RedirectStandardOutput webapp.log -RedirectStandardError webapp.err -NoNewWindow` (or just run it in its own terminal tab/window - simpler, and you can watch it live) |
| Hit an endpoint | `curl -s http://127.0.0.1:8765/api/status` | `Invoke-RestMethod http://127.0.0.1:8765/api/status` (returns a parsed object directly, no need to pipe through a JSON parser) |
| POST with a body | `curl -s -X POST url -d '{"a":1}' -H "Content-Type: application/json"` | `Invoke-RestMethod -Method Post -Uri url -Body '{"a":1}' -ContentType "application/json"` |
| Poll until a condition (no busy-wait) | `until <check>; do sleep 2; done` | `while (-not (<check>)) { Start-Sleep -Seconds 2 }` |
| List/kill a stray Chromium process | `ps aux \| grep -i chrome`, `kill <pid>` | `Get-Process chrome,"Google Chrome for Testing" -ErrorAction SilentlyContinue`, `Stop-Process -Id <pid>` |
| Check whether the sweep/webapp is still alive after a kill | `lsof -iTCP:8765 -sTCP:LISTEN` returning nothing | `Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue` returning nothing |

One behavioral difference worth knowing about, not just a syntax translation:
on macOS, killing the webapp process while it's mid-syscall can leave it
briefly in an uninterruptible ("UN") state where it doesn't respond to the
kill signal for a few seconds before finishing on its own - this is normal and
resolves itself; don't escalate to a force-kill immediately. Windows'
`Stop-Process` doesn't have a direct equivalent of this POSIX-signal nuance,
but the same general principle applies: give a graceful stop a few seconds
before reaching for `-Force`.

## Before setting up the Windows side

- **Commit everything on the Mac first.** Check `git status` - if there's
  uncommitted work, it will NOT travel to Windows via anything other than the
  actual `.git` history (a one-off zip/copy of the working directory would
  carry it, but that's a dead end for ongoing work on two machines - see
  below). Verify with `git log --oneline -3` that the commit you're about to
  bring across matches what you expect.
- **No git remote is configured yet** (see CLAUDE.md's "Known stale/open
  items") - working from two machines going forward needs one. Set up a
  remote (private GitHub repo, or similar) and `git clone` it on Windows,
  rather than a one-time file copy - that's what makes it possible to keep
  both machines' code in sync afterward (push from whichever machine you just
  worked on, pull on the other before you start). `output/` (CSV results,
  trade reports) and `.browser-profile*/` are both git-ignored already and
  won't come through the clone - decide separately whether you want that
  historical result data on Windows too (a plain file copy of just `output/`
  is fine for that, it's just data, not code).
- **`.venv/` does not transfer between OSes** - it's OS/arch-specific compiled
  packages. Always re-run `uv venv && uv sync` fresh on the new machine rather
  than copying `.venv/` over.
