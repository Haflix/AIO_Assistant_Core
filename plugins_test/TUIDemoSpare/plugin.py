"""TUI smoke demo spare — minimal plugin loaded but disabled.

Showcases the live Phase-column update: enable it from the Plugins
tab and watch the row flip DISABLED → LOADING → READY without a
periodic refresh tick (the `_core/plugin/state_changed` bus
subscription drives the immediate update).
"""

from plexus.utils import Plugin
from plexus.decorators import async_log_errors, log_errors


class TUIDemoSpare(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        self.description = (
            "Minimal spare plugin — toggle Enable/Disable to demo "
            "live Phase-column updates."
        )

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass

    @async_log_errors
    async def hello(self):
        return "Hello from TUIDemoSpare!"
