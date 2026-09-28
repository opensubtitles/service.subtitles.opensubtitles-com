"""Regression tests for the 15 findings of the external v2 review (PR #92).

One test (or group) per finding, named after the defect it pins. Each asserts
the BEHAVIOUR that was wrong, so a refactor that reintroduces the bug fails
here rather than in a user's Kodi.
"""
import os
import struct
import sys
import time
from unittest.mock import MagicMock, patch

import pytest
import xbmcaddon
import xbmcgui

from resources.lib.audio_demux import UnsupportedSource, _mp4_boxes
from resources.lib.exceptions import InvalidResponse, ProviderError
from resources.lib.osclient.provider import OpenSubtitlesProvider
import resources.lib.background_service as service
import resources.lib.transcriber as transcriber
import resources.lib.utilities as utilities


def _code_only(source):
    """Source with comments and docstring prose stripped.

    These checks are about what the code DOES; a comment explaining the old
    behaviour must not satisfy - or trip - them.
    """
    lines = []
    for line in source.splitlines():
        code = line.split("#", 1)[0].rstrip()
        if code.strip():
            lines.append(code)
    return "\n".join(lines)


# --- provider.py:381 - token survived an account change --------------------

def test_jwt_cache_key_is_bound_to_the_credentials():
    a = OpenSubtitlesProvider("key", "user_a", "pass_a")
    b = OpenSubtitlesProvider("key", "user_b", "pass_b")
    same_user_new_password = OpenSubtitlesProvider("key", "user_a", "changed")

    assert a._token_cache_key != b._token_cache_key
    assert a._token_cache_key != same_user_new_password._token_cache_key
    # a second provider for the SAME credentials must still reuse the token
    assert a._token_cache_key == OpenSubtitlesProvider("key", "user_a", "pass_a")._token_cache_key
    # the key may not carry the credentials themselves
    assert "user_a" not in a._token_cache_key and "pass_a" not in a._token_cache_key


def test_account_switch_does_not_inherit_the_previous_token():
    first = OpenSubtitlesProvider("key", "old_user", "old_pass")
    first.cache = MagicMock()
    store = {}
    first.cache.set.side_effect = lambda key, value, expires=None: store.__setitem__(key, value)
    first.cache.get.side_effect = lambda key: store.get(key)
    first.user_token = "token-of-old-account"

    second = OpenSubtitlesProvider("key", "new_user", "new_pass")
    second.cache = MagicMock()
    second.cache.get.side_effect = lambda key: store.get(key)
    assert second.user_token is None, "new account read the previous account's JWT"


# --- provider.py:687 - ratings never reached the API ------------------------

def test_rate_subtitle_does_not_touch_undefined_attributes():
    provider = OpenSubtitlesProvider("key", "", "")       # no credentials
    provider.cache = MagicMock()
    provider.cache.get.return_value = None
    # must return False, NOT raise AttributeError for self.logged_in/base_url
    assert provider.rate_subtitle(12345, 5) is False


def test_rate_subtitle_posts_with_the_bearer_token():
    provider = OpenSubtitlesProvider("key", "user", "pass")
    provider.cache = MagicMock()
    provider.cache.get.return_value = "jwt-token"
    response = MagicMock(status_code=200)
    with patch.object(provider.session, "post", return_value=response) as post:
        assert provider.rate_subtitle("777", 4, sync=True) is True
    url = post.call_args[0][0]
    assert url == "https://api.opensubtitles.com/api/v1/subtitles/rate"
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer jwt-token"
    assert post.call_args.kwargs["json"] == {"subtitle_id": 777, "rating": 4, "sync": True}


# --- transcriber.py:614 - result url received the bearer token --------------

@pytest.mark.parametrize("url,is_api", [
    ("https://api.opensubtitles.com/api/v1/ai/files/1", True),
    ("http://api.opensubtitles.com/api/v1/ai/files/1", False),   # plain http
    ("https://api.opensubtitles.com.evil.tld/x", False),         # suffix trick
    ("https://evil.tld/?x=api.opensubtitles.com", False),        # query trick
    ("https://cdn.opensubtitles.com/result.srt", False),         # other host
])
def test_only_the_api_origin_is_credentialed(url, is_api):
    assert transcriber._is_api_origin(url) is is_api


