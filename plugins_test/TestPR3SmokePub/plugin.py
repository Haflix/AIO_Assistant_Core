"""TestPR3SmokePub — minimal Stage C cross-node publisher fixture.

Loaded on the parent (publishing) node only. Publishes 3 events to topic
`smoke/ping` after on_enable, then a 4th request_event to topic `smoke/ask`
that expects a return value from the remote subscriber.
"""

import asyncio
from plexus.utils import Plugin


class TestPR3SmokePub(Plugin):
    def on_load(self):
        self.publish_count = 0
        self.request_result = None

    async def on_enable(self):
        # Give the subnode a moment to advertise its subscriptions to us.
        await asyncio.sleep(2.0)
        for i in range(3):
            count = await self.publish_event(
                "smoke_event", payload={"counter": i}
            )
            self.publish_count = i + 1
            self._logger.info(
                "[SMOKE_PUB] published smoke_event#%d → %d subscribers",
                i,
                count,
            )
            await asyncio.sleep(0.3)

        try:
            self.request_result = await self.request_event(
                "smoke_request", payload={"q": "ping"}, timeout=3.0
            )
            self._logger.info(
                "[SMOKE_PUB] request_event returned: %r", self.request_result
            )
        except Exception as e:
            self._logger.warning("[SMOKE_PUB] request_event failed: %s", e)

    async def on_disable(self):
        pass

    async def get_status(self):
        """Returns dict with publish_count + request_result for verification."""
        return {
            "publish_count": self.publish_count,
            "request_result": self.request_result,
        }
