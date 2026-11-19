#!/usr/bin/env python3
"""Real-Kodi end-to-end suite: drive the add-on through Kodi itself.

    scripts/kodi_e2e_test.py omega            # or matrix / nexus / piers
    scripts/kodi_e2e_test.py omega --no-download --keep

Where the import smoke test (scripts/kodi_smoke_test.sh) only proves the modules
load, this boots a real GUI Kodi under Xvfb (tests/e2e/Dockerfile*), installs
exactly the files that ship (scripts/addon_manifest.py) plus the
script.module.requests dependency chain from the official Kodi mirror, and
runs these scenarios against it:

  plugin searches   automatic / manual / single-language / TV-episode listings,
                    fetched the way the subtitle dialog fetches them
  cache             a repeated search is served from the cache, and the
                    "Clear cache" settings button really empties it
  settings buttons  "Test connection" without credentials, "Check for updates"
  bad login         wrong credentials end in a clean 401 dialog and status
  real login        (only with OPENSUBTITLES_USER / OPENSUBTITLES_PASS set)
  subtitle dialog   Kodi's own dialog during playback: results are for the
                    right title, selecting one downloads it and the player
                    shows it; also for a TV episode
  offline           with the network cut the user gets an error dialog, not a
                    Python traceback
  log hygiene       no add-on exception, API key or password in a DEBUG log

It talks to the LIVE OpenSubtitles.com API, so it needs network access and each
run with downloading enabled spends one download of the anonymous (or the
given account's) daily quota. JSON-RPC is reached with `docker exec curl`; no
port is published on the host.

Standard library only.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E2E_DIR = os.path.join(REPO_ROOT, "tests", "e2e")
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
from addon_manifest import iter_addon_files  # noqa: E402

ADDON_ID = "service.subtitles.opensubtitles-com"
HELPER_ID = "script.opensubtitles.e2ehelper"
MEDIA_DIR = "/media/e2e"
MIRROR = "https://mirrors.kodi.tv/addons"
DISTRO_HOME = "/root/.kodi"
FLATPAK_HOME = "/root/.var/app/tv.kodi.Kodi/data"

# How each Kodi release is obtained. Stable releases come from the distro whose
# own package is that Kodi; a pre-release only exists as Team Kodi's Flatpak
# beta, which needs a privileged container for its sandbox.
RELEASES = {
    "matrix": {"dockerfile": "Dockerfile", "build_args": {"BASE": "ubuntu:22.04"},
               "branch": "matrix", "home": DISTRO_HOME, "run_args": []},
    "nexus": {"dockerfile": "Dockerfile", "build_args": {"BASE": "debian:bookworm"},
              "branch": "nexus", "home": DISTRO_HOME, "run_args": []},
    "omega": {"dockerfile": "Dockerfile", "build_args": {"BASE": "debian:trixie"},
              "branch": "omega", "home": DISTRO_HOME, "run_args": []},
    "piers": {"dockerfile": "Dockerfile.flatpak", "build_args": {"FLATPAK_BRANCH": "beta"},
              "branch": "piers", "home": FLATPAK_HOME, "run_args": ["--privileged"]},
}

# Enabled in this order: a dependency must be enabled before its dependant.
DEPENDENCIES = [
    "script.module.certifi",
    "script.module.chardet",
    "script.module.charset-normalizer",
    "script.module.idna",
    "script.module.urllib3",
    "script.module.requests",
]

# Web server + JSON-RPC over HTTP on, no auth: only reachable inside the container.
ADVANCED_SETTINGS = """<advancedsettings>
  <services>
    <webserver>true</webserver><webserverport>8080</webserverport>
    <webserverauthentication>false</webserverauthentication>
    <esenabled>false</esenabled><zeroconf>false</zeroconf>
  </services>
  <splash>false</splash>
  <general><addonnotifications>false</addonnotifications></general>
