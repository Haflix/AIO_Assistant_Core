"""TestInternalEventBusSuite — Session 2 coverage for B-073 internal
event bus + done-callback eviction (shipped 0.24.0).

Categories:
  1. _bus_register      — register / idempotent-register
  2. _bus_unregister    — basic / during_emit / idempotent
  3. _bus_fast_path     — no-observer fast path
  4. _bus_error         — observer raises Exception → swallowed + log
  5. _bus_load          — 1k events with single observer
  6. _bus_guard         — recursion-depth guard / depth-restored invariant
  7. _bus_cleanup       — _unobserve_plugin bulk-clears observer ownership
  8. _eviction          — Request / GeneratorRequest eviction (4 cases)

Bus topics use the framework-internal ``_core/test/bus/...`` prefix —
``_internal_emit`` does not call any topic validator (direct dict
lookup at PluginCore.py:885), so the leading-underscore is safe. The
per-case YAML subscription (``bus_stream_sub`` → ``handle_bus_stream``)
uses a plain ``tibs/stream`` topic since the events/subscriptions path
DOES go through the validator and must be forward-compatible with
Step 9's eventual underscore-prefix rejection.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402
import logging  # noqa: E402
import uuid as _uuid  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

from utils import Plugin, Event  # noqa: E402
from decorators import async_gen_log_errors, async_log_errors, log_errors  # noqa: E402

from _test_helpers import CaseRecorder  # noqa: E402

# Module-level ContextVar imported so cases 9/10 can read depth from
# inside / outside an emit. Owned by PluginCore — read-only in tests.
from PluginCore import _EMIT_DEPTH, _MAX_EMIT_DEPTH  # noqa: E402


SUITE_VERSION = "0.1.0"

EXEC_TARGET = "TestExecuteTarget"
STREAM_TARGET = "TestStreamTarget"


class _LogCapture(logging.Handler):
    """Helper handler — collects records emitted to a given logger.

    Used by ``bus.error.observer_raises`` + ``bus.guard.recursion_depth``
    to assert the framework logged the expected message.
    """

    def __init__(self) -> None:
        super().__init__()
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestInternalEventBusSuite(Plugin):
    """B-073 internal event bus + eviction regression suite."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self.bus_stream_chunks: List[int] = []

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestInternalEventBusSuite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestInternalEventBusSuite disabled")

    # ─────────────────────────────────────────────────────────────────
    # YAML-declared subscription handler (eviction.event_stream.completed)
    # ─────────────────────────────────────────────────────────────────

    @async_gen_log_errors
    async def handle_bus_stream(self, event: Event):
        for i in range(3):
            yield i
            await asyncio.sleep(0)

    # ─────────────────────────────────────────────────────────────────
    # Entrypoint
    # ─────────────────────────────────────────────────────────────────

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
        rec = CaseRecorder("TestInternalEventBusSuite", SUITE_VERSION, self._plugin_core)

        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=False,
        )

        await self._bus_register(rec, kw)
        await self._bus_unregister(rec, kw)
        await self._bus_fast_path(rec, kw)
        await self._bus_error(rec, kw)
        await self._bus_load(rec, kw)
        await self._bus_guard(rec, kw)
        await self._bus_cleanup(rec, kw)
        await self._eviction(rec, kw)

        return rec.to_dict()

    # ─────────────────────────────────────────────────────────────────
    # 1. REGISTER
    # ─────────────────────────────────────────────────────────────────

    async def _bus_register(self, rec: CaseRecorder, kw: Dict) -> None:

        async def body_observe(c):
            log: List = []
            topic = "_core/test/bus/register/observe"

            def cb(t, p):
                log.append((t, p))

            self.internal_observe(topic, cb)
            try:
                self._plugin_core._internal_emit(topic, x=1, y="z")
                c.expect(log, [(topic, {"x": 1, "y": "z"})])
            finally:
                self.internal_unobserve(topic, cb)

        async def body_idempotent(c):
            log: List = []
            topic = "_core/test/bus/register/idempotent"

            def cb(t, p):
                log.append((t, p))

            self.internal_observe(topic, cb)
            self.internal_observe(topic, cb)  # second call must be no-op
            try:
                self._plugin_core._internal_emit(topic, n=1)
                # Single dispatch despite double-register
                c.expect(len(log), 1)
                # Owner set should have a single (topic, cb) pair
                owners = self._plugin_core._observer_owners.get(self.plugin_uuid, set())
                pair_count = sum(1 for t, _ in owners if t == topic)
                c.expect(pair_count, 1)
                # Per-topic listener list should also be deduplicated
                listeners = self._plugin_core._internal_observers.get(topic, [])
                c.expect(len(listeners), 1)
            finally:
                self.internal_unobserve(topic, cb)

        await rec.run_case(
            "bus.register.observe", body_observe,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )
        await rec.run_case(
            "bus.register.idempotent", body_idempotent,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )

    # ─────────────────────────────────────────────────────────────────
    # 2. UNREGISTER
    # ─────────────────────────────────────────────────────────────────

    async def _bus_unregister(self, rec: CaseRecorder, kw: Dict) -> None:

        async def body_basic(c):
            log: List = []
            topic = "_core/test/bus/unregister/basic"

            def cb(t, p):
                log.append((t, p))

            self.internal_observe(topic, cb)
            removed = self.internal_unobserve(topic, cb)
            c.expect(removed, True)
            self._plugin_core._internal_emit(topic, n=1)
            c.expect(log, [])

        async def body_during_emit(c):
            log_a: List = []
            log_b: List = []
            topic = "_core/test/bus/unregister/during_emit"

            def cb_b(t, p):
                log_b.append((t, p))

            def cb_a(t, p):
                log_a.append((t, p))
                # Mid-emit: unobserve B. Snapshot taken at PluginCore.py:897
                # means B still fires THIS emit, but NOT the next one.
                self.internal_unobserve(topic, cb_b)

            self.internal_observe(topic, cb_a)
            self.internal_observe(topic, cb_b)
            try:
                self._plugin_core._internal_emit(topic, n=1)
                # Snapshot semantics: A fires (and unregisters B), B fires THIS emit
                c.expect(len(log_a), 1)
                c.expect(len(log_b), 1)
                # Next emit: B was unobserved, only A fires
                self._plugin_core._internal_emit(topic, n=2)
                c.expect(len(log_a), 2)
                c.expect(len(log_b), 1)
            finally:
                self.internal_unobserve(topic, cb_a)
                self.internal_unobserve(topic, cb_b)

        async def body_idempotent(c):
            topic = "_core/test/bus/unregister/idempotent"

            def cb(t, p):
                pass

            # Never registered — unobserve must return False, not raise
            removed = self.internal_unobserve(topic, cb)
            c.expect(removed, False)

            self.internal_observe(topic, cb)
            try:
                # Register, unregister twice — second returns False
                r1 = self.internal_unobserve(topic, cb)
                r2 = self.internal_unobserve(topic, cb)
                c.expect(r1, True)
                c.expect(r2, False)
            finally:
                # Defensive cleanup — covers any path that left cb registered
                self.internal_unobserve(topic, cb)

        await rec.run_case(
            "bus.unregister.basic", body_basic,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )
        await rec.run_case(
            "bus.unregister.during_emit", body_during_emit,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )
        await rec.run_case(
            "bus.unregister.idempotent", body_idempotent,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )

    # ─────────────────────────────────────────────────────────────────
    # 3. FAST PATH (no observer)
    # ─────────────────────────────────────────────────────────────────

    async def _bus_fast_path(self, rec: CaseRecorder, kw: Dict) -> None:

        async def body(c):
            topic = "_core/test/bus/fast_path/no_observer"
            # Should be a no-op: no observers registered for topic.
            # Just confirm no exception raised, no side effects.
            self._plugin_core._internal_emit(topic, x=1)
            self._plugin_core._internal_emit(topic, x=2)
            self._plugin_core._internal_emit(topic, x=3)
            # Topic should not be added to _internal_observers as a side effect
            c.expect(topic in self._plugin_core._internal_observers, False)

        await rec.run_case(
            "bus.fast_path.no_observer", body,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )

    # ─────────────────────────────────────────────────────────────────
    # 4. ERROR (observer raises)
    # ─────────────────────────────────────────────────────────────────

    async def _bus_error(self, rec: CaseRecorder, kw: Dict) -> None:

        async def body(c):
            topic = "_core/test/bus/error/observer_raises"
            log_b: List = []

            def cb_a(t, p):
                raise RuntimeError("intentional observer error")

            def cb_b(t, p):
                log_b.append((t, p))

            cap = _LogCapture()
            cap.setLevel(logging.ERROR)
            pc_logger = self._plugin_core._logger
            try:
                pc_logger.addHandler(cap)
                self.internal_observe(topic, cb_a)
                self.internal_observe(topic, cb_b)
                # A raises, B still fires — exception swallowed per docstring.
                self._plugin_core._internal_emit(topic, n=1)
                c.expect(len(log_b), 1)
                # Verify the framework logged the swallowed exception.
                matches = [
                    r for r in cap.records
                    if "B-073 internal observer raised" in r.getMessage()
                ]
                if not matches:
                    raise AssertionError(
                        "expected ERROR log w/ 'B-073 internal observer raised'"
                    )
            finally:
                self.internal_unobserve(topic, cb_a)
                self.internal_unobserve(topic, cb_b)
                pc_logger.removeHandler(cap)

        await rec.run_case(
            "bus.error.observer_raises", body,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )

    # ─────────────────────────────────────────────────────────────────
    # 5. LOAD
    # ─────────────────────────────────────────────────────────────────

    async def _bus_load(self, rec: CaseRecorder, kw: Dict) -> None:

        async def body(c):
            topic = "_core/test/bus/load/1k_events"
            counter = [0]

            def cb(t, p):
                counter[0] += 1

            self.internal_observe(topic, cb)
            try:
                for i in range(1000):
                    self._plugin_core._internal_emit(topic, i=i)
                c.expect(counter[0], 1000)
            finally:
                self.internal_unobserve(topic, cb)

        await rec.run_case(
            "bus.load.1k_events", body,
            tags=("basic", "perf"), bug_ids=("B-073",), **kw,
        )

    # ─────────────────────────────────────────────────────────────────
    # 6. GUARD (recursion + depth-restored)
    # ─────────────────────────────────────────────────────────────────

    async def _bus_guard(self, rec: CaseRecorder, kw: Dict) -> None:

        async def body_recursion_depth(c):
            topic = "_core/test/bus/guard/recursion"
            counter = [0]

            def cb(t, p):
                counter[0] += 1
                # Re-emit same topic. Each level increments depth via
                # set(depth+1); when depth reaches _MAX_EMIT_DEPTH (5)
                # the next emit aborts at the guard check.
                self._plugin_core._internal_emit(t, n=counter[0])

            cap = _LogCapture()
            cap.setLevel(logging.WARNING)
            pc_logger = self._plugin_core._logger
            try:
                pc_logger.addHandler(cap)
                self.internal_observe(topic, cb)
                self._plugin_core._internal_emit(topic, n=0)
                # _MAX_EMIT_DEPTH nested observer invocations (depths
                # 1.._MAX_EMIT_DEPTH); the deepest observer's re-emit
                # attempt sees depth==_MAX_EMIT_DEPTH and is rejected
                # by the `depth >= _MAX_EMIT_DEPTH` guard.
                c.expect(counter[0], _MAX_EMIT_DEPTH)
                matches = [
                    r for r in cap.records
                    if "RECURSIVE EMIT DEPTH EXCEEDED" in r.getMessage()
                ]
                if not matches:
                    raise AssertionError(
                        "expected WARN log w/ 'RECURSIVE EMIT DEPTH EXCEEDED'"
                    )
            finally:
                self.internal_unobserve(topic, cb)
                pc_logger.removeHandler(cap)

        async def body_depth_restored(c):
            topic_outer = "_core/test/bus/guard/depth/outer"
            topic_inner = "_core/test/bus/guard/depth/inner"
            depths_seen = {"outer": None, "inner": None}

            def cb_inner(t, p):
                depths_seen["inner"] = _EMIT_DEPTH.get()

            def cb_outer(t, p):
                depths_seen["outer"] = _EMIT_DEPTH.get()
                # Nested emit on a different topic. Inner observer
                # sees depth==2; after inner emit returns control,
                # outer's depth is back to its original value.
                self._plugin_core._internal_emit(topic_inner, kind="inner")

            self.internal_observe(topic_outer, cb_outer)
            self.internal_observe(topic_inner, cb_inner)
            try:
                pre = _EMIT_DEPTH.get()
                self._plugin_core._internal_emit(topic_outer, kind="outer")
                post = _EMIT_DEPTH.get()
                # Inside outer observer: depth == 1
                c.expect(depths_seen["outer"], 1)
                # Inside nested inner observer: depth == 2
                c.expect(depths_seen["inner"], 2)
                # Post-emit: depth restored via reset(token) to pre-emit value
                c.expect(post, pre)
            finally:
                self.internal_unobserve(topic_outer, cb_outer)
                self.internal_unobserve(topic_inner, cb_inner)

        await rec.run_case(
            "bus.guard.recursion_depth", body_recursion_depth,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )
        await rec.run_case(
            "bus.guard.depth_restored", body_depth_restored,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )

    # ─────────────────────────────────────────────────────────────────
    # 7. CLEANUP (pop_plugin → _unobserve_plugin)
    # ─────────────────────────────────────────────────────────────────

    async def _bus_cleanup(self, rec: CaseRecorder, kw: Dict) -> None:

        async def body(c):
            pc = self._plugin_core
            fake_uuid = f"_test_fake_uuid_{_uuid.uuid4().hex}"
            topics = [
                "_core/test/bus/cleanup/topic_a",
                "_core/test/bus/cleanup/topic_b",
                "_core/test/bus/cleanup/topic_c",
            ]
            cbs = [
                lambda t, p: None,
                lambda t, p: None,
                lambda t, p: None,
            ]

            try:
                for tp, cb in zip(topics, cbs):
                    pc.internal_observe(fake_uuid, tp, cb)

                # Pre-state: owner entry present with all 3 pairs
                owned = pc._observer_owners.get(fake_uuid, set())
                c.expect(len(owned), 3)
                for tp in topics:
                    if tp not in pc._internal_observers:
                        raise AssertionError(
                            f"topic {tp} missing from _internal_observers pre-cleanup"
                        )

                # Trigger bulk cleanup
                removed = pc._unobserve_plugin(fake_uuid)
                c.expect(removed, 3)

                # Post-state: owner entry gone, topics cleaned
                c.expect(fake_uuid in pc._observer_owners, False)
                for tp in topics:
                    if tp in pc._internal_observers:
                        raise AssertionError(
                            f"topic {tp} not cleaned from _internal_observers"
                        )
            finally:
                # Defensive — re-call cleanup in case mid-body assert left state
                pc._unobserve_plugin(fake_uuid)

        await rec.run_case(
            "bus.cleanup.pop_plugin", body,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )

    # ─────────────────────────────────────────────────────────────────
    # 8. EVICTION
    # ─────────────────────────────────────────────────────────────────

    async def _eviction(self, rec: CaseRecorder, kw: Dict) -> None:
        pc = self._plugin_core

        async def _wait_for_eviction(pre: int, max_settle_s: float = 1.0) -> int:
            """Poll-loop: wait until ``len(pc.requests)`` returns to ``pre``,
            up to ``max_settle_s``. Returns the final length. More robust
            than a fixed sleep on loaded CI / Windows scheduler jitter.
            """
            deadline = max_settle_s
            elapsed = 0.0
            step = 0.02
            while elapsed < deadline:
                if len(pc.requests) == pre:
                    return pre
                await asyncio.sleep(step)
                elapsed += step
            return len(pc.requests)

        async def body_request_completed(c):
            pre = len(pc.requests)
            r = await self.execute(EXEC_TARGET, "ea_add", (2, 3))
            c.expect(r, 5)
            # execute()'s own consumer-side finally pops at PluginCore.py:4418
            # before await returns; no settle needed.
            post = len(pc.requests)
            if post != pre:
                raise AssertionError(
                    f"Request retained: pre={pre} post={post}"
                )

        async def body_gen_request_completed(c):
            pre = len(pc.requests)
            chunks = []
            async for chunk in self.execute_stream(
                STREAM_TARGET, "ea_gen", {"n": 5, "prefix": "x", "delay_ms": 0},
            ):
                chunks.append(chunk)
            c.expect(chunks, ["x0", "x1", "x2", "x3", "x4"])
            # GeneratorRequest: consumer's set_collected doesn't pop;
            # producer-side _process_request_stream finally at line 4053
            # pops on producer task exit. Poll-loop is robust against
            # task-scheduler jitter.
            post = await _wait_for_eviction(pre, max_settle_s=1.0)
            if post != pre:
                raise AssertionError(
                    f"GeneratorRequest retained: pre={pre} post={post}"
                )

        async def body_event_stream_completed(c):
            pre = len(pc.requests)
            chunks = []
            async for chunk in self.request_event_stream(
                "bus_stream_event", payload=None, timeout=5.0,
            ):
                chunks.append(chunk)
            # handle_bus_stream yields 0, 1, 2. First chunk is wrapped
            # in Event (PR3 LOCKED #2); chunks[1:] are the bare ints.
            c.expect(len(chunks), 3)
            c.expect(isinstance(chunks[0], Event), True)
            c.expect(chunks[0].payload, 0)
            c.expect(chunks[1:], [1, 2])
            # Producer task wind-down through event-stream notifier path
            # takes longer than execute_stream — poll up to 2s.
            post = await _wait_for_eviction(pre, max_settle_s=2.0)
            if post != pre:
                raise AssertionError(
                    f"GeneratorRequest (event_stream) retained: pre={pre} post={post}"
                )

        async def body_load_no_retention(c):
            # Pre-quiescent settle so prior cases' GeneratorRequest
            # producer wind-downs have fully evicted before we read pre.
            await asyncio.sleep(0.1)
            pre = len(pc.requests)

            for i in range(1000):
                r = await self.execute(EXEC_TARGET, "ea_add", (i, 1))
                if r != i + 1:
                    raise AssertionError(f"unexpected result at i={i}: {r}")

            # Post-burst poll for any deferred producer-task finallys.
            post = await _wait_for_eviction(pre, max_settle_s=1.0)
            if post != pre:
                raise AssertionError(
                    f"requests dict leaked: pre={pre} post={post} after 1000 execute()"
                )

        await rec.run_case(
            "eviction.request.completed", body_request_completed,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )
        await rec.run_case(
            "eviction.gen_request.completed", body_gen_request_completed,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )
        await rec.run_case(
            "eviction.event_stream.completed", body_event_stream_completed,
            tags=("basic",), bug_ids=("B-073",), **kw,
        )
        await rec.run_case(
            "eviction.load.no_retention", body_load_no_retention,
            tags=("basic", "perf"), bug_ids=("B-073",),
            slow=True, hard_timeout_s=20.0, **kw,
        )
