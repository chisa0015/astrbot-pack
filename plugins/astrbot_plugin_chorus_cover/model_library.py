"""分类音色库的扫描与落位（0.14.0）。

把「库里的模型」变成「RVC 能看到的扁平文件」：RVC 只扫 assets\\weights 这一层，
所以真实文件留在 中文/角色/ 这种树里，assets 里放同名硬链接。

这里只放纯逻辑（扫描、判断要不要建链接），不依赖 AstrBot，方便写测试。
"""

from __future__ import annotations

import os
import shutil
from typing import Dict, List


def scan_model_library(root: str) -> List[Dict[str, str]]:
    """扫库根下的 .pth / .index，返回带语言和角色信息的列表。

    约定：``库根/语言/角色/文件``；少一层也能认（直接 ``库根/语言/文件`` 时角色取文件名）。
    """
    found: List[Dict[str, str]] = []
    if not root or not os.path.isdir(root):
        return found
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            lower = name.lower()
            if not (lower.endswith(".pth") or lower.endswith(".index")):
                continue
            full = os.path.join(dirpath, name)
            parts = os.path.relpath(full, root).split(os.sep)
            language = parts[0] if len(parts) > 1 else "未分类"
            role = parts[1] if len(parts) > 2 else os.path.splitext(parts[-1])[0]
            found.append({
                "path": full,
                "name": name,
                "language": language,
                "role": role,
            })
    return found


def link_into_assets(source: str, target: str) -> str:
    """把库里的文件落到 assets：同盘硬链接，跨盘复制，已存在同大小就跳过。

    返回 ``linked`` / ``copied`` / ``skipped``，调用方拿它做统计。
    """
    if os.path.exists(target):
        try:
            if os.path.getsize(target) == os.path.getsize(source):
                return "skipped"
        except OSError:
            pass
        os.remove(target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        os.link(source, target)
        return "linked"
    except OSError:
        shutil.copy2(source, target)
        return "copied"


def index_name_matches_model(index_name: str, model_names: List[str]) -> bool:
    """判断索引文件名里有没有对应模型名（RVC 靠这个名字模糊匹配）。"""
    stem = os.path.splitext(index_name)[0].lower()
    for model_name in model_names:
        model_stem = os.path.splitext(model_name)[0].lower()
        if model_stem in stem or stem in model_stem:
            return True
    return False