def _anon_session_stub(content=b"1\n"):
    """Stand-in for requests.Session() used by the off-origin fetch path.

    Tests are 100% offline: without this the real Session would try to resolve
    the example host.
    """
    anon = MagicMock()
    anon.get.return_value = MagicMock(status_code=200, content=content)
    anon.__enter__ = lambda self=anon: anon
    anon.__exit__ = lambda *a: False
    fake_requests = MagicMock()
    fake_requests.Session.return_value = anon
    return anon, fake_requests


def test_off_origin_result_url_is_fetched_without_credentials(tmp_path):
    session = MagicMock()
    headers = {"Authorization": "Bearer secret", "Api-Key": "key"}
    anon, fake_requests = _anon_session_stub(b"1\n")
    with patch.dict(sys.modules, {"requests": fake_requests}):
        with patch.object(transcriber, "_profile_dir", return_value=str(tmp_path)):
            out = transcriber._save_completed_result(
                session, {"url": "https://someone-elses-cdn.example/out.srt"}, headers)
    sent = anon.get.call_args.kwargs["headers"]
    assert "Authorization" not in sent and "Api-Key" not in sent
    # the caller's dict is untouched for the next request
    assert headers["Authorization"] == "Bearer secret"
    # and the subtitle still gets written
    assert os.path.exists(out)


def test_api_origin_result_url_keeps_credentials(tmp_path):
    session = MagicMock()
    session.get.return_value = MagicMock(status_code=200, content=b"1\n")
    with patch.object(transcriber, "_profile_dir", return_value=str(tmp_path)):
        transcriber._save_completed_result(
            session, {"url": "https://api.opensubtitles.com/api/v1/ai/files/9"},
            {"Authorization": "Bearer secret"})
    assert session.get.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"


# --- audio_demux.py:33 / :92 - hang and unbounded allocation ---------------

def test_zero_sized_extended_box_terminates():
    # 'moov' box declaring extended size 0: the old loop never advanced
    box = struct.pack(">I4sQ", 1, b"moov", 0) + b"\x00" * 16
    boxes = list(_mp4_boxes(box, 0, len(box)))     # must simply end
    assert boxes == []


def test_box_claiming_more_than_its_parent_is_rejected():
    box = struct.pack(">I4s", 1 << 20, b"moov") + b"\x00" * 8
    assert list(_mp4_boxes(box, 0, len(box))) == []


def test_huge_stsz_count_does_not_allocate(tmp_path):
    """A tiny file claiming 4 billion fixed-size samples must be refused."""
    from resources.lib import audio_demux

    data = bytearray()
    # stsz: version/flags, sample_size=1024, count=0xFFFFFFFF
    stsz_payload = struct.pack(">III", 0, 1024, 0xFFFFFFFF)
    stsz = struct.pack(">I4s", 8 + len(stsz_payload), b"stsz") + stsz_payload
    data += stsz
    buf = bytes(data)

    with patch.object(audio_demux, "_mp4_find", side_effect=lambda b, path, s, e: (8, len(buf))):
        with pytest.raises(UnsupportedSource):
            # reaching the stsz branch is enough; the guard fires before any
            # list of 4 billion entries is built
            audio_demux._extract_mp4_from(buf, str(tmp_path / "out.aac"))


def test_extract_mp4_maps_the_file_instead_of_reading_it(tmp_path):
    """extract_mp4 must not pull the entire movie into memory."""
    import mmap as mmap_module
    from resources.lib import audio_demux

    movie = tmp_path / "movie.mp4"
    movie.write_bytes(struct.pack(">I4s", 16, b"ftyp") + b"isom" + b"\0" * 4 + b"\0" * 32)

    calls = []
    real_mmap = mmap_module.mmap

    def spy(fileno, length, **kwargs):
        calls.append((fileno, length))
        return real_mmap(fileno, length, **kwargs)

    with patch.object(mmap_module, "mmap", side_effect=spy):
        with pytest.raises(UnsupportedSource):     # no moov box in this stub
            audio_demux.extract_mp4(str(movie), str(tmp_path / "out.aac"))
    assert calls, "the file was not memory-mapped"


def test_extract_mp4_refuses_a_file_too_small_to_be_mp4(tmp_path):
    from resources.lib import audio_demux
    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(b"\0" * 4)
    with pytest.raises(UnsupportedSource):
        audio_demux.extract_mp4(str(tiny), str(tmp_path / "out.aac"))


# --- android_audio.py:322 - decoder input buffer overflow ------------------

