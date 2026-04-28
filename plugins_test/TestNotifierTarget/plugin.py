"""TestNotifierTarget — Phase 3 fixture.

Predictable handlers for the TestNotifierSuite. Tier-1.
"""

import asyncio
from typing import Any, List, Tuple, Optional

from utils import Plugin
from decorators import (
    async_gen_log_errors,
    async_log_errors,
    gen_log_errors,
    log_errors,
)


class TestNotifierTarget(Plugin):
    """Notifier fixture — config-driven topics + readback endpoints."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self.event_log: List[Tuple] = []
        self.count_invocations: int = 0
        self.self_publish_count: int = 0
        self.observed_chain: Optional[Tuple] = None
        self._code_sub_id: Optional[str] = None

    @async_log_errors
    async def on_enable(self):
        # Code-driven subscription: register count handler on test/count
        self._code_sub_id = await self._plugin_core.subscribe(
            "test/count",
            self.plugin_name,
            self.plugin_uuid,
            handler=self.n_handle_count,
        )
        self._logger.debug(
            f"TestNotifierTarget.on_enable code_sub={self._code_sub_id}"
        )

    @async_log_errors
    async def on_disable(self):
        if self._code_sub_id:
            try:
                await self._plugin_core.unsubscribe(self._code_sub_id)
            except Exception:
                pass
            self._code_sub_id = None

    # ----- Config-driven topic handlers -----

    @async_log_errors
    async def n_handle_greet(self, name: str = "world") -> str:
        self.event_log.append(("greet", name))
        return f"Hello, {name}!"

    @async_log_errors
    async def n_handle_math(self, a: int = 0, b: int = 0) -> int:
        self.event_log.append(("math", a, b))
        return a + b

    @async_log_errors
    async def n_handle_wild_a(self, *args, **kwargs):
        self.event_log.append(("wild_a", args, kwargs))
        return "wild_a"

    @async_log_errors
    async def n_handle_wild_b(self, *args, **kwargs):
        self.event_log.append(("wild_b", args, kwargs))
        return "wild_b"

    @async_log_errors
    async def n_handle_priv(self, data: Any = None) -> Any:
        self.event_log.append(("priv", data))
        return {"priv_received": data}

    @async_log_errors
    async def n_handle_count(self, *args, **kwargs) -> Any:
        self.count_invocations += 1
        self.event_log.append(("count", args, kwargs))
        return {"count": self.count_invocations}

    @async_gen_log_errors
    async def n_handle_async_gen(self, n: int = 3):
        n = max(0, int(n))
        for i in range(n):
            yield f"async_{i}"
        self.event_log.append(("async_gen_end", n))

    @gen_log_errors
    def n_handle_sync_gen(self, n: int = 3):
        n = max(0, int(n))
        for i in range(n):
            yield f"sync_{i}"
        self.event_log.append(("sync_gen_end", n))

    # ----- B-039 chain observation -----

    @log_errors
    def trigger_topic_hop(self) -> str:
        # Set a synthetic chain on this thread BEFORE the call; the
        # topic_hop_observer should see it propagated if the framework
        # forwards _call_chain through topic dispatch (it doesn't — that's
        # B-039). The observer reads its own chain on entry.
        from PluginCore import _sync_call_chain
        _sync_call_chain.chain = ("synthetic.outer.call",)
        try:
            self._plugin_core.request_topic_sync("topic/hop", None)
        finally:
            _sync_call_chain.chain = ()
        return "trigger_done"

    @log_errors
    def topic_hop_observer(self) -> str:
        from PluginCore import _sync_call_chain
        self.observed_chain = tuple(getattr(_sync_call_chain, "chain", ()) or ())
        return "observed"

    # ----- Self-publish -----

    @async_log_errors
    async def trigger_self_publish(self) -> int:
        # Plugin.notify forwards self.plugin_name / self.plugin_uuid as author
        return await self.notify("test/self")

    @async_log_errors
    async def self_publish_handler(self, *args, **kwargs):
        self.self_publish_count += 1
        self.event_log.append(("self_publish", args, kwargs))
        return None

    @async_log_errors
    async def notify_with_own_author(self, topic: str, args: Any = None) -> int:
        return await self.notify(topic, args)

    # ----- Readback / utility -----

    @async_log_errors
    async def get_event_log(self) -> List[Tuple]:
        return list(self.event_log)

    @async_log_errors
    async def reset_event_log(self) -> int:
        n = len(self.event_log)
        self.event_log = []
        self.count_invocations = 0
        self.self_publish_count = 0
        self.observed_chain = None
        return n

    @async_log_errors
    async def get_count(self) -> int:
        return self.count_invocations

    @async_log_errors
    async def get_self_publish_count(self) -> int:
        return self.self_publish_count

    @async_log_errors
    async def get_observed_chain(self) -> Optional[Tuple]:
        return self.observed_chain
