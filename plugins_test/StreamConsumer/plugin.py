"""Streaming throughput perf fixture — consumer side.

Calls self.execute_stream against StreamProducer (which may live on the
same node or a remote peer) and returns timing data. Lets the bench
measure cross-plugin + cross-node overhead via the real Plugin wrapper
(vs the Plexus-level execute_stream call used in the local bench).
"""

import time
from plexus.utils import Plugin
from plexus.decorators import log_errors, async_log_errors


class StreamConsumer(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass

    @async_log_errors
    async def drain_local(self, count: int, chunk_size: int, hosts: str = "any"):
        per_chunk_us = []
        received_chunks = 0
        received_bytes = 0
        last = time.perf_counter()
        t0 = last

        async for chunk in self.execute_stream(
            "StreamProducer",
            "produce_chunks",
            args={"count": count, "chunk_size": chunk_size},
            hosts=hosts,
        ):
            now = time.perf_counter()
            per_chunk_us.append((now - last) * 1_000_000)
            last = now
            received_bytes += len(chunk)
            received_chunks += 1

        wall_s = time.perf_counter() - t0
        return {
            "wall_s": wall_s,
            "received_chunks": received_chunks,
            "received_bytes": received_bytes,
            "per_chunk_us": per_chunk_us,
        }
