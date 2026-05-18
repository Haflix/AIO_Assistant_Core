"""Streaming throughput perf fixture.

Yields N chunks of M bytes for an execute_stream benchmark. Used to
gauge whether Plexus's streaming machinery can carry real-time audio
(audio @ 48kHz stereo PCM = 192 KB/s = ~52 chunks/s of 3840 bytes).
"""

from plexus.utils import Plugin
from plexus.decorators import log_errors, async_log_errors, async_gen_log_errors


class StreamProducer(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass

    @async_gen_log_errors
    async def produce_chunks(self, count: int, chunk_size: int):
        chunk = b"\x00" * chunk_size
        for _ in range(count):
            yield chunk
