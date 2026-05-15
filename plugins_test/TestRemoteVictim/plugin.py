"""TestRemoteVictim — Phase 5 fixture (remote=False).

Minimal plugin used to verify the framework's `plugin.remote=False` gate
blocks remote callers. Held endpoints intentionally inherit
`plugin.remote=False` so any cross-node call to them is rejected by
`find_endpoint`.

Stage N (PR4): bypass-bug fixtures removed. The historic B-001 / B-042
repros that lived here targeted the legacy `request_topic` /
`_handle_topic_request` path which Stage D deleted. Structural canaries
live in TestBugSuite (`bug.B-001.request_topic_method_gone`,
`bug.B-042.request_topic_stream_method_gone`).
"""

from typing import Any

from plexus.utils import Plugin
from plexus.decorators import async_log_errors, log_errors


class TestRemoteVictim(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass

    @async_log_errors
    async def r_local_only(self, value: Any = None) -> Any:
        return value
