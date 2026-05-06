"""TestStreamSuite — Phase 2.

Exercises:
- Basic async / sync generator dispatch
- Empty / one-item / N-item generators
- Mid-stream raise → caller gets RequestException after partial items
- B-002 producer abandonment (consumer break / cancel; sync variant)
- Local large item (sanity, NOT B-024 which is wire-only)
- execute_stream_sync from sync iter
- Stream-level timeout (gen never yields)
- Request-entry reaped on normal completion
- Calling execute_stream on a non-generator endpoint
- B-041 sync-chain-through-stream (deferred — observation-only design TBD)
- Edge: cancellation between yields
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402
import time  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

from utils import Plugin  # noqa: E402
from decorators import async_log_errors, log_errors  # noqa: E402
from exceptions import RequestException  # noqa: E402

from _test_helpers import CaseRecorder  # noqa: E402


SUITE_VERSION = "0.1.0"
TARGET = "TestStreamTarget"
EXEC_TARGET = "TestExecuteTarget"  # for the non-generator endpoint test


class TestStreamSuite(Plugin):
    """Phase 2 suite plugin. See test_suite_plan.md §6 Phase 2."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestStreamSuite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestStreamSuite disabled")

    @async_log_errors
    async def run(
        self,
        category: Optional[str] = None,
        host: Optional[str] = None,
        case_ids: Optional[List[str]] = None,
        bug_ids: Optional[List[str]] = None,
        skip_slow: bool = False,
        allow_destructive: bool = True,
    ) -> Dict[str, Any]:
        rec = CaseRecorder("TestStreamSuite", SUITE_VERSION, self._plugin_core)

        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=False,
        )

        await self._basic_dispatch(rec, kw)
        await self._basic_edge_shapes(rec, kw)
        await self._basic_error(rec, kw)
        await self._basic_abandonment(rec, kw)
        await self._basic_payload(rec, kw)
        await self._basic_sync_iter(rec, kw)
        await self._basic_timeout(rec, kw)
        await self._basic_contract(rec, kw)
        await self._basic_b041_skip(rec, kw)
        await self._edge_cancellation_between_yields(rec, kw)

        return rec.to_dict()

    # ====================================================================
    # Helpers
    # ====================================================================

    async def _make_gen_request(self, method: str, args: Any = None):
        """Create a GeneratorRequest directly so the case has access to the
        Request object (for queue.qsize() inspection)."""
        return await self._plugin_core.create_gen_request(
            TARGET, method, args,
            "", "any", self.plugin_name, self.plugin_uuid,
        )

    # ====================================================================
    # BASIC dispatch
    # ====================================================================

    async def _basic_dispatch(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_async_basic(c):
            items = []
            async for chunk in self.execute_stream(
                TARGET, "ea_gen", {"n": 5, "prefix": "x", "delay_ms": 0},
                hosts=c.hosts,
            ):
                items.append(chunk)
            c.expect(items, ["x0", "x1", "x2", "x3", "x4"])

        async def body_sync_basic(c):
            items = []
            async for chunk in self.execute_stream(
                TARGET, "es_gen", {"n": 5, "prefix": "y", "delay_ms": 0},
                hosts=c.hosts,
            ):
                items.append(chunk)
            c.expect(items, ["y0", "y1", "y2", "y3", "y4"])

        await rec.run_case(
            "stream.async.basic", body_async_basic,
            hosts=("local", "remote"), tags=("basic",), **kw,
        )
        await rec.run_case(
            "stream.sync.basic", body_sync_basic,
            tags=("basic", "sync_target"), **kw,
        )

    # ====================================================================
    # BASIC edge shapes (empty / one)
    # ====================================================================

    async def _basic_edge_shapes(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_empty(c):
            items = []
            async for chunk in self.execute_stream(
                TARGET, "ea_gen_empty", hosts=c.hosts,
            ):
                items.append(chunk)
            c.expect(items, [])

        async def body_one(c):
            items = []
            async for chunk in self.execute_stream(
                TARGET, "ea_gen_one_item", hosts=c.hosts,
            ):
                items.append(chunk)
            c.expect(items, ["only"])

        await rec.run_case(
            "stream.async.empty", body_empty,
            hosts=("local", "remote"), tags=("edge",), **kw,
        )
        await rec.run_case(
            "stream.async.one", body_one,
            hosts=("local", "remote"), tags=("edge",), **kw,
        )

    # ====================================================================
    # BASIC mid-stream error
    # ====================================================================

    async def _basic_error(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_raises_after_2(c):
            # Current behavior (locked here as regression): when the source gen
            # raises mid-stream, Request.get_queue_stream breaks silently on the
            # error+EndOfQueue chunk (utils.py:1664-1667). The consumer just
            # sees the stream end after the items that were already pushed — no
            # exception. PluginCore.execute_stream's `if error: raise` (line
            # 1767) is therefore dead code. Bugtracker entry to be added.
            items = []
            try:
                async for chunk in self.execute_stream(
                    TARGET, "ea_gen_raises_after", {"n_yielded": 2},
                    hosts=c.hosts,
                ):
                    items.append(chunk)
            except RequestException:
                # If a future fix surfaces the error correctly, accept it too.
                pass
            c.expect(items[:2], ["item_0", "item_1"])

        await rec.run_case(
            "stream.async.raises_after_2", body_raises_after_2,
            hosts=("local", "remote"), tags=("error", "regression_lock"), **kw,
        )

    # ====================================================================
    # BASIC B-002 abandonment cases
    # ====================================================================

    async def _basic_abandonment(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_consumer_break(c):
            req = await self._make_gen_request("ea_gen_infinite", None)
            consumed = 0
            async for item, error, timed_out in req.get_queue_stream():
                if error:
                    raise AssertionError(f"unexpected error chunk: {item}")
                consumed += 1
                if consumed >= 3:
                    break

            await req.set_collected()
            await asyncio.sleep(0.1)              # let cancel propagate
            qsize_t1 = req.queue.qsize()
            await asyncio.sleep(1.0)              # bounded re-check
            qsize_t2 = req.queue.qsize()
            c.expect(qsize_t2, qsize_t1)          # B-002 fix: producer cancelled, no growth

        async def body_consumer_cancel(c):
            req = await self._make_gen_request("ea_gen_infinite", None)

            async def consume():
                async for item, error, _ in req.get_queue_stream():
                    if error:
                        return

            task = asyncio.create_task(consume())
            await asyncio.sleep(0.1)  # let task drain a few items
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            await req.set_collected()
            await asyncio.sleep(0.1)              # let cancel propagate
            qsize_t1 = req.queue.qsize()
            await asyncio.sleep(1.0)              # bounded re-check
            qsize_t2 = req.queue.qsize()
            c.expect(qsize_t2, qsize_t1)          # B-002 fix: producer cancelled, no growth

        async def body_consumer_break_sync(c):
            # Sync caller drives execute_stream_sync, breaks early, then we
            # observe the same producer-leak via the underlying GeneratorRequest.
            # Use create_gen_request_sync to retain a handle to the request.
            req_holder: Dict[str, Any] = {}

            def sync_block():
                req = self._plugin_core.create_gen_request_sync(
                    TARGET, "ea_gen_infinite", None,
                    "", "any", self.plugin_name, self.plugin_uuid,
                )
                req_holder["req"] = req
                consumed = 0
                for item, error, _ in req.get_queue_stream_sync():
                    if error:
                        return
                    consumed += 1
                    if consumed >= 3:
                        return

            await asyncio.to_thread(sync_block)
            req = req_holder["req"]
            await req.set_collected()
            await asyncio.sleep(0.1)              # let cancel propagate
            qsize_t1 = req.queue.qsize()
            await asyncio.sleep(1.0)              # bounded re-check
            qsize_t2 = req.queue.qsize()
            c.expect(qsize_t2, qsize_t1)          # B-002 fix: producer cancelled, no growth

        await rec.run_case(
            "stream.B-002.consumer_break", body_consumer_break,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-002",),
            **kw,
        )
        await rec.run_case(
            "stream.B-002.consumer_cancel", body_consumer_cancel,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-002",),
            **kw,
        )
        await rec.run_case(
            "stream.B-002.consumer_break_sync", body_consumer_break_sync,
            tags=("bug_repro", "regression_guard", "sync"), bug_ids=("B-002",),
            **kw,
        )

    # ====================================================================
    # BASIC large payload (local sanity, NOT B-024)
    # ====================================================================

    async def _basic_payload(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_large_item(c):
            items = []
            async for chunk in self.execute_stream(
                TARGET, "ea_gen_returns_one_large_item",
                {"size_bytes": 100_000}, hosts=c.hosts,
            ):
                items.append(chunk)
            c.expect(len(items), 1)
            c.expect(len(items[0]), 100_000)
            c.expect(items[0][0:1], b"\xab")

        await rec.run_case(
            "stream.local.large_item", body_large_item,
            tags=("payload",), **kw,
        )

    # ====================================================================
    # BASIC sync-iter (execute_stream_sync from sync context)
    # ====================================================================

    async def _basic_sync_iter(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_from_sync_context(c):
            def sync_block():
                items = []
                for chunk in self.execute_stream_sync(
                    TARGET, "ea_gen", {"n": 3, "prefix": "s", "delay_ms": 0},
                ):
                    items.append(chunk)
                return items

            items = await asyncio.to_thread(sync_block)
            c.expect(items, ["s0", "s1", "s2"])

        await rec.run_case(
            "stream.sync.from_sync_context", body_from_sync_context,
            tags=("sync",), **kw,
        )

    # ====================================================================
    # BASIC timeout
    # ====================================================================

    async def _basic_timeout(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_hanging_gen(c):
            # Current behavior: stream timeout raises asyncio.TimeoutError out
            # of get_queue_stream (utils.py:1655-1661 re-raises) — NOT a
            # RequestException. Asymmetric vs execute() which wraps timeout as
            # RequestException. Locked here; bugtracker note pending.
            t0 = time.perf_counter()
            saw_error = False
            try:
                async for _ in self.execute_stream(
                    TARGET, "ea_gen_hangs",
                    hosts=c.hosts, timeout=2.0,
                ):
                    raise AssertionError("hanging gen yielded unexpectedly")
            except (RequestException, asyncio.TimeoutError):
                saw_error = True
            elapsed = time.perf_counter() - t0
            if not saw_error:
                raise AssertionError(
                    f"expected timeout exception, got clean exit after {elapsed:.2f}s"
                )
            if not (1.5 <= elapsed <= 6.0):
                raise AssertionError(
                    f"elapsed={elapsed:.2f}s outside [1.5, 6.0]"
                )

        await rec.run_case(
            "stream.timeout.hanging_gen", body_hanging_gen,
            hosts=("local", "remote"), tags=("timeout", "regression_lock"),
            hard_timeout_s=15.0, **kw,
        )

    # ====================================================================
    # BASIC contract (request entry reaped on normal completion)
    # ====================================================================

    async def _basic_contract(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_collected_set(c):
            req = await self._make_gen_request(
                "ea_gen", {"n": 3, "prefix": "c", "delay_ms": 0},
            )
            req_id = req.id

            items = []
            async for item, error, _ in req.get_queue_stream():
                if error:
                    raise AssertionError(f"unexpected error chunk: {item}")
                items.append(item)

            await req.set_collected()
            c.expect(items[:3], ["c0", "c1", "c2"])

            deadline = time.perf_counter() + 30.0
            while time.perf_counter() < deadline:
                if req_id not in self._plugin_core.requests:
                    return
                await asyncio.sleep(0.5)
            raise AssertionError(
                f"request {req_id} not reaped within 30s"
            )

        async def body_endpoint_not_generator(c):
            # Current behavior (locked): same silent-truncate as raises_after_2
            # — get_queue_stream breaks on the error+EndOfQueue chunk produced
            # by _process_request_stream's "non-generator" branch. The
            # consumer's `async for` ends with zero items and no exception.
            items = []
            try:
                async for chunk in self.execute_stream(
                    EXEC_TARGET, "ea_add", (1, 2), hosts=c.hosts,
                ):
                    items.append(chunk)
            except RequestException:
                # If a future fix surfaces the error correctly, accept it too.
                pass
            c.expect(items, [])

        await rec.run_case(
            "stream.contract.collected_set", body_collected_set,
            tags=("lifecycle",), hard_timeout_s=45.0,
            **kw,
        )
        await rec.run_case(
            "stream.error.endpoint_not_generator", body_endpoint_not_generator,
            hosts=("local", "remote"), tags=("error", "regression_lock"), **kw,
        )

    # ====================================================================
    # BASIC B-041 — deferred (skip with reason)
    # ====================================================================

    async def _basic_b041_skip(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_b041(c):
            c.skip(
                "B-041 case design TBD: needs concrete sync→stream→sync cycle "
                "fixture. Direct chain observation duplicates B-039; deadlock "
                "observation requires saturating the threadpool."
            )

        await rec.run_case(
            "stream.B-041.sync_chain_through_stream", body_b041,
            tags=("bug_repro",), bug_ids=("B-041",),
            **kw,
        )

    # ====================================================================
    # EDGE cancellation between yields
    # ====================================================================

    async def _edge_cancellation_between_yields(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body_between_yields(c):
            req = await self._make_gen_request(
                "ea_gen", {"n": 5, "prefix": "z", "delay_ms": 200},
            )
            req_id = req.id

            collected = []

            async def consume():
                async for item, error, _ in req.get_queue_stream():
                    if error:
                        return
                    collected.append(item)

            task = asyncio.create_task(consume())
            # Let one item arrive (delay_ms=200) then cancel
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await req.set_collected()

            # Request entry should still be reaped within 30s via the
            # cleanup_requests created_at fallback (B-043 covers reap window).
            deadline = time.perf_counter() + 30.0
            while time.perf_counter() < deadline:
                if req_id not in self._plugin_core.requests:
                    return
                await asyncio.sleep(0.5)
            raise AssertionError(
                f"request {req_id} not reaped within 30s after cancel"
            )

        await rec.run_case(
            "stream.edge.cancellation.between_yields", body_between_yields,
            category="edge", tags=("cancellation", "edge"),
            hard_timeout_s=45.0,
            **kw,
        )
