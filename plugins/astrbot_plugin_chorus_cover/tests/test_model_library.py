import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model_library as ml  # noqa: E402


def _touch(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)


def test_scan_model_library(tmp_path):
    root = tmp_path / "RVC"
    _touch(str(root / "中文" / "安可" / "安可CN.pth"))
    _touch(str(root / "中文" / "安可" / "安可CN.index"))
    _touch(str(root / "日语" / "Yuuka" / "Yuuka.pth"))
    _touch(str(root / "未分类" / "Kiki" / "kikiV1.pth"))
    _touch(str(root / "说明.txt"))

    found = ml.scan_model_library(str(root))
    names = {item["name"] for item in found}
    assert names == {"安可CN.pth", "安可CN.index", "Yuuka.pth", "kikiV1.pth"}

    ank = next(item for item in found if item["name"] == "安可CN.pth")
    assert ank["language"] == "中文"
    assert ank["role"] == "安可"


def test_scan_missing_root(tmp_path):
    assert ml.scan_model_library(str(tmp_path / "不存在")) == []


def test_link_into_assets_linked_then_skipped(tmp_path):
    source = tmp_path / "lib" / "a.pth"
    _touch(str(source), b"model-bytes")
    target = tmp_path / "assets" / "a.pth"

    first = ml.link_into_assets(str(source), str(target))
    assert first in ("linked", "copied")
    assert target.read_bytes() == b"model-bytes"

    second = ml.link_into_assets(str(source), str(target))
    assert second == "skipped"


def test_index_name_matches_model():
    models = ["安可CN.pth", "Yuuka.pth"]
    assert ml.index_name_matches_model("安可CN.index", models)
    assert ml.index_name_matches_model("added_IVF936_Flat_nprobe_1_Yuuka_v2.index", models)
    assert not ml.index_name_matches_model("model.index", models)
