"""TUI smoke demo orchestrator — multi-target subscriber that adds
variety to the Subscriptions / Plugins-detail panes:

  - 4 declared subscriptions across 3 topics (one cross-plugin to
    TUIDemoSub, demoing the Target column)
  - 3 handler endpoints + a stats endpoint + a streaming endpoint
    (try Call on `count_stream` to see streaming output rendering)
"""

import asyncio

from plexus.utils import Plugin
from plexus.decorators import async_log_errors, async_gen_log_errors, log_errors


class TUIDemoOrch(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        self.description = (
            "TUI smoke demo orchestrator — multi-target subscriber + "
            "streaming endpoint demo."
        )
        self._counts: dict = {
            "heartbeat": 0,
            "burst": 0,
            "greeted_via_orch": 0,
        }

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass

    @async_log_errors
    async def handle_heartbeat(self, event):
        self._counts["heartbeat"] += 1
        return {"counter": self._counts["heartbeat"]}

    @async_log_errors
    async def handle_burst(self, event):
        self._counts["burst"] += 1
        return {"counter": self._counts["burst"]}

    @async_log_errors
    async def handle_greeted_via_orch(self, event):
        self._counts["greeted_via_orch"] += 1
        return {"counter": self._counts["greeted_via_orch"]}

    @async_log_errors
    async def stats(self):
        return dict(self._counts)

    @async_gen_log_errors()
    async def count_stream(self, n: int = 5):
        """Async generator endpoint — yields integers 1..n with a
        small delay between each. Demonstrates the framework's
        execute_stream path + the TUI's streaming-result rendering.
        """
        n = max(1, min(int(n), 50))
        for i in range(1, n + 1):
            await asyncio.sleep(0.2)
            yield {"i": i, "of": n}