def test_sample_larger_than_the_input_buffer_is_refused():
    """The copy is bounded by the DESTINATION capacity, never the sample size."""
    import inspect
    from resources.lib import android_audio
    source = _code_only(inspect.getsource(android_audio))
    assert "min(n, isize.value)" in source, "input copy is not bounded by isize"
    assert "ctypes.memmove(iptr, sample_buf.raw, copy_n)" in source
    # the clamped case raises rather than queueing a truncated frame
    assert "larger than the device decoder" in source


# --- background_service.py:474 - failed copy deleted the user's sidecar ----

def _player():
    player = service.OpenSubtitlesPlayer()
    player.monitor = MagicMock()
    player.monitor.abortRequested.return_value = False
    return player


def test_existing_subtitle_survives_a_failed_replacement():
    player = _player()
    state = {"target": True, "backup": False}

    def exists(path):
        return state["backup"] if path.endswith(".osbak") else state["target"]

    def rename(src, dst):
        if src.endswith(".osbak"):       # restore
            state["backup"], state["target"] = False, True
        else:                            # move aside
            state["target"], state["backup"] = False, True
        return True

    with patch.object(service, "xbmcvfs") as vfs:
        vfs.exists.side_effect = exists
        vfs.rename.side_effect = rename
        vfs.copy.return_value = False            # VFS copy fails
        vfs.delete.side_effect = lambda p: None
        with patch("shutil.copyfile", side_effect=OSError("smb unreachable")):
            result = player._store_subtitle_copy("/tmp/new.srt", "/video/movie.en.srt")

    assert result is None                        # replacement honestly failed
    assert state["target"] is True, "the user's original subtitle was lost"


def test_successful_replacement_removes_the_backup():
    player = _player()
    deleted = []
    with patch.object(service, "xbmcvfs") as vfs:
        vfs.exists.return_value = True
        vfs.rename.return_value = True
        vfs.copy.return_value = True
        vfs.delete.side_effect = deleted.append
        result = player._store_subtitle_copy("/tmp/new.srt", "/video/movie.en.srt")
    assert result == "/video/movie.en.srt"
    assert any(str(p).endswith(".osbak") for p in deleted), "backup left behind"


# --- background_service.py:646 - late thread hijacked new playback ---------

def test_stale_generation_is_discarded():
    player = _player()
    player.playback_generation = 7
    assert player._generation_is_current(7) is True
    assert player._generation_is_current(6) is False


def test_new_playback_bumps_the_generation():
    player = _player()
    addon = xbmcaddon.Addon()
    addon.setSetting("auto_download", "false")
    addon.setSetting("prompt_rating", "false")
    before = player.playback_generation
    with patch.object(service, "threading"):
        player._handle_playback_started()
    assert player.playback_generation == before + 1


# --- background_service.py:595 - fallback started a paid translation -------

def test_paid_only_results_are_never_auto_downloaded():
    """With nothing but on-demand AI translations, auto-download stands down."""
    import inspect
    source = _code_only(inspect.getsource(service.OpenSubtitlesPlayer._auto_download_flow))
    assert "is_on_demand_translation" in source
    assert "ranked[0]" not in source, "fallback still takes the top result blindly"


# --- background_service.py:902 - accepted sync offer raised immediately ----

def test_sync_offer_passes_the_video_path():
    player = MagicMock()
    session = {"sub_path": "/tmp/sub.srt", "media": {"file_original_path": "/video/movie.mkv"}}
    fake_syncer = MagicMock()
    fake_syncer.sync_subtitle.return_value = {"path": "/tmp/sub.synced.srt", "offset_ms": 120}
    fake_syncer.EngineNotAvailable = type("EngineNotAvailable", (Exception,), {})
    fake_syncer.SyncError = type("SyncError", (Exception,), {})

    with patch.dict(sys.modules, {"resources.lib.syncer": fake_syncer}):
        with patch.object(service.xbmcgui, "Dialog") as dialog:
            dialog.return_value.yesno.return_value = True
            with patch("os.path.exists", return_value=True):
                service._offer_sync(player, session)

    assert fake_syncer.sync_subtitle.call_args.kwargs["video_path"] == "/video/movie.mkv"


