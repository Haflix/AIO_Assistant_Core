"""TUI smoke demo publisher.

Two purposes:

  1. Fires a burst of demo events from `fire_events` so the Events ▸
     Live stream panel populates. The events match topics that
     `TUIDemoSub` subscribes to, so subscriber dispatch metrics show
     up too.
  2. Carries a few tagged endpoints (`demo`, `fire`, `ping`) so the
     Plugins-tab tag-search input has something to filter against.
"""

from utils import Plugin
from decorators import async_log_errors, log_errors


class TUIDemoPub(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        self.description = (
            "TUI smoke demo publisher — fires demo events on demand."
        )

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass

    @async_log_errors
    async def fire_events(self):
        """Publish 5 events: 3 greeted + 2 ticked. Each fires a
        `_core/event/published` bus emit (and `_core/event/requested`
        for any matching subscriber dispatch), populating the Events
        tab's Live-stream panel."""
        for i in range(3):
            await self.publish_event("greeted", payload={"i": i, "kind": "greet"})
        for i in range(2):
            await self.publish_event("ticked", payload={"i": i, "kind": "tick"})
        return {"fired": 5, "topics": ["demo.greeted", "demo.ticked"]}

    @async_log_errors
    async def ping(self):
        """Tagged with `demo, ping` — try filtering by tag in the
        Plugins tab."""
        return "pong"

    @async_log_errors
    async def echo(self, message: str = "hello"):
        return {"echo": message}
