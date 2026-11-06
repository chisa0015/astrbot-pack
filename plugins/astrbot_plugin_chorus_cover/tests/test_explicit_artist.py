"""用户点名歌手时，即使那条是翻唱也要保留——那是他要的版本。"""

import importlib.util
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PACKAGE = "choruscover_explicit_test"
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


def test_named_artist_cover_is_kept():
    songs = [
        candidate(1, "小幸运 (翻唱)", "某UP"),
        candidate(2, "小幸运", "田馥甄"),
    ]
    picked, status, _stats = pick_best_song(songs, "小幸运", "某UP")
    assert status == "ok"
    assert picked is not None
    assert picked.artists == "某UP"


def test_named_artist_cover_kept_even_with_original_present():
    songs = [
        candidate(1, "后来 (原唱 刘若英)", "翻唱歌手"),
        candidate(2, "后来", "刘若英"),
    ]
    picked, status, _stats = pick_best_song(songs, "后来", "翻唱歌手")
    assert status == "ok"
    assert picked is not None
    assert picked.artists == "翻唱歌手"


def test_random_cover_still_rejected_without_artist():
    songs = [
        candidate(1, "小幸运 (翻唱)", "某UP"),
        candidate(2, "小幸运", "田馥甄"),
    ]
    picked, status, _stats = pick_best_song(songs, "小幸运")
    assert status == "ok"
    assert picked is not None
    assert picked.artists == "田馥甄"
