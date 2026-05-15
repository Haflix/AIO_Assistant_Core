"""TestPR3SmokeSub — minimal Stage C cross-node subscriber fixture.

Loaded on the subnode only. Subscribes to `smoke/ping` (publish_event
fan-out) and `smoke/ask` (request_event with return value).
"""

from plexus.utils import Plugin


class TestPR3SmokeSub(Plugin):
    def on_load(self):
        self.received_events = []
        self.request_count = 0

    async def on_enable(self):
        pass

    async def on_disable(self):
        pass

    async def handle_ping(self, event):
        self.received_events.append(
            {
                "topic": event.topic,
                "payload": event.payload,
                "author": event.author,
                "author_host": event.author_host,
            }
        )
        self._logger.info(
            "[SMOKE_SUB] received topic=%r counter=%s author_host=%r",
            event.topic,
            event.payload.get("counter"),
            event.author_host,
        )

    async def handle_ask(self, event):
        self.request_count += 1
        self._logger.info(
            "[SMOKE_SUB] request_event topic=%r payload=%r author_host=%r",
            event.topic,
            event.payload,
            event.author_host,
        )
        return {"pong": event.payload.get("q"), "received_count": self.request_count}

    async def get_received(self):
        """Cross-node-callable status check."""
        return {
            "events_received": len(self.received_events),
            "request_count": self.request_count,
            "events": self.received_events,
        }
