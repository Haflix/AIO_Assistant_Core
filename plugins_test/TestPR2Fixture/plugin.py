"""TestPR2Fixture — minimal fixture used by TestPR2Suite.

Provides a single callable endpoint `ping` that returns "pong". Used to
verify that dict-form endpoint loading actually produces a callable endpoint.
"""

from utils import Plugin
from decorators import log_errors, async_log_errors


class TestPR2Fixture(Plugin):
    """Minimal fixture plugin for PR2 config-restructure test cases."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("TestPR2Fixture.on_enable")

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("TestPR2Fixture.on_disable")

    @async_log_errors
    async def ping(self) -> str:
        return "pong"
