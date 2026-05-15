"""TUI smoke demo timer — fires demo events on a background tick so
the Events ▸ Live stream panel stays populated automatically (and
the Plugins-tab Phase column shows non-zero traffic flowing through
the framework even when nobody is clicking buttons)."""

import asyncio

from plexus.utils import Plugin
from plexus.decorators import async_log_errors, log_errors


class TUIDemoTimer(Plugin):
    @log_errors
    def on_load(self, tick_interval: float = 3.0, *args, **kwargs):
        self.description = (
            "TUI smoke demo timer — auto-fires demo events every "
            f"{tick_interval}s."
        )
        self._interval: float = float(tick_interval)
        self._tick_count: int = 0
        self._paused: bool = False
        self._task = None

    @async_log_errors
    async def on_enable(self):
        self._paused = False
        self._task = asyncio.create_task(self._heartbeat_loop())

    @async_log_errors
    async def on_disable(self):
        self._paused = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _heartbeat_loop(self):
        """Fire heartbeat + occasional burst events at the configured
        interval. Cancellation-safe."""
        try:
            while True:
                await asyncio.sleep(self._interval)
                if self._paused:
                    continue
                self._tick_count += 1
                try:
                    await self.publish_event(
                        "heartbeat",
                        payload={"tick": self._tick_count},
                    )
                    # Every 4th tick, fire a burst (3 events in quick
                    # succession) to demonstrate the deque + 100ms
                    # batched flush handling rapid emits.
                    if self._tick_count % 4 == 0:
                        for i in range(3):
                            await self.publish_event(
                                "burst",
                                payload={"tick": self._tick_count, "i": i},
                            )
                except Exception:
                    # Don't let one publish failure kill the loop.
                    pass
        except asyncio.CancelledError:
            return

    @async_log_errors
    async def status(self):
        return {
            "interval": self._interval,
            "tick_count": self._tick_count,
            "paused": self._paused,
        }

    @async_log_errors
    async def set_interval(self, seconds: float):
        self._interval = max(0.5, float(seconds))
        return {"interval": self._interval}

    @async_log_errors
    async def pause(self):
        self._paused = not self._paused
        return {"paused": self._paused}
