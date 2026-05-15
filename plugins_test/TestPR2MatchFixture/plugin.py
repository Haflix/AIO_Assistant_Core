"""TestPR2MatchFixture — fixture with access_name field equal to dict key.

Used by TestPR2Suite case 5 to verify that when the access_name field
matches the dict key exactly, the plugin loads without a warning.
"""

from plexus.utils import Plugin
from plexus.decorators import log_errors, async_log_errors


class TestPR2MatchFixture(Plugin):
    """Fixture where endpoint access_name field matches dict key."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("TestPR2MatchFixture.on_enable")

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("TestPR2MatchFixture.on_disable")

    @async_log_errors
    async def ping(self) -> str:
        return "pong"
