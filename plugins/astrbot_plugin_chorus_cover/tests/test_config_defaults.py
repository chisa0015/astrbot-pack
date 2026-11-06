"""代码默认值必须和 _conf_schema.json 的默认值一致。

0.15.3 之前踩过：schema 把 male_to_female_shift 默认改成 5，代码里还是 12，
老配置缺这个键时会直接拔高一个八度。这个测试把两者钉在一起，避免再漂。
"""

import io
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 这两个用空串表示「走内置默认地址」，和 schema 里的展示值不同，属于有意为之
SENTINEL_KEYS = {"netease_meting_base", "netease_api_base"}


def _load_main():
    with io.open(os.path.join(ROOT, "main.py"), encoding="utf-8") as handle:
        return handle.read()


def _schema():
    with io.open(os.path.join(ROOT, "_conf_schema.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _normalize(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            return text
    if isinstance(value, list):
        return tuple(value)
    return value


def test_code_defaults_match_schema():
    text = _load_main()
    schema = _schema()
    problems = []

    for key, item in schema.items():
        if not isinstance(item, dict) or key in SENTINEL_KEYS:
            continue
        pattern = re.compile(
            r'config\.get\(\s*["\']' + re.escape(key) + r'["\']\s*,\s*([^\)]+)\)'
        )
        for raw in pattern.findall(text):
            raw = raw.strip()
            try:
                code_default = eval(raw, {"__builtins__": {}}, {})
            except Exception:
                continue
            if _normalize(code_default) != _normalize(item.get("default")):
                problems.append((key, raw, item.get("default")))

    assert not problems, "代码默认值与 schema 不一致: %r" % (problems,)


def test_float_defaults_match_schema():
    """_bounded_float 的默认值也要对得上（比如 index_rate）。"""
    text = _load_main()
    schema = _schema()
    problems = []
    pattern = re.compile(
        r'_bounded_float\(\s*config,\s*["\'](\w+)["\']\s*,\s*([0-9.]+)'
    )
    for key, raw in pattern.findall(text):
        item = schema.get(key, {})
        if _normalize(float(raw)) != _normalize(item.get("default")):
            problems.append((key, raw, item.get("default")))
    assert not problems, "浮点默认值不一致: %r" % (problems,)
