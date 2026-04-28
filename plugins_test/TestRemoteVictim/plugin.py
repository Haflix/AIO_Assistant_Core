"""TestRemoteVictim — Phase 5 fixture (remote=False).

Provides a code-driven sub on test/r/code (B-001 bypass target) and a
streaming code-driven sub on test/r/code_stream (B-042 streaming variant).
"""

from typing import Any

from utils import Plugin
from decorators import async_gen_log_errors, async_log_errors, log_errors


class TestRemoteVictim(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        self.bypass_count: int = 0
        self.stream_bypass_count: int = 0
        self._sub_ids = []

    @async_log_errors
    async def on_enable(self):
        sid = await self._plugin_core.subscribe(
            "test/r/code",
            self.plugin_name,
            self.plugin_uuid,
            handler=self._code_handler,
        )
        self._sub_ids.append(sid)

        sid_stream = await self._plugin_core.subscribe(
            "test/r/code_stream",
            self.plugin_name,
            self.plugin_uuid,
            handler=self._code_stream_handler,
        )
        self._sub_ids.append(sid_stream)
        self._logger.debug(
            f"TestRemoteVictim.on_enable subs={[s[:8] for s in self._sub_ids]}"
        )

    @async_log_errors
    async def on_disable(self):
        for sid in list(self._sub_ids):
            try:
                await self._plugin_core.unsubscribe(sid)
            except Exception:
                pass
        self._sub_ids = []

    async def _code_handler(self, *args, **kwargs):
        self.bypass_count += 1

    @async_gen_log_errors
    async def _code_stream_handler(self, *args, **kwargs):
        for i in range(3):
            self.stream_bypass_count += 1
            yield f"bypass_{i}"

    @async_log_errors
    async def r_local_only(self, value: Any = None) -> Any:
        return value

    @async_log_errors
    async def r_topic_local_only(self, payload: Any = None) -> Any:
        return {"local_only_received": payload}

    @async_log_errors
    async def get_bypass_count(self) -> int:
        return self.bypass_count

    @async_log_errors
    async def get_stream_bypass_count(self) -> int:
        return self.stream_bypass_count

    @async_log_errors
    async def reset_bypass(self) -> int:
        prev = self.bypass_count + self.stream_bypass_count
        self.bypass_count = 0
        self.stream_bypass_count = 0
        return prev
