"""测试用的最小环境：不装 AstrBot 也能 import 插件里的纯逻辑模块。"""

import logging
import sys
import types

if "astrbot" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("chorus-cover-test")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

# aiohttp 只在真正联网时才需要；纯逻辑测试装个占位模块即可
try:  # pragma: no cover
    import aiohttp  # noqa: F401
except ImportError:  # pragma: no cover
    aiohttp_stub = types.ModuleType("aiohttp")

    class _ClientTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class _ClientSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *args, **kwargs):
            raise RuntimeError("测试环境不联网")

        async def post(self, *args, **kwargs):
            raise RuntimeError("测试环境不联网")

    aiohttp_stub.ClientTimeout = _ClientTimeout
    aiohttp_stub.ClientSession = _ClientSession
    sys.modules["aiohttp"] = aiohttp_stub
