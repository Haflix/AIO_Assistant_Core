"""TestRemoteTarget — Phase 5 fixture (remote=True)."""

import asyncio
from typing import Any

from utils import Plugin
from decorators import async_gen_log_errors, async_log_errors, log_errors


class TestRemoteTarget(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("TestRemoteTarget.on_enable")

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("TestRemoteTarget.on_disable")

    @async_log_errors
    async def r_open(self, value: Any = None) -> Any:
        return value

    @async_log_errors
    async def r_remote_only(self, value: Any = None) -> Any:
        return value

    @async_gen_log_errors
    async def r_async_gen(self, n: int = 3):
        for i in range(max(0, int(n))):
            yield f"r_{i}"

    @async_gen_log_errors
    async def r_async_gen_huge_item(self, size_mb: int = 101):
        size_bytes = max(1, int(size_mb)) * 1024 * 1024
        yield {"data": b"\xab" * size_bytes}

    @async_gen_log_errors
    async def r_async_gen_raises_after(self, n_yielded: int = 5):
        for i in range(max(0, int(n_yielded))):
            yield f"r_{i}"
        raise ValueError("midstream")

    @async_log_errors
    async def r_hang(self) -> str:
        await asyncio.Event().wait()
        return "did_not_hang"

    @async_log_errors
    async def r_topic_open(self, payload: Any = None) -> Any:
        return {"received": payload}
