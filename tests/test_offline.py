"""Offline unit tests for dashrip.

These cover the pure, deterministic parts of the pipeline -- manifest
parsing, key-tier aliasing, subtitle conversion, title matching, episode
selection, cookie handling, and the output layout -- so they run with no
network, no device file, and no media.  Run from the repository root:

    python -m pytest -q
"""

import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashrip import cli
from dashrip import config as C
from dashrip import core as K
from dashrip import structure as S

# ------------------------- output layout -------------------------

def test_safe_dir_strips_invalid_characters():
    assert S.safe_dir("Rick and Morty: The Anime") == "Rick and Morty The Anime"
    assert S.safe_dir("") == "untitled"


def test_slug_is_dotted():
    assert S.slug("The Matrix") == "The.Matrix"


def test_year_of():
    assert S.year_of({"premiereDate": "1999-03-31T00:00:00Z"}) == "1999"
    assert S.year_of({}) == ""


def test_final_name_movie_and_episode():
    assert S.final_name("The Matrix", {"season": None}, False) == "The.Matrix.720p.mkv"
    ep = {"season": 2, "episode": 3, "name": "Chapter Three"}
    assert S.final_name("Show", ep, False) == "S02E03 - Chapter Three.720p.mkv"
    # single-season series are flattened to season 01
    assert S.final_name("Show", ep, True) == "S01E03 - Chapter Three.720p.mkv"


def test_title_dir_movie_uses_year():
    d = S.title_dir("/out", "The Matrix", {"premiereDate": "1999-03-31"}, False)
    assert d.replace("\\", "/") == "/out/Movies/The Matrix (1999)"


def test_final_path_episode():
    p = S.final_path(
        "/out", "Show",
        {"season": 1, "episode": 2, "name": "Pilot"}, {}, True)
    assert p.replace("\\", "/").endswith(
        "/out/Series/Show/Season 01/Episode 02 - Pilot/S01E02 - Pilot.720p.mkv")


# ------------------------- config: cookies -------------------------

def test_cookie_token_variants():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9.sig"
    assert C.cookie_token("st=" + jwt) == "st=" + jwt
    assert C.cookie_token(jwt) == "st=" + jwt
    assert C.cookie_token("a=1; st=" + jwt + "; b=2") == "st=" + jwt
    assert C.cookie_token("not-a-token") == ""


def test_cookie_expiry_reads_exp():
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": 2000000000}).encode()).decode().rstrip("=")
    token = f"st=eyJhbGciOiJIUzI1NiJ9.{payload}.sig"
    assert C.cookie_expiry(token) == 2000000000


def test_config_get_set_roundtrip():
    cfg = {}
    C.set(cfg, "playback.url", "https://example")
    assert C.get(cfg, "playback.url") == "https://example"
    assert C.get(cfg, "playback.missing", "fallback") == "fallback"


# ------------------------- core: pure helpers -------------------------

def test_xsd_duration():
    assert K._xsd_duration("PT1H2M3.5S") == 3723.5
    assert K._xsd_duration("PT22M18.647S") == pytest.approx(1338.647)
    assert K._xsd_duration("garbage") == 0.0


def test_resolve_original_lang():
    assert K.resolve_original_lang(["en", "de"], "de") == "de"
    assert K.resolve_original_lang(["en", "de"]) == "en"
    assert K.resolve_original_lang(["en"]) == "en"
    assert K.resolve_original_lang([]) is None
    # the platform hint may carry a region separator of either kind
    assert K.resolve_original_lang(["en-US", "de-DE"], "de_DE") == "de-DE"


def test_ttml_timestamp_helpers():
    assert K._ttml_ts_to_ms("00:00:01.500") == 1500
    assert K._ttml_ts_to_ms("PT1H2M3.4S") == 3723400
    assert K._ms_to_srt_ts(3723400) == "01:02:03,400"


def test_ttml_to_srt_converts_cues():
    ttml = (
        "<tt><body><div>"
        '<p begin="00:00:01.000" end="00:00:03.000">'
        "Hello <span>world</span><br/>line 2</p>"
        '<p begin="00:00:04.000" dur="00:00:02.000">A &amp; B</p>'
        "</div></body></tt>")
    srt = K.ttml_to_srt(ttml)
    assert "1\n00:00:01,000 --> 00:00:03,000\nHello world\nline 2" in srt
    assert "A & B" in srt
    assert "00:00:06,000" in srt  # begin 4s + dur 2s


