"""TUI smoke demo subscriber — receives events published by
TUIDemoPub. Adds rows to the Subscriptions panel so that pane is not
empty in single-node smoke testing."""

from plexus.utils import Plugin
from plexus.decorators import async_log_errors, log_errors


class TUIDemoSub(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        self.description = (
            "TUI smoke demo subscriber — handles TUIDemoPub events."
        )
        self._counter = 0

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass

    @async_log_errors
    async def handle_greeted(self, event):
        self._counter += 1
        return {"counter": self._counter}

    @async_log_errors
    async def handle_ticked(self, event):
        self._counter += 1
        return {"counter": self._counter}

    @async_log_errors
    async def count(self):
        return {"counter": self._counter}
