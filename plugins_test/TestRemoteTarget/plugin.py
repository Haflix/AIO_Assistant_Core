"""TestRemoteTarget — Phase 5 fixture (remote=True).

Provides the subnode-side endpoints + topic handlers that TestRemoteSuite
exercises across the wire. Each runtime subscription registered in
on_enable is unsubscribed in on_disable; cleanup is symmetric so
disable→re-enable cycles do not leak.
"""

import asyncio
from typing import Any

from plexus.utils import Plugin
from plexus.decorators import async_gen_log_errors, async_log_errors, log_errors


class TestRemoteTarget(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        self._sub_ids: list[str] = []

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("TestRemoteTarget.on_enable")

        # Code-driven hang sub on test/r/hang — B-020 driver. Registered
        # at runtime via subscribe_event(target_access_name=...) so publish_event
        # dispatch goes through execute() to the r_hang_topic_handler endpoint.
        sid_hang = await self._plexus.subscribe_event(
            "test/r/hang",
            self.plugin_name,
            self.plugin_uuid,
            target_access_name="r_hang_topic_handler",
        )
        self._sub_ids.append(sid_hang)

        # Stage N (PR4): request_event coverage. Four positive-guard subs
        # backing the new TestRemoteSuite.remote.request_event[*] cases.
        for topic, target in (
            ("test/r/req_basic",        "r_request_basic_handler"),
            ("test/r/req_raise",        "r_request_raise_handler"),
            ("test/r/req_stream_basic", "r_request_stream_basic_handler"),
            ("test/r/req_stream_raise", "r_request_stream_raise_handler"),
        ):
            sid = await self._plexus.subscribe_event(
                topic,
                self.plugin_name,
                self.plugin_uuid,
                target_access_name=target,
            )
            self._sub_ids.append(sid)

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("TestRemoteTarget.on_disable")
        for sid in list(self._sub_ids):
            try:
                await self._plexus.unsubscribe_event(sid)
            except Exception:
                pass
        self._sub_ids = []

    # ── Plain remote-callable endpoints ─────────────────────────────────

    @async_log_errors
    async def r_open(self, value: Any = None) -> Any:
        return value

    @async_gen_log_errors
    async def r_async_gen(self, n: int = 3):
        for i in range(max(0, int(n))):
            yield f"r_{i}"

    @async_gen_log_errors
    async def r_async_gen_huge_item(self, size_mb: int = 101):
        size_bytes = max(1, int(size_mb)) * 1024 * 1024
        yield {"data": b"\xab" * size_bytes}

    @async_gen_log_errors
    async def r_async_gen_raises_after(self, n_yielded: int = 5):
        for i in range(max(0, int(n_yielded))):
            yield f"r_{i}"
        raise ValueError("midstream")

    # ── Topic handlers (subscribed via on_enable) ──────────────────────

    @async_log_errors
    async def r_topic_open(self, event=None) -> Any:
        # Subscriber endpoints receive an Event object under the new API.
        payload = event.payload if event is not None else None
        return {"received": payload}

    @async_gen_log_errors
    async def r_topic_huge_stream(self, event=None):
        # B-024 driver. _handle_request_event_stream emits ONE
        # MSG_STREAM_CHUNK per yielded item with chunk_length = pickled-size + 1.
        # If chunk_length exceeds MAX_MESSAGE_SIZE (100 MB on the receiver),
        # the receiver raises NetworkRequestException and the entire stream
        # is killed. _handle_execute_stream splits;
        # _handle_request_event_stream does NOT — that's the bug.
        # 101 MB triggers the rejection.
        yield {"data": b"\xab" * (101 * 1024 * 1024)}

    @async_log_errors
    async def r_hang_topic_handler(self, event=None):
        # B-020 driver: subscribed via on_enable to test/r/hang.
        # publish_event_sync against this from a SYNC context should NOT
        # block (post-Stage-D contract: publish_event_sync is fire-and-forget).
        # Reused by Stage N's request_event timeout-honored test — the
        # handler hangs forever, so any caller that waits for a result must
        # honor its own timeout to avoid deadlocking.
        await asyncio.Event().wait()

    # ── Stage N (PR4): request_event handler endpoints ─────────────────

    @async_log_errors
    async def r_request_basic_handler(self, event=None):
        """Happy-path request_event handler. Echoes the publisher payload
        back inside an envelope so the parent test can verify the round
        trip (payload preservation + Event metadata wrapping)."""
        payload = event.payload if event is not None else None
        return {"echoed": payload}

    @async_log_errors
    async def r_request_raise_handler(self, event=None):
        """Handler that raises a ValueError with a marker string. Verifies
        that remote handler exceptions surface to the caller as
        RequestException with the original message preserved."""
        raise ValueError("requested-error-marker")

    @async_gen_log_errors
    async def r_request_stream_basic_handler(self, event=None):
        """Async-gen handler yielding 3 chunks. The first chunk reaches
        the caller wrapped in an Event (LOCKED I); subsequent chunks are
        raw."""
        for i in range(3):
            yield {"chunk": i}

    @async_gen_log_errors
    async def r_request_stream_raise_handler(self, event=None):
        """Async-gen handler yielding 2 chunks then raising a ValueError.
        Verifies mid-stream errors propagate to the caller as
        RequestException with the original message preserved."""
        yield {"chunk": 0}
        yield {"chunk": 1}
        raise ValueError("midstream-error-marker")
