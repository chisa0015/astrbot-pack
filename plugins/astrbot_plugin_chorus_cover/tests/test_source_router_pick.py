"""选歌规则：必须优先原唱，不能拿翻唱顶替。

背景：网易云搜索「晴天 周杰伦」排在最前的是《晴天(深情版)》《晴天 (原唱 周杰伦)》
这类翻唱；旧逻辑按搜索顺序取第一条，于是下载到的全是翻唱。
"""

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PACKAGE = "choruscover_under_test"
if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [ROOT]
    sys.modules[_PACKAGE] = package

_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE + ".source_router", os.path.join(ROOT, "source_router.py")
)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

SongCandidate = _MODULE.SongCandidate
pick_best_song = _MODULE.pick_best_song


def candidate(song_id, name, artists, alias=None, duration=250000):
    return SongCandidate(
        id=str(song_id),
        name=name,
        artists=artists,
        duration_ms=duration,
        alias=alias or "",
        source="netease",
    )


def test_exact_title_beats_cover_versions():
    songs = [
        candidate(1, "晴天(深情版)", "Lucky小爱"),
        candidate(2, "晴天 (原唱 周杰伦)", "RyaVocal"),
        candidate(3, "晴天 (女声版)", "GYBeat"),
        candidate(4, "晴天", "周杰伦"),
    ]
    picked, status, _stats = pick_best_song(songs, "晴天", "周杰伦")
    assert status == "ok"
    assert picked is not None
    assert picked.artists == "周杰伦"
    assert picked.name == "晴天"


def test_covers_rejected_when_original_missing():
    songs = [
        candidate(1, "晴天(深情版)", "Lucky小爱"),
        candidate(2, "晴天 (原唱 周杰伦)", "RyaVocal"),
    ]
    picked, status, _stats = pick_best_song(songs, "晴天", "周杰伦")
    assert picked is None
    assert status == "no_match"


def test_requested_version_is_kept():
    songs = [
        candidate(1, "晴天", "周杰伦"),
        candidate(2, "晴天 (女声版)", "GYBeat"),
    ]
    picked, status, _stats = pick_best_song(songs, "晴天 女声版")
    assert status == "ok"
    assert picked is not None
    assert "女声版" in picked.name


def test_original_wins_even_without_artist_hint():
    songs = [
        candidate(1, "打上花火 (翻唱)", "某UP"),
        candidate(2, "打上花火", "DAOKO × 米津玄師"),
    ]
    picked, status, _stats = pick_best_song(songs, "打上花火")
    assert status == "ok"
    assert picked is not None
    assert picked.artists.startswith("DAOKO")

def test_preview_clip_loses_to_full_track():
    songs = [
        candidate(1, "晴天", "周杰伦", duration=30000),   # 30 秒试听
        candidate(2, "晴天", "周杰伦", duration=269000),  # 完整版
    ]
    picked, status, _stats = pick_best_song(songs, "晴天", "周杰伦")
    assert status == "ok"
    assert picked is not None
    assert picked.duration_ms == 269000


def test_instrumental_only_is_reported():
    songs = [candidate(1, "晴天 (伴奏)", "周杰伦")]
    picked, status, _stats = pick_best_song(songs, "晴天", "周杰伦")
    assert picked is None
    assert status == "only_accompaniment"


def test_explicit_version_requires_that_version():
    songs = [
        candidate(1, "晴天", "周杰伦"),
        candidate(2, "晴天 (Live)", "周杰伦"),
    ]
    picked, status, _stats = pick_best_song(songs, "晴天 Live", "周杰伦")
    assert status == "ok"
    assert picked is not None
    assert "Live" in picked.name
