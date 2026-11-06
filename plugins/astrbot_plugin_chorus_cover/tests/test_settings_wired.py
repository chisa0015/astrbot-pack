"""把设置项和源码钉在一起：schema 里有、源码里也必须读它。

这条测试防的是「假设置」——设置页能看到、改了却没有代码消费。
每加一个配置项都要在源码里出现一次，否则测试失败。
"""

import io
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = os.path.join(ROOT, "_conf_schema.json")
SOURCES = [
    "main.py",
    "source_router.py",
    "outbound.py",
    "selection.py",
    "netease_fetch.py",
    "model_library.py",
    "language_detect.py",
    "intent_gate.py",
    "qq_callbacks.py",
]

# 这些键由点歌插件的配置或运行时状态决定，插件自身不读，属于有意为之
EXTERNAL_KEYS = set()


def _schema_keys():
    with io.open(SCHEMA, encoding="utf-8") as handle:
        return [k for k, v in json.load(handle).items() if isinstance(v, dict)]


def _source_text():
    parts = []
    for name in SOURCES:
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            with io.open(path, encoding="utf-8") as handle:
                parts.append(handle.read())
    return "\n".join(parts)


def test_every_setting_is_read_by_code():
    text = _source_text()
    missing = [k for k in _schema_keys() if k not in EXTERNAL_KEYS and f'"{k}"' not in text]
    assert not missing, "这些设置没有任何代码读取: %r" % (missing,)


def test_settings_doc_covers_all_keys():
    doc = os.path.join(ROOT, "SETTINGS.md")
    assert os.path.isfile(doc), "缺少 SETTINGS.md，请重新生成设置清单"
    with io.open(doc, encoding="utf-8") as handle:
        content = handle.read()
    missing = [k for k in _schema_keys() if f"`{k}`" not in content]
    assert not missing, "SETTINGS.md 里缺这些设置: %r" % (missing,)


def test_schema_defaults_are_serialisable():
    with io.open(SCHEMA, encoding="utf-8") as handle:
        schema = json.load(handle)
    for key, item in schema.items():
        if not isinstance(item, dict):
            continue
        assert "default" in item, "%s 没有默认值" % key
        assert item.get("hint"), "%s 没有中文说明" % key

def test_only_string_settings_have_options():
    """下拉选项只属于字符串类型；给布尔加 options 会让设置页显示空下拉。"""
    with io.open(SCHEMA, encoding="utf-8") as handle:
        schema = json.load(handle)
    bad = [
        key for key, item in schema.items()
        if isinstance(item, dict) and item.get("type") != "string" and "options" in item
    ]
    assert not bad, "这些非字符串设置带了 options: %r" % (bad,)
