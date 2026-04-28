"""TestRemoteSpoofer — Phase 5 fixture (peer subprocess only).

Issues notify_remote with caller-supplied author / author_id, bypassing
Plugin.notify's automatic author-stamping. Used by TestRemoteSuite for B-018
spoofing repros (literal "system" string + known-uuid variants).
"""

from typing import Any, Optional

from utils import Plugin
from decorators import async_log_errors, log_errors


class TestRemoteSpoofer(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        if (
            not getattr(self._plugin_core, "networking_enabled", False)
            or getattr(self._plugin_core, "network", None) is None
        ):
            self._logger.warning(
                "TestRemoteSpoofer enabled but networking not available; "
                "spoof_notify will skip"
            )

    @async_log_errors
    async def on_disable(self):
        pass

    @async_log_errors
    async def spoof_notify(
        self,
        target_ip: str,
        topic: str,
        args: Optional[Any] = None,
        author: str = "remote",
        author_id: str = "remote",
    ) -> Any:
        if (
            not getattr(self._plugin_core, "networking_enabled", False)
            or getattr(self._plugin_core, "network", None) is None
        ):
            return {"skipped": "networking not available"}
        return await self._plugin_core.network.notify_remote(
            target_ip,
            topic,
            args,
            author,
            author_id,
        )
