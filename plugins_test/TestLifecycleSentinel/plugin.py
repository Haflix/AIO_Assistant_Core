"""TestLifecycleSentinel — Phase 4 static fixture.

Trivial plugin; its presence in core.plugins (or absence) is the B-007
assertion. MUST be listed AFTER TestLifecycleBrokenVersion in test_config.yml.
"""

from plexus.utils import Plugin
from plexus.decorators import async_log_errors, log_errors


class TestLifecycleSentinel(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        self.loaded: bool = True

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass

    @async_log_errors
    async def get_loaded(self) -> bool:
        return self.loaded