def test_sync_offer_explains_a_failure_instead_of_going_silent():
    player = MagicMock()
    session = {"sub_path": "/tmp/sub.srt", "media": {"file_original_path": "/video/movie.mkv"}}
    fake_syncer = MagicMock()
    fake_syncer.EngineNotAvailable = type("EngineNotAvailable", (Exception,), {})

    class SyncError(Exception):
        pass
    fake_syncer.SyncError = SyncError
    fake_syncer.sync_subtitle.side_effect = SyncError("Audio could not be read.")

    with patch.dict(sys.modules, {"resources.lib.syncer": fake_syncer}):
        with patch.object(service.xbmcgui, "Dialog") as dialog:
            dialog.return_value.yesno.return_value = True
            service._offer_sync(player, session)
        assert dialog.return_value.ok.called, "user accepted the offer and heard nothing"


# --- subtitle_downloader.py:961 - [SYNC] retimed a stale subtitle ----------

def test_remembered_subtitle_is_rejected_for_a_different_video(tmp_path):
    sub = tmp_path / "movie.en.srt"
    sub.write_text("1\n")
    with patch.object(utilities, "__name__", utilities.__name__):
        utilities.remember_loaded_subtitle(str(sub), "/video/first.mkv")
        with patch("xbmc.Player") as player:
            player.return_value.getPlayingFile.return_value = "/video/second.mkv"
            assert utilities.recall_loaded_subtitle() == ""
        # and the stale value is cleared, not merely hidden
        assert xbmcgui.Window(10000).getProperty(utilities.LAST_SUBTITLE_PROP) == ""


def test_remembered_subtitle_is_returned_for_the_same_video(tmp_path):
    sub = tmp_path / "movie.en.srt"
    sub.write_text("1\n")
    utilities.remember_loaded_subtitle(str(sub), "/video/first.mkv")
    with patch("xbmc.Player") as player:
        player.return_value.getPlayingFile.return_value = "/video/first.mkv"
        assert utilities.recall_loaded_subtitle() == str(sub)


# --- subtitle_downloader.py:747 - refreshed quota reverted -----------------

def test_quota_refresh_writes_through_to_the_state_file(tmp_path):
    from resources.lib import account_state
    with patch.object(account_state, "_state_path", return_value=str(tmp_path / "account_state.json")):
        account_state.save_account_state({"account_details": "Quota: 5 downloads left today",
                                          "account_status": "OK (Free User)"})
        account_state.update_account_state({"account_details": "Quota: 4 downloads left today"})
        state = account_state.load_account_state()
    assert state["account_details"] == "Quota: 4 downloads left today"
    assert state["account_status"] == "OK (Free User)", "unrelated fields were dropped"


# --- subtitle_downloader.py:415 - malformed payload aborted the chain ------

def test_unusable_payload_keeps_the_remaining_fallbacks_running():
    from resources.lib.subtitle_downloader import SubtitleDownloader

    downloader = SubtitleDownloader.__new__(SubtitleDownloader)
    downloader.open_subtitles = MagicMock()
    downloader.open_subtitles.search_subtitles.side_effect = InvalidResponse("bad shape")

    results, ok = downloader._search_subtitles({"query": "x"})
    assert results is None
    assert ok is True, "an unusable response still ends the search chain"
    assert downloader.invalid_response_seen is True


def test_hard_provider_error_still_stops_the_chain():
    from resources.lib.subtitle_downloader import SubtitleDownloader

    downloader = SubtitleDownloader.__new__(SubtitleDownloader)
    downloader.open_subtitles = MagicMock()
    downloader.open_subtitles.search_subtitles.side_effect = ProviderError("gateway down")
    with patch("resources.lib.subtitle_downloader.error"):
        results, ok = downloader._search_subtitles({"query": "x"})
    assert (results, ok) == (None, False)


# --- transcriber.py:387 - overlapping runs shared one temp file ------------

def test_transcription_temp_paths_are_invocation_unique(tmp_path):
    with patch.object(transcriber, "_profile_dir", return_value=str(tmp_path)):
        path = transcriber._out_path("transcribe_audio.mp3")
    assert os.path.basename(path) != "transcribe_audio.mp3"
    assert path.endswith(".mp3")
    assert transcriber._INVOCATION_TOKEN in path