</advancedsettings>
"""

BUILD_TIMEOUT_SECONDS = 1500

MOVIE_FILE = "The.Matrix.1999.1080p.BluRay.x264.mkv"
EPISODE_FILE = "Breaking.Bad.S01E01.720p.BluRay.x264.mkv"
BAD_USER, BAD_PASS = "e2e_no_such_user_zz91", "wrong-pass-e2e-7731"

# Kodi window ids
WIN_HOME, WIN_FULLSCREEN_VIDEO = 10000, 12005
WIN_YESNO, WIN_OK, WIN_ADDON_SETTINGS, WIN_SUBTITLE_SEARCH = 10100, 12002, 10140, 10153
# Subtitle search dialog controls (fixed in Kodi's GUIDialogSubtitles.cpp)
SUB_LIST, SERVICE_LIST = 120, 150

SEARCH_EN = "action=search&languages=English&preferredlanguage=English"


class E2EFailure(Exception):
    pass


def run(cmd, check=True):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          errors="replace")
    if check and proc.returncode != 0:
        raise E2EFailure(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc


def http_get(url):
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def check(name, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        raise E2EFailure(name)


class Kodi:
    def __init__(self, container, home):
        self.container = container
        self.home = home
        self._nonce = 0

    # -- plumbing ---------------------------------------------------------
    def exec(self, *cmd, check=True):
        return run(["docker", "exec", self.container, *cmd], check=check)

    def rpc(self, method, params=None, timeout=120):
        body = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        proc = self.exec("curl", "-s", "-m", str(timeout), "-X", "POST",
                         "-H", "Content-Type: application/json",
                         "-d", json.dumps(body), "http://127.0.0.1:8080/jsonrpc", check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            raise E2EFailure(f"{method}: no JSON-RPC response (curl exit {proc.returncode})")
        reply = json.loads(proc.stdout)
        if "error" in reply:
            raise E2EFailure(f"{method}: {reply['error']}")
        return reply["result"]

    def wait_for_rpc(self, seconds=180):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                if self.rpc("JSONRPC.Ping", timeout=5) == "pong":
                    return
            except (E2EFailure, ValueError):
                pass
            time.sleep(3)
        raise E2EFailure(f"Kodi JSON-RPC did not come up within {seconds}s")

    def wait_until(self, what, probe, seconds=60, interval=1.5):
        """Poll probe() until it returns something truthy, and return that."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            value = probe()
            if value:
                return value
            time.sleep(interval)
        raise E2EFailure(f"timed out after {seconds}s waiting for {what}")

    def log(self):
        return self.exec("cat", f"{self.home}/temp/kodi.log", check=False).stdout

    def log_mark(self):
        return len(self.log())

    def log_since(self, mark):
        return self.log()[mark:]

    # -- GUI ----------------------------------------------------------------
    def window(self):
        return self.rpc("GUI.GetProperties", {"properties": ["currentwindow"]})["currentwindow"]["id"]

    def labels(self, *names):
        return self.rpc("XBMC.GetInfoLabels", {"labels": list(names)})

    def dialog_text(self):
        """Heading and body of the dialog on screen (ok / yes-no dialogs)."""
        got = self.labels("Control.GetLabel(1)", "Control.GetLabel(9)")
        return got["Control.GetLabel(1)"], got["Control.GetLabel(9)"]

    def wait_window(self, *window_ids, seconds=60):
        return self.wait_until(f"window {'/'.join(map(str, window_ids))}",
                               lambda: self.window() in window_ids and self.window(), seconds)

    def idle(self):
        """Back out of every dialog until the home screen or the video is on top."""
        for _ in range(10):
            if self.window() in (WIN_HOME, WIN_FULLSCREEN_VIDEO):
                return
            self.rpc("Input.Back")
            time.sleep(1.5)
        raise E2EFailure(f"could not get back to an idle window (stuck on {self.window()})")

    # -- player -------------------------------------------------------------
    def play(self, filename):
        self.rpc("Player.Open", {"item": {"file": f"{MEDIA_DIR}/{filename}"}})

        def playing():
            players = self.rpc("Player.GetActivePlayers")
            if not players:
                return False
            item = self.rpc("Player.GetItem", {"playerid": players[0]["playerid"],
                                               "properties": ["file"]})["item"]
            return item.get("file", "").endswith(filename)
        self.wait_until(f"playback of {filename}", playing)
        self.wait_window(WIN_FULLSCREEN_VIDEO)

    def stop(self):
        for player in self.rpc("Player.GetActivePlayers"):
            self.rpc("Player.Stop", {"playerid": player["playerid"]})
        self.wait_until("playback to stop", lambda: not self.rpc("Player.GetActivePlayers"))

    # -- the add-on -----------------------------------------------------------
    def list_plugin(self, query):
        """What Kodi's subtitle dialog does: list the add-on's plugin:// URL."""
        url = f"plugin://{ADDON_ID}/?{query}"
        return self.rpc("Files.GetDirectory", {"directory": url}).get("files") or []

    def helper(self, *params):
        """Run the Kodi-side helper add-on and return its verdict payload."""
        mark = self.log_mark()
        self.rpc("Addons.ExecuteAddon", {"addonid": HELPER_ID, "params": list(params)})

        def verdict():
            found = re.search(r"E2EHELPER (OK|FAIL) (.*)", self.log_since(mark))
            return found and (found.group(1), found.group(2))
        status, rest = self.wait_until("helper add-on verdict", verdict, 30)
        if status != "OK":
            raise E2EFailure(f"helper add-on failed: {rest}")
        return json.loads(rest.split(" ", 1)[1])

    def run_addon_script(self, script):
        """Press a settings.xml RunScript() button."""
        self.helper("builtin", f"RunScript(special://home/addons/{ADDON_ID}/{script})")

    def get_settings(self, *ids):
        self._nonce += 1
        return self.helper("getsettings", f"n{self._nonce}", *ids)

    def set_settings(self, values):
        """Via a file, so credentials never pass through a loggable RPC call."""
        local = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        try:
            json.dump(values, local)
            local.close()
            remote = f"{MEDIA_DIR}/e2e-settings.json"
            run(["docker", "cp", local.name, f"{self.container}:{remote}"])
            self.exec("chmod", "644", remote)
            self.helper("loadsettings", remote)
            self.exec("rm", "-f", remote)
        finally:
            os.unlink(local.name)

    def test_connection(self):
        """Press "Test connection"; return the result dialog's (heading, text)."""
        self.run_addon_script("test_connection.py")
        self.wait_window(WIN_OK, seconds=90)
        shown = self.dialog_text()
        self.rpc("Input.Select")
        # the script reopens the add-on settings once the result is dismissed
        self.wait_window(WIN_ADDON_SETTINGS, seconds=30)
        self.idle()
        return shown


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------
def stage(stage_dir, branch):
    """Lay out <stage>/addons and <stage>/userdata for copying into Kodi's home."""
    addons_dir = os.path.join(stage_dir, "addons")
    for src, rel in iter_addon_files(REPO_ROOT):
        dest = os.path.join(addons_dir, ADDON_ID, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)
    shutil.copytree(os.path.join(E2E_DIR, HELPER_ID), os.path.join(addons_dir, HELPER_ID))

    installed = []
    for dep in DEPENDENCIES:
        try:
            index = http_get(f"{MIRROR}/{branch}/{dep}/").decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            if err.code != 404:
                raise
            index = ""  # dependency sets differ per branch
        zips = sorted(set(re.findall(re.escape(dep) + r"-[0-9.+a-z]*\.zip", index)),
                      key=lambda name: [int(n) for n in re.findall(r"\d+", name)])
        if not zips:
            print(f"  dep skipped (not on {branch}): {dep}")
            continue
        archive = os.path.join(stage_dir, zips[-1])
        with open(archive, "wb") as fh:
            fh.write(http_get(f"{MIRROR}/{branch}/{dep}/{zips[-1]}"))
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(addons_dir)
        installed.append(dep)
        print(f"  dep staged: {zips[-1]}")

    userdata = os.path.join(stage_dir, "userdata")
    os.makedirs(userdata)
    with open(os.path.join(userdata, "advancedsettings.xml"), "w", encoding="utf-8") as fh:
        fh.write(ADVANCED_SETTINGS)
    return addons_dir, userdata, installed


