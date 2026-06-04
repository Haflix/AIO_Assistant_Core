"""TestDepResolutionA — base fixture; no deps. Version pinned at 1.0.0
so TestDepResolutionB's `>=1.0,<2.0` spec is exercised.
"""

from plexus.utils import Plugin
from plexus.decorators import async_log_errors, log_errors


class TestDepResolutionA(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass
