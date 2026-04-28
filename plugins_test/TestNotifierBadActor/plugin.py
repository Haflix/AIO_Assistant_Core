"""TestNotifierBadActor — Phase 3 fixture.

Misbehaving handlers (raises ValueError, raises CancelledError, hangs forever)
that the suite registers on-demand for B-035 / B-036 / error.sub_raises.
Loaded programmatically by TestNotifierSuite using the §5.4 idiom.
"""

import asyncio
from typing import Any, Dict, List, Optional

from utils import Plugin
from decorators import async_log_errors, log_errors


class TestNotifierBadActor(Plugin):
    """On-demand misbehaving subscriber fixture."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self._sub_ids: List[str] = []

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("TestNotifierBadActor.on_enable")

    @async_log_errors
    async def on_disable(self):
        # Defensive: clean up any subs we still have
        for sid in list(self._sub_ids):
            try:
                await self._plugin_core.unsubscribe(sid)
            except Exception:
                pass
        self._sub_ids = []

    # ----- Bad handlers -----

    async def bad_handler_raises(self, *args, **kwargs):
        raise ValueError("bad-actor-raises")

    async def bad_handler_cancels(self, *args, **kwargs):
        raise asyncio.CancelledError()

    async def bad_handler_hangs(self, *args, **kwargs):
        await asyncio.Event().wait()

    async def bad_handler_returns(self, *args, **kwargs):
        return "ok"

    # ----- Registration API -----

    _HANDLERS: Dict[str, str] = {
        "raises": "bad_handler_raises",
        "cancels": "bad_handler_cancels",
        "hangs": "bad_handler_hangs",
        "returns": "bad_handler_returns",
    }

    @async_log_errors
    async def register_handler(self, topic: str, handler_name: str) -> str:
        method_name = self._HANDLERS.get(handler_name)
        if method_name is None:
            raise ValueError(
                f"unknown handler_name {handler_name!r}; "
                f"valid: {sorted(self._HANDLERS)}"
            )
        handler = getattr(self, method_name)
        sub_id = await self._plugin_core.subscribe(
            topic,
            self.plugin_name,
            self.plugin_uuid,
            handler=handler,
        )
        self._sub_ids.append(sub_id)
        return sub_id

    @async_log_errors
    async def unregister_all(self) -> int:
        n = 0
        for sid in list(self._sub_ids):
            try:
                if await self._plugin_core.unsubscribe(sid):
                    n += 1
            except Exception:
                pass
        self._sub_ids = []
        return n
