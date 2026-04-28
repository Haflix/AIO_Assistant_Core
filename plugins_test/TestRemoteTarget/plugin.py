"""TestRemoteTarget — Phase 5 fixture (remote=True)."""

import asyncio
from typing import Any

from utils import Plugin
from decorators import async_gen_log_errors, async_log_errors, log_errors


class TestRemoteTarget(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        self._hang_sub_id = None

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("TestRemoteTarget.on_enable")
        # Code-driven hang sub on test/r/hang — B-020 driver. Registered as
        # code-driven so notify dispatch goes through _call_sub's direct-
        # handler path (no execute round-trip).
        self._hang_sub_id = await self._plugin_core.subscribe(
            "test/r/hang",
            self.plugin_name,
            self.plugin_uuid,
            handler=self.r_hang_topic_handler,
        )

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("TestRemoteTarget.on_disable")
        if self._hang_sub_id:
            try:
                await self._plugin_core.unsubscribe(self._hang_sub_id)
            except Exception:
                pass
            self._hang_sub_id = None

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

    @async_gen_log_errors
    async def r_topic_huge_stream(self):
        # B-024 driver. _handle_topic_request_stream emits ONE
        # MSG_STREAM_CHUNK per yielded item with chunk_length = pickled-size + 1.
        # If chunk_length exceeds MAX_MESSAGE_SIZE (100 MB on the receiver),
        # the receiver raises NetworkRequestException at networking.py:1639
        # and the entire stream is killed. _handle_execute_stream splits;
        # _handle_topic_request_stream does NOT — that's the bug.
        # 101 MB triggers the rejection.
        yield {"data": b"\xab" * (101 * 1024 * 1024)}

    @async_log_errors
    async def r_hang_topic_handler(self, *args, **kwargs):
        # B-020 driver: subscribed via on_enable to test/r/hang. notify_sync
        # against this from a SYNC context should block until this returns
        # (which is never, until cancelled).
        await asyncio.Event().wait()
