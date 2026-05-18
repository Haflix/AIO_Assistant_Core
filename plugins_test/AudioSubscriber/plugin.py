"""publish_event throughput perf fixture — subscriber side.

Subscribes to AudioPublisher/audio/chunk. Records receive timestamps
so the bench can compute publish-to-receive latency and inter-chunk
jitter.
"""

import time
from plexus.utils import Plugin
from plexus.decorators import log_errors, async_log_errors


class AudioSubscriber(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        self._log = []   # list[(seq, t_publish_us, t_receive_us)]

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass

    @async_log_errors
    async def handle_chunk(self, event):
        t_recv_us = time.perf_counter() * 1_000_000
        p = event.payload or {}
        self._log.append((
            p.get("seq"),
            p.get("t_publish_us"),
            t_recv_us,
        ))

    @async_log_errors
    async def reset(self):
        self._log.clear()
        return {"cleared": True}

    @async_log_errors
    async def collect(self):
        latencies_us = []
        delta_us = []
        last_recv = None
        for seq, t_pub, t_recv in self._log:
            if t_pub is not None and t_recv is not None:
                latencies_us.append(t_recv - t_pub)
            if last_recv is not None:
                delta_us.append(t_recv - last_recv)
            last_recv = t_recv
        return {
            "received_count": len(self._log),
            "latencies_us": latencies_us,
            "delta_us": delta_us,
        }
