"""TestDepResolutionB — depends on TestDepResolutionA and plexus framework."""

from plexus.utils import Plugin
from plexus.decorators import async_log_errors, log_errors


class TestDepResolutionB(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass
