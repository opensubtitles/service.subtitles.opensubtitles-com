# Kodi Compatibility Test Plan

Target floor: **Matrix (19) → Piers (22)** — the same four branches the add-on
(and the legacy .org add-on at 5.1.5) is published for in the official Kodi
repository. `addon.xml` declares `xbmc.python 3.0.0`, which is the Matrix API.

## Automated tiers (run by CI)

| Tier | What | Where | Catches |
|------|------|-------|---------|
| 1 | pytest on Python 3.8 / 3.9 / 3.11 / 3.12 / 3.13 | `addon-check.yml` `test` job matrix | Syntax/stdlib breaks for each Kodi's embedded Python (Matrix ~3.8 … Piers ~3.12+) |
| 1b | vermin gate: shipped files must need ≤ Python 3.8 | same job (3.11 leg) | A construct newer than the Matrix floor sneaking into shipped code |
| 2 | kodi-addon-checker per branch (matrix/nexus/omega/piers) | `addon-check.yml` `kodi-addon-checker` job | Repo-compliance and metadata regressions per branch |
| 3 | Real-Kodi smoke: headless Kodi containers (matthuisman/kodi-headless), add-on + requests dependency chain installed, all shipped modules imported inside the real embedded Python | `kodi-smoke.yml` (weekly + master pushes touching shipped code) | Runtime Kodi-API drift mocks cannot see; verdict line `SMOKETEST RESULT: PASS/FAIL` in kodi.log |
| 3b | Real-Kodi end-to-end: a full GUI Kodi under Xvfb (`tests/e2e/Dockerfile*`), exactly the shipped files installed, driven over JSON-RPC by `scripts/kodi_e2e_test.py` against the **live** API | `kodi-e2e.yml` (weekly + manual with one download per release; search-only on PRs touching the suite) | The Player/subtitle-dialog contract itself: plugin listings (movie, manual, single language, TV episode), result relevance in Kodi's own dialog, download → subtitle active in the player and saved next to the video, cache hit and "Clear cache", "Test connection" (no / wrong / real credentials), "Check for updates", network-down error dialog, and no exception, API key or password in a DEBUG log |

Local run of tier 3: `bash scripts/kodi_smoke_test.sh Omega-21.3` (Docker required).

Local run of tier 3b: `python3 scripts/kodi_e2e_test.py omega` (also `matrix`,
`nexus`, `piers`; Docker and network required). `--no-download` spends no
download quota, `--keep` leaves the container up for inspection. Set
`OPENSUBTITLES_USER` / `OPENSUBTITLES_PASS` to add the real-login scenario
(and to download as that account); in CI these come from repository secrets
of the same names. Without them the suite runs anonymously and skips it.

## Tier 4 — manual end-to-end (before any upstream submission)

Tier 3b automates most of this on Linux, on distro (or, for Piers, Flatpak
beta) builds of Kodi. What it cannot cover is other platforms, real library
items, typing into the on-screen keyboard and how subtitles actually render.
Check on the
OLDEST (Matrix — Android emulator APK or an old device) and NEWEST (Piers
beta) supported Kodi; daily development on macOS covers Omega continuously.

1. Fresh install from zip → configure credentials → Test Connection shows account.
2. Play a library episode → subtitle dialog → search returns results with badges.
3. Download a subtitle → it activates and renders during playback.
4. Manual-search flow (query typed by hand) returns and downloads.
5. Check for Updates → correct dialog for the install channel (fast-track repo
   present / disabled / missing).
6. Settings open cleanly; no Development tab in a release zip.
7. `grep -i "error.*opensubtitles\|traceback" kodi.log` is silent.

## Known per-version ground truth

Tier-3 baseline run (2026-08-25, all PASS 11/11 modules):

| Kodi | Embedded Python (container) | Result |
|------|------------------------------|--------|
| Matrix 19.5 | **3.6.9** (Ubuntu 18.04 system python) | PASS |
| Nexus 20.5 | 3.10.12 | PASS |
| Omega 21.3 | 3.10.12 | PASS |
| Piers 22.0-BETA1 | 3.10.12 | PASS |

- Matrix-era Linux builds run Python **3.6**, older than commonly assumed —
  the vermin gate therefore enforces `-t=3.6-` on shipped files (CI cannot
  execute 3.6 anymore; the floor is static). Desktop Matrix builds ship 3.8;
  the pytest matrix (3.8-3.13) covers the executable spread.
- On the matrix mirror branch, prefer the `+matrix`-suffixed
  script.module builds — the unsuffixed newest (urllib3 2.x etc.) need
  Python 3.7+ and crash the 3.6 runtime (handled in kodi_smoke_test.sh).
- Piers headless images ship an empty-MySQL advancedsettings.xml template that
  crash-loops Kodi ("Failed to initialize databases"); the smoke driver
  neutralizes both /defaults and userdata copies before the test boot.
- The headless images have no window manager at all: `xbmcgui.Window(10000)`
  raises "Window id does not exist", so the window-property cache - and with
  it every search - cannot run there. That is why tier 3b uses a real Kodi
  under Xvfb instead of those images.
- Tier 3b image sources: Matrix = ubuntu:22.04, Nexus = debian:bookworm, Omega
  = debian:trixie (each distro's own `kodi` package). Debian is preferred -
  Ubuntu's archive mirrors stalled and refused connections on GitHub runners,
  so the Ubuntu build rotates through mirrors; Matrix cannot move because
  Debian 11 (Kodi 19) is end-of-life. Piers has no distro
  package yet, so it runs Team Kodi's Flatpak beta in a `--privileged`
  container; move it to a distro base once one ships Kodi 22.
- Files copied into the Flatpak Kodi's home must be root-owned: the sandbox
  drops root's capabilities and Kodi dies with "unable to load settings" on a
  userdata directory owned by another uid (handled by the e2e driver).
- Version-sensitive APIs already audited (2026-08-25): `xbmcvfs.translatePath`
  (v19+), `Dialog.textviewer` (v16+), `Addons33.db` schema + `origin` column
  (v18+), `System.BuildVersionShort` (not universal — code falls back to
  `System.BuildVersion`), JSON-RPC `Addons.GetAddonDetails` (ancient).
  Details: `docs/kodi_api_internals.md` gotchas 15-16.
