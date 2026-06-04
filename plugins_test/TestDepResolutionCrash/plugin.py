"""TestDepResolutionCrash — fixture for Hook 5 enable-time cascade test.

When `arguments.raise_on_enable` is true, raises RuntimeError inside
`on_enable`. The framework rollback transitions this plugin back to
INACTIVE (NOT FAILED_LOAD — see core.py rollback path). Any required-deps
dependent in a later topo level should then be transitioned to
FAILED_LOAD by Hook 5's enable-time precheck because the dep didn't
reach ENABLED state.
"""

from plexus.utils import Plugin
from plexus.decorators import async_log_errors, log_errors


class TestDepResolutionCrash(Plugin):
    @log_errors
    def on_load(self, *args, raise_on_enable: bool = False, **kwargs):
        self._raise_on_enable = bool(raise_on_enable)

    @async_log_errors
    async def on_enable(self):
        if self._raise_on_enable:
            raise RuntimeError(
                "TestDepResolutionCrash: raise_on_enable=true; "
                "deliberately failing for enable-time cascade test"
            )

    @async_log_errors
    async def on_disable(self):
        pass
