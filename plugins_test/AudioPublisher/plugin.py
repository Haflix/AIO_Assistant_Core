"""publish_event throughput perf fixture — publisher side.

Bursts N publish_event calls with audio-sized payloads. Models the
DiscordBot voice sink's per-frame publish behavior: each captured 20ms
PCM frame would publish one audio_chunk event for any subscribed
plugin (STT, VAD, recorder, ...) to consume.
"""

import time
from plexus.utils import Plugin
from plexus.decorators import log_errors, async_log_errors


class AudioPublisher(Plugin):
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
    async def burst(self, count: int, chunk_size: int):
        per_publish_us = []
        subscriber_counts = []
        chunk_template = b"\x00" * chunk_size
        wall_t0 = time.perf_counter()

        for seq in range(count):
            t_publish = time.perf_counter()
            n_subs = await self.publish_event(
                "audio_chunk",
                payload={
                    "seq": seq,
                    "user_id": 1234567890,        # simulated speaker id
                    "pcm": chunk_template,
                    "t_publish_us": t_publish * 1_000_000,
                },
            )
            per_publish_us.append((time.perf_counter() - t_publish) * 1_000_000)
            subscriber_counts.append(n_subs)

        wall_s = time.perf_counter() - wall_t0
        return {
            "wall_s": wall_s,
            "publish_count": count,
            "per_publish_us": per_publish_us,
            "min_subs_seen": min(subscriber_counts) if subscriber_counts else 0,
            "max_subs_seen": max(subscriber_counts) if subscriber_counts else 0,
        }
