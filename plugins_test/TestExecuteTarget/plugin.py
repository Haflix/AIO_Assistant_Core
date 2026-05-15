"""TestExecuteTarget — Phase 1 fixture for TestExecuteSuite.

Exposes predictable sync/async/error endpoints. No business logic. Tier-1.
"""

import asyncio
from typing import Any, Optional

from plexus.utils import Plugin
from plexus.decorators import async_log_errors, log_errors
from plexus.exceptions import RequestException


class TestExecuteTarget(Plugin):
    """Predictable fixture endpoints for execute-suite cases."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.debug(
            f"TestExecuteTarget.on_enable name={self.plugin_name} uuid={self.plugin_uuid}"
        )

    @async_log_errors
    async def on_disable(self):
        self._logger.debug(f"TestExecuteTarget.on_disable name={self.plugin_name}")

    # ----- Value endpoints -----

    @async_log_errors
    async def ea_add(self, a: int = 0, b: int = 1) -> int:
        return a + b

    @log_errors
    def es_add(self, a: int = 0, b: int = 1) -> int:
        return a + b

    @async_log_errors
    async def ea_no_args(self) -> str:
        return "ok"

    @log_errors
    def es_no_args(self) -> str:
        return "ok"

    @async_log_errors
    async def ea_kwargs_only(self, *, name: str, value: int) -> str:
        return f"{name}={value}"

    @async_log_errors
    async def ea_positional_only(self, a, b, /) -> int:
        return a + b

    # ----- Error endpoints -----

    @async_log_errors
    async def ea_raises(self) -> None:
        raise ValueError("intentional")

    @log_errors
    def es_raises(self) -> None:
        raise ValueError("intentional")

    @async_log_errors
    async def ea_raises_request_exc(self) -> None:
        raise RequestException("specific")

    @async_log_errors
    async def ea_returns_none(self) -> None:
        return None

    # ----- Future-typed return endpoints -----

    @async_log_errors
    async def ea_returns_future(self) -> Any:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        fut.set_result("future_value")
        return fut

    @async_log_errors
    async def ea_returns_failing_future(self) -> Any:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        fut.set_exception(ValueError("intentional"))
        return fut

    # ----- Timing / accessibility / payload -----

    @async_log_errors
    async def ea_hang(self, seconds: float = 10.0) -> str:
        await asyncio.sleep(seconds)
        return "did_not_hang"

    @async_log_errors
    async def ea_private(self, value: int = 42) -> int:
        """accessible_by_other_plugins=False per plugin_config.yml."""
        return value

    @async_log_errors
    async def ea_large_return(self, size_bytes: int = 100_000) -> bytes:
        return b"\xab" * max(0, int(size_bytes))

    @async_log_errors
    async def ea_returns_arg(self, x: Any) -> Any:
        return x

    # ----- Self-call (multi-instance correctness) -----

    @async_log_errors
    async def ea_self_call(
        self,
        target_method: str,
        target_args: Optional[Any] = None,
        plugin_uuid: Optional[str] = None,
    ) -> Any:
        """Call another endpoint on THIS plugin instance.

        plugin_uuid defaults to self.plugin_uuid so the call is pinned to this
        instance even when the plugin class is loaded twice (multi-instance).
        """
        target_uuid = plugin_uuid if plugin_uuid is not None else self.plugin_uuid
        return await self.execute(
            self.plugin_name,
            target_method,
            target_args,
            plugin_uuid=target_uuid,
        )

    # ----- Chain-step (deep error propagation) -----

    @async_log_errors
    async def ea_chain_step(
        self,
        depth: int = 0,
        target_method: str = "ea_raises_request_exc",
    ) -> Any:
        """Call self.execute recursively `depth` times, then call `target_method`.

        Used to verify that a RequestException originating at depth=N reaches
        depth=0's caller with the original message preserved.
        """
        if depth <= 0:
            return await self.execute(
                self.plugin_name,
                target_method,
                None,
                plugin_uuid=self.plugin_uuid,
            )
        return await self.execute(
            self.plugin_name,
            "ea_chain_step",
            {"depth": depth - 1, "target_method": target_method},
            plugin_uuid=self.plugin_uuid,
        )

    # ----- Identity readback -----

    @async_log_errors
    async def get_uuid(self) -> str:
        return self.plugin_uuid
