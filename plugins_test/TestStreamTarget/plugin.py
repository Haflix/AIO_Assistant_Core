"""TestStreamTarget — Phase 2 fixture for TestStreamSuite.

Provides predictable async/sync generator endpoints. Tier-1.
"""

import asyncio
import time
from typing import Any

from utils import Plugin
from decorators import (
    async_gen_log_errors,
    async_log_errors,
    gen_log_errors,
    log_errors,
)


class TestStreamTarget(Plugin):
    """Predictable generator fixtures for stream-suite cases."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("TestStreamTarget.on_enable")

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("TestStreamTarget.on_disable")

    # ----- Standard generators -----

    @async_gen_log_errors
    async def ea_gen(self, n: int = 5, prefix: str = "x", delay_ms: int = 0):
        n = max(0, int(n))
        delay = max(0, int(delay_ms)) / 1000.0
        for i in range(n):
            if delay:
                await asyncio.sleep(delay)
            yield f"{prefix}{i}"

    @gen_log_errors
    def es_gen(self, n: int = 5, prefix: str = "x", delay_ms: int = 0):
        n = max(0, int(n))
        delay = max(0, int(delay_ms)) / 1000.0
        for i in range(n):
            if delay:
                time.sleep(delay)
            yield f"{prefix}{i}"

    # ----- Error generator -----

    @async_gen_log_errors
    async def ea_gen_raises_after(self, n_yielded: int = 2):
        for i in range(max(0, int(n_yielded))):
            yield f"item_{i}"
        raise ValueError("midstream")

    # ----- Infinite generator (B-002 abandonment) -----

    @async_gen_log_errors
    async def ea_gen_infinite(self):
        i = 0
        while True:
            yield i
            i += 1
            await asyncio.sleep(0)

    # ----- Edge generators -----

    @async_gen_log_errors
    async def ea_gen_one_item(self):
        yield "only"

    @async_gen_log_errors
    async def ea_gen_empty(self):
        if False:
            yield  # makes this an async-gen function

    @gen_log_errors
    def es_gen_with_delay(self, n: int = 3, delay_ms: int = 20):
        n = max(0, int(n))
        delay = max(0, int(delay_ms)) / 1000.0
        for i in range(n):
            if delay:
                time.sleep(delay)
            yield i

    # ----- Large payload -----

    @async_gen_log_errors
    async def ea_gen_returns_one_large_item(self, size_bytes: int = 100_000):
        yield b"\xab" * max(0, int(size_bytes))

    # ----- Hanging generator (stream timeout test) -----

    @async_gen_log_errors
    async def ea_gen_hangs(self):
        await asyncio.Event().wait()
        yield "never"  # unreachable