def test_stale_transcription_temp_files_are_swept(tmp_path):
    old = tmp_path / "transcribe_audio.deadbeef.mp3"
    fresh = tmp_path / "transcribe_audio.cafe0000.mp3"
    other = tmp_path / "account_state.json"
    for f in (old, fresh, other):
        f.write_text("x")
    os.utime(str(old), (time.time() - 7 * 60 * 60,) * 2)
    os.utime(str(other), (time.time() - 99 * 60 * 60,) * 2)

    with patch.object(transcriber, "_profile_dir", return_value=str(tmp_path)):
        transcriber._sweep_stale_temp_files()

    assert not old.exists(), "stale temp file left behind"
    assert fresh.exists(), "a concurrent invocation's file was deleted"
    assert other.exists(), "swept a file that is not ours"


# === second review pass: defects introduced by the first round of fixes ======

# --- transcriber.py:680 - Api-Key still shipped via the shared session ------

def test_off_origin_fetch_does_not_use_the_credentialed_session(tmp_path):
    """The provider's Session carries Api-Key in session.headers, and requests
    MERGES those with per-request headers - so an off-origin fetch must not use
    that session at all."""
    provider_session = MagicMock()
    provider_session.headers = {"Api-Key": "secret-key"}

    anon, fake_requests = _anon_session_stub()

    with patch.dict(sys.modules, {"requests": fake_requests}):
        with patch.object(transcriber, "_profile_dir", return_value=str(tmp_path)):
            transcriber._save_completed_result(
                provider_session, {"url": "https://cdn.example/out.srt"},
                {"Authorization": "Bearer secret", "Api-Key": "secret-key"})

    provider_session.get.assert_not_called()
    sent = anon.get.call_args.kwargs["headers"]
    assert set(sent) == {"User-Agent"}, f"credentials leaked to an off-origin host: {sent}"


# --- background_service.py:542 - concurrent workers shared one .osbak -------

def test_each_replacement_gets_its_own_backup_path():
    player = _player()
    seen = []

    with patch.object(service, "xbmcvfs") as vfs:
        vfs.exists.return_value = True
        vfs.rename.side_effect = lambda src, dst: (seen.append(dst), True)[1]
        vfs.copy.return_value = True
        vfs.delete.side_effect = lambda p: None
        player._store_subtitle_copy("/tmp/a.srt", "/video/movie.en.srt")
        player._store_subtitle_copy("/tmp/b.srt", "/video/movie.en.srt")

    backups = [p for p in seen if str(p).endswith(".osbak")]
    assert len(backups) == 2
    assert backups[0] != backups[1], "two workers shared one backup path"


# --- account_state.py:67 - read-modify-write raced a full snapshot ----------

def test_state_merge_holds_an_exclusive_lock(tmp_path):
    from resources.lib import account_state
    state_file = tmp_path / "account_state.json"
    held = []

    with patch.object(account_state, "_state_path", return_value=str(state_file)):
        real_write = account_state._write_account_state

        def write_spy(state):
            held.append(os.path.exists(str(state_file) + ".lock"))
            return real_write(state)

        with patch.object(account_state, "_write_account_state", side_effect=write_spy):
            account_state.update_account_state({"account_details": "Quota: 3 downloads left today"})

    assert held == [True], "the merge wrote without holding the lock"
    assert not os.path.exists(str(state_file) + ".lock"), "lock file left behind"


def test_stale_lock_does_not_wedge_updates(tmp_path):
    from resources.lib import account_state
    state_file = tmp_path / "account_state.json"
    lock = tmp_path / "account_state.json.lock"
    lock.write_text("")
    os.utime(str(lock), (time.time() - 120,) * 2)      # older than STALE_AFTER

    with patch.object(account_state, "_state_path", return_value=str(state_file)):
        account_state.update_account_state({"account_status": "OK (VIP)"})
        assert account_state.load_account_state()["account_status"] == "OK (VIP)"


def test_live_lock_is_waited_for_then_gives_up_without_losing_the_write(tmp_path):
    """A fresh foreign lock must not raise or drop the update - worst case the
    merge proceeds unlocked, which is the pre-lock behaviour."""
    from resources.lib import account_state
    state_file = tmp_path / "account_state.json"
    lock = tmp_path / "account_state.json.lock"
    lock.write_text("")                                 # fresh: not stale

    with patch.object(account_state, "_state_path", return_value=str(state_file)):
        with patch.object(account_state._StateLock, "TIMEOUT", 0.1):
            account_state.update_account_state({"account_status": "OK (Free User)"})
    assert account_state.load_account_state.__name__ == "load_account_state"
    with patch.object(account_state, "_state_path", return_value=str(state_file)):
        assert account_state.load_account_state()["account_status"] == "OK (Free User)"
