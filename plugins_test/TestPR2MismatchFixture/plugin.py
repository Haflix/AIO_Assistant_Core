"""TestPR2MismatchFixture — fixture with access_name field != dict key.

Used by TestPR2Suite case 6 to verify that when the access_name field
differs from the dict key, the framework warns and uses the key.
"""

from plexus.utils import Plugin
from plexus.decorators import log_errors, async_log_errors


class TestPR2MismatchFixture(Plugin):
    """Fixture where endpoint access_name field differs from dict key."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("TestPR2MismatchFixture.on_enable")

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("TestPR2MismatchFixture.on_disable")

    @async_log_errors
    async def ping(self) -> str:
        return "pong"
