"""TestDepResolutionCrashDependent — required-deps TestDepResolutionCrash.

When the crash fixture's on_enable raises, framework rolls it back to
INACTIVE. Hook 5's enable-time precheck should observe that the required
dep is not ENABLED and transition THIS plugin to FAILED_LOAD before its
on_enable runs. No-op on_load / on_enable so the only failure path is
the precheck cascade.
"""

from plexus.utils import Plugin
from plexus.decorators import async_log_errors, log_errors


class TestDepResolutionCrashDependent(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass
