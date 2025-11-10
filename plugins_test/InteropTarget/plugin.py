from utils import Plugin
from decorators import log_errors, async_log_errors
import asyncio
import time


class InteropTarget(Plugin):
    """Target plugin exposing sync/async functions and generators for interop tests."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self.plugin_name = "InteropTarget"
        self.version = "0.0.1"
        self.description = (
            "Exposes sync/async functions and generators for interop testing"
        )

    @async_log_errors
    async def on_enable(self):
        self.enabled = True
        self._logger.debug("InteropTarget.on_enable")

    @async_log_errors
    async def on_disable(self):
        self.enabled = False
        self._logger.debug("InteropTarget.on_disable")

    # ----- Value endpoints -----

    @log_errors
    def sync_add(self, a=None, b=1, **kwargs):
        """Synchronous add with support for positional/kw/single-arg. Returns a + b."""
        return (a if a is not None else 0) + (b if b is not None else 0)

    @async_log_errors
    async def async_add(self, a=None, b=1, **kwargs):
        """Async add mirroring sync_add."""
        return (a if a is not None else 0) + (b if b is not None else 0)

    # ----- Generator endpoints -----

    def sync_gen(self, n=3, prefix="g", delay_ms=10):
        """Synchronous generator yielding prefix+index strings optionally delayed by sleep."""
        n = max(0, int(n))
        delay_s = max(0, int(delay_ms)) / 1000.0
        for i in range(n):
            if delay_s:
                time.sleep(delay_s)
            yield f"{prefix}{i}"

    async def async_gen(self, n=3, prefix="ag", delay_ms=10):
        """Async generator yielding prefix+index strings with asyncio sleep."""
        n = max(0, int(n))
        delay_s = max(0, int(delay_ms)) / 1000.0
        for i in range(n):
            if delay_s:
                await asyncio.sleep(delay_s)
            yield f"{prefix}{i}"
