"""TestPR2ListFixture — fixture with legacy list-form endpoints.

Used by TestPR2Suite case 1 to verify that load_plugin_with_conf rejects
the list-form with a clear error message referencing the dict form.
"""

from utils import Plugin
from decorators import log_errors, async_log_errors


class TestPR2ListFixture(Plugin):
    """Fixture with list-form endpoints — load should fail with PR2 error."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("TestPR2ListFixture.on_enable")

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("TestPR2ListFixture.on_disable")

    @async_log_errors
    async def ping(self) -> str:
        return "pong"
