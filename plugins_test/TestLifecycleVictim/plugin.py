"""TestLifecycleVictim — Phase 4 fixture.

Controllable victim plugin with configurable on_enable / on_disable behaviors.
Used by TestLifecycleSuite for B-004 / B-005 / B-006 / B-008 / B-009 / B-010
repros plus basic load/enable/disable/reload contract checks.
"""

import asyncio
from typing import Any, Dict, Optional

from utils import Plugin
from decorators import async_log_errors, log_errors


class TestLifecycleVictim(Plugin):
    """Configurable lifecycle victim."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self.db_open: bool = False
        self.enable_count: int = 0
        self.disable_count: int = 0
        self._on_enable_raises_after_setup: bool = False
        self._on_disable_raises: bool = False
        self._on_disable_hangs_secs: float = 0.0
        self._on_enable_delay_secs: float = 0.0
        self._cross_call_result: Optional[Any] = None
        self._cross_call_during_enable: bool = False

    @async_log_errors
    async def on_enable(self):
        if self._on_enable_delay_secs > 0:
            await asyncio.sleep(self._on_enable_delay_secs)
        # Open "DB" first (mirrors B-004 fixture spec — a partial setup that
        # would leak resources if on_enable raises mid-way).
        self.db_open = True

        if self._cross_call_during_enable:
            try:
                self._cross_call_result = await self.execute(
                    "TestLifecycleVictim2", "is_db_open",
                )
            except Exception as e:
                self._cross_call_result = f"ERROR: {type(e).__name__}: {e}"
            self._cross_call_during_enable = False

        if self._on_enable_raises_after_setup:
            self._on_enable_raises_after_setup = False  # one-shot
            raise RuntimeError("intentional on_enable failure after partial setup")

        self.enable_count += 1

    @async_log_errors
    async def on_disable(self):
        if self._on_disable_hangs_secs > 0:
            await asyncio.sleep(self._on_disable_hangs_secs)
        if self._on_disable_raises:
            self._on_disable_raises = False  # one-shot
            raise RuntimeError("intentional on_disable failure")
        self.db_open = False
        self.disable_count += 1

    # ----- Endpoints -----

    @async_log_errors
    async def configure(
        self,
        on_enable_raises_after_setup: Optional[bool] = None,
        on_disable_raises: Optional[bool] = None,
        on_disable_hangs_secs: Optional[float] = None,
        on_enable_delay_secs: Optional[float] = None,
        cross_call_during_enable: Optional[bool] = None,
    ) -> Dict[str, Any]:
        if on_enable_raises_after_setup is not None:
            self._on_enable_raises_after_setup = bool(on_enable_raises_after_setup)
        if on_disable_raises is not None:
            self._on_disable_raises = bool(on_disable_raises)
        if on_disable_hangs_secs is not None:
            self._on_disable_hangs_secs = float(on_disable_hangs_secs)
        if on_enable_delay_secs is not None:
            self._on_enable_delay_secs = float(on_enable_delay_secs)
        if cross_call_during_enable is not None:
            self._cross_call_during_enable = bool(cross_call_during_enable)
        return await self.get_state()

    @async_log_errors
    async def get_state(self) -> Dict[str, Any]:
        return {
            "db_open": self.db_open,
            "enable_count": self.enable_count,
            "disable_count": self.disable_count,
            "on_enable_raises_after_setup": self._on_enable_raises_after_setup,
            "on_disable_raises": self._on_disable_raises,
            "on_disable_hangs_secs": self._on_disable_hangs_secs,
            "on_enable_delay_secs": self._on_enable_delay_secs,
            "cross_call_during_enable": self._cross_call_during_enable,
            "cross_call_result": self._cross_call_result,
        }

    @async_log_errors
    async def get_arguments(self) -> Any:
        return self.arguments

    @async_log_errors
    async def victim_hang_endpoint(self, secs: float = 60.0) -> str:
        await asyncio.sleep(float(secs))
        return "did_not_hang"

    @async_log_errors
    async def inject_bad_request(self, req_id: str = "bad-test-id") -> str:
        # Direct write into core.requests for B-006. The recorder has the
        # private-API carve-out for this in plan §3.4.
        self._plugin_core.requests[req_id] = object()
        return req_id

    @async_log_errors
    async def is_db_open(self) -> bool:
        return self.db_open

    @async_log_errors
    async def cross_call_victim2(self) -> Any:
        return await self.execute("TestLifecycleVictim2", "is_db_open")
