#!/usr/bin/env python3
"""Real-Kodi end-to-end test: search and download a subtitle through Kodi itself.

    scripts/kodi_e2e_test.py omega            # or matrix / nexus
    scripts/kodi_e2e_test.py omega --no-download --keep

Where the import smoke test (scripts/kodi_smoke_test.sh) only proves the modules
load, this drives the add-on the way Kodi's subtitle dialog does:

  1. build a real GUI Kodi image running under Xvfb (tests/e2e/Dockerfile)
  2. install exactly the files that ship (scripts/addon_manifest.py) plus the
     script.module.requests dependency chain from the official Kodi mirror
  3. enable the add-ons over JSON-RPC
  4. play a synthetic video named like a real release
  5. list plugin://<id>/?action=search (what the subtitle dialog calls) and
     ?action=manualsearch, for a movie and for a TV episode
  6. list ?action=download for the first hit and check a real .srt landed
  7. fail on any Python exception the add-on left in kodi.log

It talks to the LIVE OpenSubtitles.com API, so it needs network access and each
run with downloading enabled spends one download of the anonymous (or, if
OPENSUBTITLES_USER / OPENSUBTITLES_PASS are set, that account's) daily quota.
JSON-RPC is reached with `docker exec curl`; no port is published on the host.

Standard library only.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from xml.sax.saxutils import escape

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
from addon_manifest import iter_addon_files  # noqa: E402

ADDON_ID = "service.subtitles.opensubtitles-com"
KODI_HOME = "/root/.kodi"
MEDIA_DIR = "/media/e2e"
MIRROR = "https://mirrors.kodi.tv/addons"

# release -> (distro base image whose own package is that Kodi, mirror branch)
RELEASES = {
    "matrix": ("ubuntu:22.04", "matrix"),
    "nexus": ("ubuntu:24.04", "nexus"),
    "omega": ("debian:trixie", "omega"),
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

MOVIE_FILE = "The.Matrix.1999.1080p.BluRay.x264.mkv"
EPISODE_FILE = "Breaking.Bad.S01E01.720p.BluRay.x264.mkv"


class E2EFailure(Exception):
    pass


def run(cmd, check=True, **kwargs):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
    if check and proc.returncode != 0:
        raise E2EFailure(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc


def http_get(url):
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


class Kodi:
    def __init__(self, container):
        self.container = container

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

    def wait_for_rpc(self, seconds=120):
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                if self.rpc("JSONRPC.Ping", timeout=5) == "pong":
                    return
            except (E2EFailure, ValueError):
                pass
            time.sleep(3)
        raise E2EFailure(f"Kodi JSON-RPC did not come up within {seconds}s")

    def list_plugin(self, query):
        """What Kodi's subtitle dialog does: list the add-on's plugin:// URL."""
        url = f"plugin://{ADDON_ID}/?{query}"
        return self.rpc("Files.GetDirectory", {"directory": url}).get("files") or []

    def play(self, filename):
        self.rpc("Player.Open", {"item": {"file": f"{MEDIA_DIR}/{filename}"}})
        deadline = time.time() + 60
        while time.time() < deadline:
            players = self.rpc("Player.GetActivePlayers")
            if players:
                item = self.rpc("Player.GetItem", {"playerid": players[0]["playerid"],
                                                   "properties": ["file"]})["item"]
                if item.get("file", "").endswith(filename):
                    return
            time.sleep(2)
        raise E2EFailure(f"playback of {filename} never started")

    def stop(self):
        for player in self.rpc("Player.GetActivePlayers"):
            self.rpc("Player.Stop", {"playerid": player["playerid"]})
        time.sleep(2)

    def log(self):
        return self.exec("cat", f"{KODI_HOME}/temp/kodi.log", check=False).stdout


def stage_addons(stage_dir, branch):
    """Lay out <stage>/addons/ with the shipped add-on and its dependencies."""
    addons_dir = os.path.join(stage_dir, "addons")
    for src, rel in iter_addon_files(REPO_ROOT):
        dest = os.path.join(addons_dir, ADDON_ID, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)

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
    return addons_dir, installed


def stage_credentials(stage_dir):
    """Optional login: without it the add-on searches and downloads anonymously."""
    user, password = os.getenv("OPENSUBTITLES_USER"), os.getenv("OPENSUBTITLES_PASS")
    if not (user and password):
        return None
    userdata = os.path.join(stage_dir, "userdata")
    data_dir = os.path.join(userdata, "addon_data", ADDON_ID)
    os.makedirs(data_dir)
    with open(os.path.join(data_dir, "settings.xml"), "w", encoding="utf-8") as fh:
        fh.write('<settings version="2">\n'
                 f'    <setting id="OSuser">{escape(user)}</setting>\n'
                 f'    <setting id="OSpass">{escape(password)}</setting>\n'
                 '</settings>\n')
    return userdata


