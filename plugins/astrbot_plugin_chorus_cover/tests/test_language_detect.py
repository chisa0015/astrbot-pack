import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import language_detect as langdet  # noqa: E402


def test_score_three_languages():
    zh = "雨落下的声音 打在窗台 我还记得那天你说的话"
    ja = "夜に駆ける 君の声が聞こえた そんな気がした"
    en = "the club isn't the best place to find a lover so the bar is where I go"

    assert langdet._score(zh)[0] == "zh"
    assert langdet._score(ja)[0] == "ja"
    assert langdet._score(en)[0] == "en"


def test_score_english_with_stray_chinese():
    # 英文歌词里偶尔夹几个中文译名，不能因此判成中文
    text = "hello world " * 40 + "晴天"
    assert langdet._score(text)[0] == "en"


def test_score_unknown_and_instrumental():
    assert langdet._score("")[0] == ""
    assert langdet._score("纯音乐，请欣赏")[0] == ""


def test_strip_lyric_removes_timestamps_and_credits():
    raw = "[00:01.00]作词：某某\n[00:02.00]雨落下的声音\n[00:03.00]打在窗台"
    cleaned = langdet._strip_lyric(raw)
    assert "00:01" not in cleaned
    assert "作词" not in cleaned
    assert "雨落下的声音" in cleaned


def test_detect_language_uses_cache(tmp_path, monkeypatch):
    cache = tmp_path / "language_cache.json"
    calls = {"count": 0}

    def fake_fetch(song_id, bases, timeout):
        calls["count"] += 1
        return "夜に駆ける 君の声が聞こえた そんな気がした"

    monkeypatch.setattr(langdet, "_fetch_lyric", fake_fetch)
    song = {"source": "netease", "id": 1, "name": "夜に駆ける", "artists": "YOASOBI"}

    lang, evidence = asyncio.run(langdet.detect_language(song, str(cache), ["http://127.0.0.1:3003"]))
    assert lang == "ja"
    assert evidence.startswith("lyrics(")
    assert calls["count"] == 1
    assert json.loads(cache.read_text(encoding="utf-8"))["netease:1"]["lang"] == "ja"

    # 第二次应该直接读缓存，不再请求歌词
    monkeypatch.setattr(langdet, "_fetch_lyric", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该再请求")))
    lang2, _ = asyncio.run(langdet.detect_language(song, str(cache), ["http://127.0.0.1:3003"]))
    assert lang2 == "ja"


def test_detect_language_falls_back_to_title(tmp_path, monkeypatch):
    cache = tmp_path / "language_cache.json"
    monkeypatch.setattr(langdet, "_fetch_lyric", lambda *a, **k: "")
    song = {"source": "kugou", "id": "x", "name": "夜に駆ける", "artists": "YOASOBI"}
    lang, evidence = asyncio.run(langdet.detect_language(song, str(cache), []))
    assert lang == "ja"
    assert evidence.startswith("title(")
