"""TestPR2NullEpFixture — fixture with endpoints: null.

Used by TestPR2Suite case 13 to verify that a plugin with no endpoints
loads cleanly with 0 endpoints and no error.
"""

from utils import Plugin
from decorators import log_errors, async_log_errors


class TestPR2NullEpFixture(Plugin):
    """Fixture with null endpoints — should load with 0 endpoints."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("TestPR2NullEpFixture.on_enable")

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("TestPR2NullEpFixture.on_disable")