def test_keys_arg_aliases_every_tier():
    kid = "0100" + "ab" * 14
    key = "cd" * 16
    arg = K.keys_arg({kid: key})
    for tier in ("0100", "0102", "0105"):
        assert "key_id={}{}".format(tier, "ab" * 14) in arg
    # all-zero keys are the "no key for this tier" marker and are dropped
    assert K.keys_arg({kid: "0" * 32}) == ""


def test_merge_subs_renumbers_cues(tmp_path):
    a = tmp_path / "a.vtt"
    a.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n",
                 encoding="utf-8")
    b = tmp_path / "b.vtt"
    b.write_text("WEBVTT\n\n3\n00:00:03.000 --> 00:00:04.000\nWorld\n",
                 encoding="utf-8")
    out = K.merge_subs(None, [(str(a), "x"), (str(b), "y")])
    assert out["x"].startswith("WEBVTT")
    assert "Hello" in out["x"]
    assert "World" in out["y"]


MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT1H0M0S">
  <Period>
    <AdaptationSet contentType="video" mimeType="video/mp4">
      <Representation id="v720" width="1280" height="720">
        <BaseURL>video_720.mp4</BaseURL>
      </Representation>
      <Representation id="v1080" width="1920" height="1080">
        <BaseURL>video_1080.mp4</BaseURL>
      </Representation>
    </AdaptationSet>
    <AdaptationSet contentType="audio" lang="en-US">
      <Representation id="a1"><BaseURL>audio_en.mp4</BaseURL></Representation>
    </AdaptationSet>
    <AdaptationSet contentType="audio" lang="de-DE">
      <Representation id="a2"><BaseURL>audio_de.mp4</BaseURL></Representation>
    </AdaptationSet>
    <AdaptationSet contentType="text" lang="en-US">
      <Representation id="s1">
        <SegmentTemplate media="sub_en_$Number$.vtt" startNumber="1"/>
      </Representation>
    </AdaptationSet>
    <ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed">
      <cenc:pssh xmlns:cenc="urn:mpeg:cenc:2013">AAAA</cenc:pssh>
    </ContentProtection>
  </Period>
</MPD>"""


def test_parse_mpd_selects_best_video_and_all_tracks():
    p = K.parse_mpd(MPD)
    assert p["video_wh"] == "1920x1080"
    assert set(p["audio"].keys()) == {"en-US", "de-DE"}
    assert [t["lang"] for t in p["subs"]] == ["en-US"]
    assert p["duration"] == 3600.0
    assert p["pssh"] == base64.b64decode("AAAA")


# ------------------------- cli: matching and selection -------------------------

def test_norm_folds_accents_and_case():
    assert cli.norm("Interstellar") == "interstellar"
    assert cli.norm("Amelie") == cli.norm("Am\u00e9lie")
    assert cli.norm("\u00c7\u0131\u011f\u00f6\u015f\u00fc") == "cigosu"


def test_query_text_extracts_last_url_segment():
    assert cli._query_text("https://play.example.com/show/abc-def") == "abc-def"
    assert cli._query_text("The Matrix") == "The Matrix"


def test_parse_ep_spec():
    one_season = [
        {"season": 1, "episode": 1, "editId": "a"},
        {"season": 1, "episode": 2, "editId": "b"},
        {"season": 1, "episode": 3, "editId": "c"},
    ]
    assert cli.parse_ep_spec("", one_season) == one_season
    assert [v["editId"] for v in cli.parse_ep_spec("1-2", one_season)] == ["a", "b"]
    assert [v["editId"] for v in cli.parse_ep_spec("3", one_season)] == ["c"]

    two_seasons = [*one_season, {"season": 2, "episode": 1, "editId": "d"}]
    assert [v["editId"] for v in cli.parse_ep_spec("S02E01", two_seasons)] == ["d"]


def test_fmt_airdate():
    assert cli.fmt_airdate("2024-08-15T00:01:00Z") == "15 Aug 2024"
    assert cli.fmt_airdate("garbage") == ""


def test_resolve_audio_policy():
    avail = ["en-US", "de-DE"]
    assert cli._resolve_audio_policy(["orig", "de-DE"], avail, "en") == \
        ["en-US", "de-DE"]
    assert cli._resolve_audio_policy("all", avail) is None
    assert cli._resolve_audio_policy("none", avail) == []
