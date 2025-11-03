from utils import Plugin
from decorators import log_errors, async_log_errors
import asyncio


class NetTest(Plugin):
    """Networking test plugin to validate remote execute and streaming."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self._logger.debug("NetTest.on_load")

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("NetTest.on_enable")

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("NetTest.on_disable")

    # ----- Execute endpoints -----

    @async_log_errors
    async def echo(self, value):
        """Return the provided value as-is (tests arbitrary picklable objects)."""
        return value

    @async_log_errors
    async def big_object(self, size_mb: int = 8):
        """Return a large object (~size_mb MiB) to test chunking over sockets."""
        size_bytes = max(1, int(size_mb)) * 1024 * 1024
        blob = bytes([0xAB]) * size_bytes
        # Wrap in a dict to ensure pickling of a structured object
        return {"kind": "big_object", "size": size_bytes, "data": blob}

    # ----- Streaming endpoints -----

    @async_log_errors
    async def stream_count(self, n: int = 10, delay_ms: int = 50):
        """Yield integers 0..n-1 with a small delay to test streaming."""
        delay = max(0, int(delay_ms)) / 1000.0
        for i in range(max(0, int(n))):
            await asyncio.sleep(delay)
            yield i

    @async_log_errors
    async def stream_big(self, chunks: int = 3, size_mb: int = 2, delay_ms: int = 50):
        """Yield a few large blobs to test chunked streaming of big items."""
        chunks = max(1, int(chunks))
        size_bytes = max(1, int(size_mb)) * 1024 * 1024
        delay = max(0, int(delay_ms)) / 1000.0
        for idx in range(chunks):
            await asyncio.sleep(delay)
            yield {"idx": idx, "data": bytes([0xCD]) * size_bytes}