def copy_as_root(src_dir, container, dest_dir):
    """`docker cp` a directory, owned by root inside the container.

    A plain `docker cp` keeps the host uid. The Flatpak sandbox drops root's
    capabilities, so there Kodi cannot write into a userdata directory that
    belongs to someone else and dies with "unable to load settings".
    """
    def as_root(info):
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        return info

    with tempfile.TemporaryFile() as archive:
        with tarfile.open(fileobj=archive, mode="w") as tar:
            tar.add(src_dir, arcname=os.path.basename(src_dir), filter=as_root)
        archive.seek(0)
        proc = subprocess.run(["docker", "cp", "-", f"{container}:{dest_dir}/"],
                              stdin=archive, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise E2EFailure(f"docker cp into {container} failed: {proc.stderr.strip()}")


def default_api_key():
    root = ET.parse(os.path.join(REPO_ROOT, "resources", "settings.xml")).getroot()
    node = root.find(".//setting[@id='APIKey']/default")
    return (node.text or "").strip() if node is not None else ""


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------
def check_listing(name, files, allowed_languages):
    check(f"{name}: returned results", len(files) > 0, f"{len(files)} subtitles")
    bad = [f["file"] for f in files
           if not f["file"].startswith(f"plugin://{ADDON_ID}/?action=download&id=")]
    check(f"{name}: every entry is a download link", not bad, bad[0] if bad else "")
    languages = {f.get("label") for f in files}
    check(f"{name}: languages within request", languages <= set(allowed_languages),
          ", ".join(sorted(str(lang) for lang in languages)))


def scenario_plugin_searches(kodi, ctx):
    kodi.play(MOVIE_FILE)
    check_listing("movie, automatic", kodi.list_plugin(SEARCH_EN), ["English"])
    manual = "action=manualsearch&searchstring=" + urllib.parse.quote("Inception 2010")
    check_listing("manual, two languages",
                  kodi.list_plugin(manual + "&languages=English,French&preferredlanguage=English"),
                  ["English", "French"])
    check_listing("manual, non-English only",
                  kodi.list_plugin(manual + "&languages=Czech&preferredlanguage=Czech"), ["Czech"])
    kodi.stop()
    kodi.play(EPISODE_FILE)
    check_listing("TV episode, automatic", kodi.list_plugin(SEARCH_EN), ["English"])


def scenario_cache(kodi, ctx):
    kodi.play(MOVIE_FILE)
    kodi.list_plugin(SEARCH_EN)  # make sure the entry exists whatever ran before
    mark = kodi.log_mark()
    again = kodi.list_plugin(SEARCH_EN)
    check("repeated search served from cache",
          "CACHE HIT: Returning cached subtitles" in kodi.log_since(mark), f"{len(again)} subtitles")

    mark = kodi.log_mark()
    kodi.run_addon_script("clear_cache.py")
    kodi.wait_until("clear_cache.py to finish",
                    lambda: "clear_cache.py): script successfully run" in kodi.log_since(mark), 30)
    mark = kodi.log_mark()
    fresh = kodi.list_plugin(SEARCH_EN)
    since = kodi.log_since(mark)
    check("\"Clear cache\" button empties the cache",
          "CACHE HIT: Returning cached subtitles" not in since and "CACHE SAVE" in since,
          f"{len(fresh)} subtitles fetched again")


def scenario_settings_buttons(kodi, ctx):
    heading, text = kodi.test_connection()
    status = kodi.get_settings("account_status", "account_details")
    check("\"Test connection\" without credentials asks for them",
          heading == "OpenSubtitles.com" and "username and password" in text, text)
    check("... and records it in the account status",
          status["account_status"].startswith("Not Verified"), status["account_status"])

    kodi.run_addon_script("check_updates.py")
    kodi.wait_window(WIN_YESNO, WIN_OK, seconds=90)
    heading, text = kodi.dialog_text()
    check("\"Check for updates\" reports a result",
          heading == "OpenSubtitles.com" and len(text) > 10, text.splitlines()[0])


def scenario_bad_login(kodi, ctx):
    kodi.set_settings({"OSuser": BAD_USER, "OSpass": BAD_PASS})
    try:
        heading, text = kodi.test_connection()
        status = kodi.get_settings("account_status")["account_status"]
        # 1.x appends the API's "401 ..." detail, 2.x shows the localized line only
        check("wrong credentials: explained in a dialog",
              "401" in text or "authenticate" in text.lower(), text.splitlines()[0])
        check("wrong credentials: account status says so", "401" in status, status)
    finally:
        kodi.set_settings({"OSuser": "", "OSpass": ""})


def scenario_real_login(kodi, ctx):
    user, password = ctx["credentials"]
    kodi.set_settings({"OSuser": user, "OSpass": password})
    heading, text = kodi.test_connection()
    status = kodi.get_settings("account_status", "account_details")
    # 1.x: "OK (Free User)" / "OK (VIP)"; 2.x: "Free account" / "OK (VIP)"
    check("real account: login succeeds",
          status["account_status"].startswith("OK") or status["account_status"] == "Free account",
          f"{status['account_status']} | {status['account_details']}")
    check("real account: dialog shows the quota", "Downloads today" in text)
    # credentials stay set: the subtitle dialog scenario then downloads as this account


def open_subtitle_dialog(kodi, title_token):
    """Open Kodi's subtitle dialog on the playing video; check what it lists."""
    kodi.rpc("GUI.ActivateWindow", {"window": "subtitlesearch"})
    kodi.wait_window(WIN_SUBTITLE_SEARCH, seconds=30)
    count_label = f"Container({SUB_LIST}).NumItems"

    def results():
        value = kodi.labels(count_label)[count_label]
        return value.isdigit() and int(value)
    count = kodi.wait_until("the subtitle dialog to list results", results, 120)
    service = f"Container({SERVICE_LIST}).ListItemAbsolute(0).Label"
    check("dialog lists the add-on as service", kodi.labels(service)[service] == "OpenSubtitles.com")
    check("dialog shows results", count > 0, f"{count} subtitles")

    wanted = [f"Container({SUB_LIST}).ListItemAbsolute({i}).Label2" for i in range(min(count, 10))]
    releases = list(kodi.labels(*wanted).values())
    relevant = [name for name in releases if title_token in name.lower()]
    check(f"results are for the right title (\"{title_token}\")",
          len(relevant) >= 0.7 * len(releases),
          f"{len(relevant)}/{len(releases)} of the top hits, e.g. {releases[0]}")


def scenario_subtitle_dialog(kodi, ctx):
    kodi.exec("sh", "-c", f"rm -f {MEDIA_DIR}/*.srt")
    kodi.play(MOVIE_FILE)
    open_subtitle_dialog(kodi, "matrix")
    if ctx["download"]:
        kodi.rpc("Input.Select")  # first result has focus once the search finished
        kodi.wait_window(WIN_FULLSCREEN_VIDEO, seconds=90)

        def subtitle_loaded():
            players = kodi.rpc("Player.GetActivePlayers")
            props = kodi.rpc("Player.GetProperties", {
                "playerid": players[0]["playerid"],
                "properties": ["subtitles", "currentsubtitle", "subtitleenabled"]})
            return props["subtitles"] and props
        props = kodi.wait_until("the player to load the subtitle", subtitle_loaded, 30)
        current = props["currentsubtitle"]
        check("selected subtitle is active in the player",
              # Kodi 22 reports the two-letter code, older releases the three-letter one
              props["subtitleenabled"] and current.get("language") in ("en", "eng"),
              f"{current.get('name')} [{current.get('language')}]")
        saved = kodi.exec("sh", "-c", f"ls {MEDIA_DIR}/*.srt && head -c 4000 {MEDIA_DIR}/*.srt",
                          check=False)
        check("subtitle saved next to the video, with cues",
              saved.returncode == 0 and "-->" in saved.stdout, saved.stdout.splitlines()[0]
              if saved.stdout else "no .srt")
    else:
        print("  [SKIP] download through the dialog (--no-download)")
        kodi.idle()

    kodi.stop()
    kodi.play(EPISODE_FILE)
    open_subtitle_dialog(kodi, "breaking")


def scenario_offline(kodi, ctx):
    kodi.play(MOVIE_FILE)
    query = ("action=manualsearch&searchstring=" + urllib.parse.quote("Alien 1979")
             + "&languages=English&preferredlanguage=English")
    outcome = {}

    def search():
        try:
            outcome["files"] = kodi.list_plugin(query)
        except E2EFailure as failure:  # Kodi may report the empty listing as an error
            outcome["error"] = str(failure)

    run(["docker", "network", "disconnect", "bridge", kodi.container])
    try:
        worker = threading.Thread(target=search, daemon=True)
        worker.start()
        # The add-on blocks on an error dialog; the listing returns once it is dismissed.
        kodi.wait_window(WIN_OK, seconds=90)
        heading, text = kodi.dialog_text()
        check("network down: the user gets an error dialog",
              heading == "OpenSubtitles.com" and "unavailable" in text, text.splitlines()[0])
        kodi.rpc("Input.Select")
        worker.join(60)
        check("network down: the search ends with no results",
              not worker.is_alive() and not outcome.get("files"))
    finally:
        run(["docker", "network", "connect", "bridge", kodi.container], check=False)
    time.sleep(3)
    kodi.idle()
    check_listing("network back: search works again", kodi.list_plugin(SEARCH_EN), ["English"])


def scenario_log_hygiene(kodi, ctx):
    log = kodi.log()
    check("log is a DEBUG log", " debug <" in log.lower())
    blocks = [b for b in log.split("EXCEPTION Thrown")[1:] if ADDON_ID in b.split("-->End of")[0]]
    check("no Python exception from the add-on", not blocks,
          blocks[0].split("-->End of")[0].strip().splitlines()[-1].strip() if blocks else "")
    secrets = {"API key": ctx["api_key"], "wrong-login password": BAD_PASS}
    if ctx["credentials"]:
        secrets["account password"] = ctx["credentials"][1]
    for label, secret in secrets.items():
        check(f"{label} never written to the log", not secret or secret not in log)


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("release", choices=sorted(RELEASES))
    parser.add_argument("--no-download", action="store_true",
                        help="never download a subtitle; spends no quota")
    parser.add_argument("--keep", action="store_true",
                        help="leave the container running for inspection")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # live progress in CI logs

    release = RELEASES[args.release]
    image = f"os-kodi-e2e:{args.release}"
    container = f"kodi-e2e-{args.release}"
    kodi = Kodi(container, release["home"])
    user, password = os.getenv("OPENSUBTITLES_USER"), os.getenv("OPENSUBTITLES_PASS")
    ctx = {
        "download": not args.no_download,
        "credentials": (user, password) if user and password else None,
        "api_key": default_api_key(),
    }
    scenarios = [
        ("plugin searches", scenario_plugin_searches),
        ("cache", scenario_cache),
        ("settings buttons", scenario_settings_buttons),
        ("bad login", scenario_bad_login),
        ("real login", scenario_real_login if ctx["credentials"] else None),
        ("subtitle dialog", scenario_subtitle_dialog),
        ("offline", scenario_offline),
        ("log hygiene", scenario_log_hygiene),
    ]
    failed, skipped = [], []
    stage_dir = tempfile.mkdtemp(prefix="kodi-e2e-")
    try:
        print(f"== build {image} ==")
        build = ["docker", "build", "--progress=plain", "-t", image,
                 "-f", os.path.join(E2E_DIR, release["dockerfile"])]
        for key, value in release["build_args"].items():
            build += ["--build-arg", f"{key}={value}"]
        # Output is streamed, not captured: a stalled package download must be
        # visible in the CI log, and must not run into the job timeout.
        try:
            built = subprocess.run(build + [E2E_DIR], timeout=BUILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise E2EFailure(f"image build exceeded {BUILD_TIMEOUT_SECONDS}s")
        if built.returncode != 0:
            raise E2EFailure(f"image build failed ({built.returncode})")

        print("== stage add-ons ==")
        addons_dir, userdata, deps = stage(stage_dir, release["branch"])
        run(["docker", "rm", "-f", container], check=False)
        run(["docker", "create", "--name", container, *release["run_args"], image])
        for path in (addons_dir, userdata):
            copy_as_root(path, container, kodi.home)

        print("== boot ==")
        run(["docker", "start", container])
        kodi.wait_for_rpc()
        version = kodi.rpc("Application.GetProperties", {"properties": ["version"]})["version"]
        print(f"  Kodi {version['major']}.{version['minor']} ({version.get('tag', '')})")
        # Add-ons copied in by hand start disabled (Kodi 18+); enable like a user would.
        for addon in deps + [ADDON_ID, HELPER_ID]:
            kodi.rpc("Addons.SetAddonEnabled", {"addonid": addon, "enabled": True})
        details = kodi.rpc("Addons.GetAddonDetails",
                           {"addonid": ADDON_ID, "properties": ["enabled", "version"]})["addon"]
        check("add-on installed and enabled", details["enabled"], f"v{details['version']}")
        kodi.rpc("Settings.SetSettingValue", {"setting": "debug.showloginfo", "value": True})
        time.sleep(3)
        kodi.idle()  # Kodi asks about each hand-installed add-on at startup
        kodi.exec("sh", "-c",
                  f"for f in {MOVIE_FILE} {EPISODE_FILE}; do "
                  "ffmpeg -loglevel error -y -f lavfi -i testsrc=size=320x240:rate=5 -t 1800 "
                  f"-c:v mpeg4 -q:v 31 {MEDIA_DIR}/$f || exit 1; done")

        for name, scenario in scenarios:
            print(f"== {name} ==")
            if scenario is None:
                print("  [SKIP] OPENSUBTITLES_USER / OPENSUBTITLES_PASS not set")
                skipped.append(name)
                continue
            try:
                scenario(kodi, ctx)
            except E2EFailure as failure:
                print(f"  scenario aborted: {failure}")
                failed.append(name)
            # leave a clean slate for the next scenario
            try:
                kodi.idle()
                kodi.stop()
            except E2EFailure as failure:
                print(f"  cleanup failed: {failure}")
                failed.append(f"{name} (cleanup)")
                break
    except E2EFailure as failure:
        print(f"\nSETUP FAILURE: {failure}")
        failed.append("setup")
    finally:
        if failed:
            noise = ("OpenSink", "InitSink")
            tail = [line for line in kodi.log().splitlines() if not any(n in line for n in noise)]
            print("---- kodi.log (tail) ----\n" + "\n".join(tail[-80:]))
        shutil.rmtree(stage_dir, ignore_errors=True)
        if args.keep:
            print(f"container kept: {container}")
        else:
            run(["docker", "rm", "-f", container], check=False)

    if skipped:
        print(f"skipped: {', '.join(skipped)}")
    if failed:
        print(f"failed: {', '.join(failed)}")
    print(f"== [{args.release}] E2E {'FAIL' if failed else 'PASS'} ==")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