def check(name, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    if not condition:
        raise E2EFailure(name)


def check_listing(name, files, allowed_languages):
    check(f"{name}: returned results", len(files) > 0, f"{len(files)} subtitles")
    bad = [f["file"] for f in files
           if not f["file"].startswith(f"plugin://{ADDON_ID}/?action=download&id=")]
    check(f"{name}: every entry is a download link", not bad, bad[0] if bad else "")
    languages = {f.get("label") for f in files}
    check(f"{name}: languages within request", languages <= set(allowed_languages),
          ", ".join(sorted(str(l) for l in languages)))


def run_scenarios(kodi, download):
    kodi.exec("sh", "-c",
              f"mkdir -p {MEDIA_DIR} && for f in {MOVIE_FILE} {EPISODE_FILE}; do "
              "ffmpeg -loglevel error -y -f lavfi -i testsrc=size=320x240:rate=5 -t 900 "
              f"-c:v mpeg4 -q:v 31 {MEDIA_DIR}/$f || exit 1; done")

    version = kodi.rpc("Application.GetProperties", {"properties": ["version"]})["version"]
    print(f"== Kodi {version['major']}.{version['minor']} ({version.get('tag', '')}) ==")

    print("== movie ==")
    kodi.play(MOVIE_FILE)
    movie = kodi.list_plugin("action=search&languages=English&preferredlanguage=English")
    check_listing("automatic search", movie, ["English"])
    manual = kodi.list_plugin("action=manualsearch&searchstring=" + urllib.parse.quote("Inception 2010")
                              + "&languages=English,French&preferredlanguage=English")
    check_listing("manual search", manual, ["English", "French"])

    if download:
        print("== download ==")
        query = movie[0]["file"].split("?", 1)[1]
        got = kodi.list_plugin(query)
        check("download: one subtitle returned", len(got) == 1, f"{len(got)} entries")
        path = got[0]["file"]
        check("download: path is a subtitle file", path.endswith(".srt"), os.path.basename(path))
        head = kodi.exec("head", "-c", "4000", path, check=False)
        check("download: file exists and holds cues", head.returncode == 0 and "-->" in head.stdout)
    else:
        print("== download skipped (--no-download) ==")

    print("== TV episode ==")
    kodi.stop()
    kodi.play(EPISODE_FILE)
    episode = kodi.list_plugin("action=search&languages=English&preferredlanguage=English")
    check_listing("automatic search", episode, ["English"])
    kodi.stop()

    print("== kodi.log ==")
    log = kodi.log()
    blocks = [b for b in log.split("EXCEPTION Thrown")[1:] if ADDON_ID in b]
    check("no Python exception from the add-on", not blocks,
          blocks[0].strip().splitlines()[-2].strip() if blocks else "")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("release", choices=sorted(RELEASES))
    parser.add_argument("--no-download", action="store_true",
                        help="search only; spends no download quota")
    parser.add_argument("--keep", action="store_true",
                        help="leave the container running for inspection")
    args = parser.parse_args()

    base, branch = RELEASES[args.release]
    image = f"os-kodi-e2e:{args.release}"
    container = f"kodi-e2e-{args.release}"
    kodi = Kodi(container)
    stage_dir = tempfile.mkdtemp(prefix="kodi-e2e-")
    ok = False
    try:
        print(f"== build {image} (from {base}) ==")
        run(["docker", "build", "-q", "-t", image, "--build-arg", f"BASE={base}",
             os.path.join(REPO_ROOT, "tests", "e2e")])

        print("== stage add-ons ==")
        addons_dir, deps = stage_addons(stage_dir, branch)
        userdata = stage_credentials(stage_dir)
        print(f"  login: {'account from environment' if userdata else 'anonymous'}")

        run(["docker", "rm", "-f", container], check=False)
        run(["docker", "create", "--name", container, image])
        run(["docker", "cp", addons_dir, f"{container}:{KODI_HOME}/"])
        if userdata:
            # merges into the userdata dir the image already has
            run(["docker", "cp", userdata + "/.", f"{container}:{KODI_HOME}/userdata/"])

        print("== boot ==")
        run(["docker", "start", container])
        kodi.wait_for_rpc()
        # Add-ons copied in by hand start disabled (Kodi 18+); enable like a user would.
        for addon in deps + [ADDON_ID]:
            kodi.rpc("Addons.SetAddonEnabled", {"addonid": addon, "enabled": True})
        details = kodi.rpc("Addons.GetAddonDetails",
                           {"addonid": ADDON_ID, "properties": ["enabled", "version"]})["addon"]
        check("add-on installed and enabled", details["enabled"], f"v{details['version']}")

        run_scenarios(kodi, download=not args.no_download)
        ok = True
    except E2EFailure as failure:
        print(f"\nE2E FAILURE: {failure}")
        tail = [line for line in kodi.log().splitlines() if "OpenSink" not in line and "InitSink" not in line]
        print("---- kodi.log (tail) ----\n" + "\n".join(tail[-60:]))
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
        if args.keep:
            print(f"container kept: {container}")
        else:
            run(["docker", "rm", "-f", container], check=False)

    print(f"== [{args.release}] E2E {'PASS' if ok else 'FAIL'} ==")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
