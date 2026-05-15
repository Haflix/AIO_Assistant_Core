"""TestRuntimeToggleSuite — Phase 2a coverage for the runtime
sub/event enable-toggle API + the ``kind=`` field added to
``_core/request/started`` and ``_core/request/completed`` emits.

Categories:
  1. registry_level  — TopicRegistry.set_subscription_enabled atomic + unknown_uuid
  2. pc_subscription — Plexus.set_subscription_enabled wrapper
  3. pc_event        — Plexus.set_event_enabled wrapper
  4. kind_field      — kind=request.kind on _core/request/* emits
  5. dispatch        — disabled subs/events drop at dispatch time
  6. emit_depth      — chained toggle stays inside the emit-depth budget

The suite leans on two YAML-declared bindings (see plugin_config.yml):
  - event ``runtime_toggle_event`` with topic ``trts/runtime_toggle``
  - subscription ``runtime_toggle_dispatch_sub`` matching the same topic
    via ``handle_runtime_toggle_event`` (no-op handler)

Tests that need additional subs create them at runtime via
``pc.topic_registry.subscribe`` and clean up via ``unsubscribe`` in
finally blocks.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402
import time  # noqa: E402
import uuid as _uuid  # noqa: E402
from typing import Any, Dict, List, Optional, Tuple  # noqa: E402

from plexus.utils import Plugin, Event  # noqa: E402
from plexus.decorators import async_log_errors, log_errors  # noqa: E402

from _test_helpers import CaseRecorder  # noqa: E402

# Module-level ContextVar imported so the emit_depth case can read the
# depth observed inside a recursive observer call.
from plexus.core import _EMIT_DEPTH, _MAX_EMIT_DEPTH  # noqa: E402


SUITE_VERSION = "0.1.0"

EXEC_TARGET = "TestExecuteTarget"
RUNTIME_TOGGLE_EVENT_ID = "runtime_toggle_event"
RUNTIME_TOGGLE_TOPIC = "trts/runtime_toggle"


class TestRuntimeToggleSuite(Plugin):
    """Phase 2a runtime-toggle API regression suite."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self._dispatch_count: int = 0

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestRuntimeToggleSuite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestRuntimeToggleSuite disabled")

    @async_log_errors
    async def handle_runtime_toggle_event(self, event: Event):
        """No-op handler. Counts dispatches so dispatch.* cases can read
        the count if needed (target_count from _core/event/published is
        the primary assertion path; this counter is a defensive backup).
        """
        self._dispatch_count += 1

    # ─────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────

    async def _make_runtime_sub(
        self,
        topic: str,
        target_access_name: str = "handle_runtime_toggle_event",
        *,
        enabled: bool = True,
        hosts: str = "local",
    ) -> str:
        """Register a runtime subscription owned by THIS plugin so it
        cleans up automatically on suite disable. Returns sub_uuid.
        """
        return await self._plexus.topic_registry.subscribe(
            topic_pattern=topic,
            plugin_name=self.plugin_name,
            plugin_uuid=self.plugin_uuid,
            target_plugin=self.plugin_name,
            target_access_name=target_access_name,
            hosts=hosts,
            enabled=enabled,
        )

    async def _drop_runtime_sub(self, sub_uuid: Optional[str]) -> None:
        if sub_uuid is None:
            return
        try:
            await self._plexus.topic_registry.unsubscribe(sub_uuid)
        except Exception:
            pass

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
        rec = CaseRecorder("TestRuntimeToggleSuite", SUITE_VERSION, self._plexus)
        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=False,
        )

        await self._registry_level(rec, kw)
        await self._pc_subscription(rec, kw)
        await self._pc_event(rec, kw)
        await self._kind_field(rec, kw)
        await self._dispatch(rec, kw)
        await self._emit_depth(rec, kw)

        return rec.to_dict()

    # ─────────────────────────────────────────────────────────────────
    # 1. REGISTRY LEVEL
    # ─────────────────────────────────────────────────────────────────

    async def _registry_level(self, rec: CaseRecorder, kw: Dict) -> None:
        pc = self._plexus
        reg = pc.topic_registry

        async def body_atomic(c):
            """Test #35 — TopicRegistry.set_subscription_enabled mutates
            inside the lock and returns (sub, changed) correctly.
            """
            uuid = await self._make_runtime_sub(RUNTIME_TOGGLE_TOPIC)
            try:
                # 1. Toggle True → False (real change)
                sub_obj, changed = await reg.set_subscription_enabled(uuid, False)
                c.expect(changed, True)
                c.expect(sub_obj is not None, True)
                c.expect(sub_obj.enabled, False)
                c.expect(sub_obj.sub_uuid, uuid)

                # 2. Idempotent: False → False (no change)
                sub_obj2, changed2 = await reg.set_subscription_enabled(uuid, False)
                c.expect(changed2, False)
                c.expect(sub_obj2 is sub_obj, True)  # same registry entry
                c.expect(sub_obj2.enabled, False)

                # 3. Flip back True (real change)
                sub_obj3, changed3 = await reg.set_subscription_enabled(uuid, True)
                c.expect(changed3, True)
                c.expect(sub_obj3.enabled, True)
            finally:
                await self._drop_runtime_sub(uuid)

        async def body_unknown(c):
            """Test #36 — unknown uuid returns (None, False)."""
            fake_uuid = _uuid.uuid4().hex
            sub_obj, changed = await reg.set_subscription_enabled(
                fake_uuid, True
            )
            c.expect(sub_obj, None)
            c.expect(changed, False)

        await rec.run_case(
            "registry.set_enabled.atomic", body_atomic,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "registry.set_enabled.unknown_uuid", body_unknown,
            tags=("basic",), bug_ids=(), **kw,
        )

    # ─────────────────────────────────────────────────────────────────
    # 2. PLUGINCORE WRAPPER — SUBSCRIPTION
    # ─────────────────────────────────────────────────────────────────

    async def _pc_subscription(self, rec: CaseRecorder, kw: Dict) -> None:
        pc = self._plexus

        async def body_toggles_flag(c):
            """Test #37 — wrapper mutates Subscription.enabled + returns True."""
            uuid = await self._make_runtime_sub(RUNTIME_TOGGLE_TOPIC)
            try:
                # True → False
                result = await pc.set_subscription_enabled(uuid, False)
                c.expect(result, True)
                sub = await pc.topic_registry.get_subscription(uuid)
                c.expect(sub.enabled, False)
                # False → True
                result2 = await pc.set_subscription_enabled(uuid, True)
                c.expect(result2, True)
                sub2 = await pc.topic_registry.get_subscription(uuid)
                c.expect(sub2.enabled, True)
            finally:
                await self._drop_runtime_sub(uuid)

        async def body_emits_bus_event(c):
            """Test #38 — _core/subscription/state_changed fires with
            correct payload on a real toggle.
            """
            uuid = await self._make_runtime_sub(RUNTIME_TOGGLE_TOPIC)
            captured: List[Tuple[str, dict]] = []

            def cb(topic, payload):
                captured.append((topic, dict(payload)))  # defensive copy

            self.internal_observe("_core/subscription/state_changed", cb)
            try:
                await pc.set_subscription_enabled(uuid, False)
                # Single emit captured
                c.expect(len(captured), 1)
                topic, payload = captured[0]
                c.expect(topic, "_core/subscription/state_changed")
                c.expect(payload.get("sub_uuid"), uuid)
                c.expect(payload.get("enabled"), False)
                # ts present + reasonable
                ts = payload.get("ts")
                c.expect(isinstance(ts, float), True)
            finally:
                self.internal_unobserve("_core/subscription/state_changed", cb)
                await self._drop_runtime_sub(uuid)

        async def body_unknown_uuid(c):
            """Test #39 — wrapper returns False for an unknown uuid.
            Plus cycle 3 LOW-7 defensive guard: observe
            ``_core/subscription/state_changed`` and assert NO emit
            fires (a future regression that mutated state on a
            fallback path before the None short-circuit would still
            return False but emit — this catches that).
            """
            fake_uuid = _uuid.uuid4().hex
            captured: List = []

            def cb(topic, payload):
                if payload.get("sub_uuid") == fake_uuid:
                    captured.append(payload)

            self.internal_observe("_core/subscription/state_changed", cb)
            try:
                result = await pc.set_subscription_enabled(fake_uuid, True)
                c.expect(result, False)
                c.expect(len(captured), 0)
            finally:
                self.internal_unobserve("_core/subscription/state_changed", cb)

        async def body_idempotent(c):
            """Test #40 — second call with same value returns True
            without re-broadcasting or re-emitting.
            """
            uuid = await self._make_runtime_sub(RUNTIME_TOGGLE_TOPIC)
            captured: List = []

            def cb(topic, payload):
                captured.append(topic)

            self.internal_observe("_core/subscription/state_changed", cb)
            try:
                # First call: True → True (no-op)
                result = await pc.set_subscription_enabled(uuid, True)
                c.expect(result, True)
                c.expect(len(captured), 0)  # no emit
                # Real flip to bring some emit baseline
                await pc.set_subscription_enabled(uuid, False)
                c.expect(len(captured), 1)
                # Second no-op: False → False
                result2 = await pc.set_subscription_enabled(uuid, False)
                c.expect(result2, True)
                c.expect(len(captured), 1)  # STILL 1 — no extra emit
            finally:
                self.internal_unobserve("_core/subscription/state_changed", cb)
                await self._drop_runtime_sub(uuid)

        async def body_concurrent(c):
            """Test #41 — two concurrent calls with opposite values.
            Registry-level mutation serializes through the lock; final
            state is whichever task acquired the lock last. Either
            ordering is valid. Assertions tightened post-cycle-1 code
            review (F1/M2 from reviewer cycle): the original
            ``in (True, False)`` was a tautology that passed for any
            value of ``sub.enabled`` including non-bool corruption.

            We assert: (a) no exceptions, (b) both calls succeeded
            (both return True since both found the sub), (c) the final
            value is a strict Python ``bool`` (not int/None/string),
            (d) 1 or 2 ``_core/subscription/state_changed`` emits fire
            (depending on lock-acquisition order — see body comments
            for the case analysis), and (e) the final registry state
            appears at least once in the captured emits (set-membership,
            not strict "last == final" — broadcast latency can reorder
            emits across the two concurrent tasks when peer adverts
            exist; cycle 4 M1 fix).
            """
            uuid = await self._make_runtime_sub(RUNTIME_TOGGLE_TOPIC)
            captured: List[bool] = []

            def cb(topic, payload):
                captured.append(payload.get("enabled"))

            self.internal_observe("_core/subscription/state_changed", cb)
            try:
                # Schedule both concurrently. asyncio.gather doesn't
                # guarantee execution order; both are valid orderings.
                results = await asyncio.gather(
                    pc.set_subscription_enabled(uuid, True),
                    pc.set_subscription_enabled(uuid, False),
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, BaseException):
                        raise AssertionError(
                            f"concurrent toggle raised: {type(r).__name__}: {r}"
                        )
                # Both calls must return True (sub found).
                c.expect(results[0], True)
                c.expect(results[1], True)
                sub = await pc.topic_registry.get_subscription(uuid)
                # Strict bool — not int, not None, not string.
                c.expect(isinstance(sub.enabled, bool), True)
                # Final state is one of the two valid orderings.
                if sub.enabled not in (True, False):
                    raise AssertionError(
                        f"sub.enabled {sub.enabled!r} not bool"
                    )
                # Emit count: initial sub state is True. set(True) no-ops
                # if it runs FIRST (state already True); set(False) flips
                # → 1 emit. If set(False) runs FIRST, it flips; set(True)
                # then flips back → 2 emits. So count is 1 or 2.
                if len(captured) not in (1, 2):
                    raise AssertionError(
                        f"expected 1 or 2 state_changed emits; got "
                        f"{len(captured)}: {captured}"
                    )
                # Registry final state must equal one of the captured
                # emit values (i.e. an emit DID fire reflecting that
                # state at some point). We CANNOT assert ``captured[-1]
                # == sub.enabled`` strictly because broadcasts happen
                # OUTSIDE the registry lock — when real peer adverts
                # are present, broadcast latency interleaves with the
                # second task's mutation+emit. Cycle 4 M1 fix:
                # downgrade the strict "last emit matches" check to a
                # set-membership invariant ("final state was emitted at
                # least once across the captured set").
                if sub.enabled not in captured:
                    raise AssertionError(
                        f"final registry enabled={sub.enabled} not in "
                        f"captured emits {captured}"
                    )
            finally:
                self.internal_unobserve("_core/subscription/state_changed", cb)
                await self._drop_runtime_sub(uuid)

        async def body_emit_after_broadcast(c):
            """Test #54 (post-cycle-1 review tightened): the emit must
            fire AFTER the broadcast call has returned. The original
            assertion only compared timestamps against the call window,
            which trivially passes when the broadcast path is skipped
            (no peers configured → ``nm.is_ready`` False).

            This version monkey-patches the NetworkManager's broadcast
            helper to record its invocation timestamp, then asserts
            ``broadcast_ts < emit_ts``. When networking is not ready,
            the test still verifies the emit fires inside the call
            window but ALSO asserts no broadcast attempt was made
            (so the implementation cannot silently swap order without
            our notice).
            """
            uuid = await self._make_runtime_sub(RUNTIME_TOGGLE_TOPIC)
            captured_emit_ts: List[float] = []
            captured_broadcast_ts: List[float] = []

            def cb(topic, payload):
                captured_emit_ts.append(payload.get("ts", 0.0))

            self.internal_observe("_core/subscription/state_changed", cb)
            nm = pc.network
            original_broadcast = None
            networking_ready = (
                getattr(pc, "networking_enabled", False)
                and nm is not None
                and getattr(nm, "is_ready", False)
            )
            try:
                # Install spy whenever an ``nm`` exists — even if not
                # ``is_ready`` — so the test can verify the gate IS
                # enforced (if implementation bypasses the is_ready
                # check and calls broadcast on a not-ready NM, the spy
                # records it and the assertion below fires). cycle 3
                # MEDIUM-1 fix: the cycle 2 version skipped spy install
                # when not-ready, making the "no broadcast happened"
                # assertion vacuous. When nm itself is None, there's
                # nothing to spy on — skip the case to avoid pretending
                # the gate was tested.
                if nm is None:
                    c.skip(
                        "set_subscription_enabled.emit_after_broadcast: "
                        "no NetworkManager (networking disabled at boot) — "
                        "broadcast-path ordering cannot be exercised; "
                        "skip rather than trivially pass"
                    )
                    return

                original_broadcast = nm.broadcast_local_sub_removed

                async def spy(sub, *, _target_uuid=uuid, _orig=original_broadcast):
                    # Filter by sub_uuid so concurrent broadcasts from
                    # unrelated paths (peer-driven flows, other suites'
                    # teardowns) don't pollute the capture list (cycle 2
                    # MEDIUM-1).
                    if getattr(sub, "sub_uuid", None) == _target_uuid:
                        captured_broadcast_ts.append(time.time())
                    return await _orig(sub)

                nm.broadcast_local_sub_removed = spy

                t_before = time.time()
                await pc.set_subscription_enabled(uuid, False)
                t_after = time.time()

                c.expect(len(captured_emit_ts), 1)
                emit_ts = captured_emit_ts[0]
                if not (t_before <= emit_ts <= t_after):
                    raise AssertionError(
                        f"emit ts {emit_ts} outside window "
                        f"[{t_before}, {t_after}]"
                    )

                if networking_ready:
                    # Networking up: broadcast WAS called; ordering
                    # invariant must hold (broadcast_ts <= emit_ts).
                    if len(captured_broadcast_ts) != 1:
                        raise AssertionError(
                            f"expected exactly 1 broadcast call; got "
                            f"{len(captured_broadcast_ts)}"
                        )
                    bts = captured_broadcast_ts[0]
                    if not (bts <= emit_ts):
                        raise AssertionError(
                            f"broadcast ts {bts} > emit ts {emit_ts} — "
                            f"emit fired BEFORE broadcast (order regression)"
                        )
                else:
                    # Networking not ready but nm exists: spy WAS
                    # installed. Implementation MUST respect the
                    # ``is_ready`` gate at core.py:6712-6716 and
                    # skip the broadcast call. If captured_broadcast_ts
                    # is non-empty, the gate was bypassed — real bug.
                    if captured_broadcast_ts:
                        raise AssertionError(
                            f"broadcast called despite nm.is_ready=False; "
                            f"gate bypassed: captured {captured_broadcast_ts}"
                        )
            finally:
                if original_broadcast is not None and nm is not None:
                    nm.broadcast_local_sub_removed = original_broadcast
                self.internal_unobserve("_core/subscription/state_changed", cb)
                await self._drop_runtime_sub(uuid)

        async def body_noop_no_emit(c):
            """Test #56 — explicit no-emit guard. Companion to #40 —
            specifically asserts the observer count does NOT increase
            on a no-op call.
            """
            uuid = await self._make_runtime_sub(RUNTIME_TOGGLE_TOPIC)
            captured: List = []

            def cb(topic, payload):
                captured.append(payload)

            self.internal_observe("_core/subscription/state_changed", cb)
            try:
                # Already True; call with True → no-op
                result = await pc.set_subscription_enabled(uuid, True)
                c.expect(result, True)
                c.expect(len(captured), 0)
            finally:
                self.internal_unobserve("_core/subscription/state_changed", cb)
                await self._drop_runtime_sub(uuid)

        await rec.run_case(
            "pc.set_sub_enabled.toggles_flag", body_toggles_flag,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "pc.set_sub_enabled.emits_bus_event", body_emits_bus_event,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "pc.set_sub_enabled.unknown_uuid", body_unknown_uuid,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "pc.set_sub_enabled.idempotent", body_idempotent,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "pc.set_sub_enabled.concurrent", body_concurrent,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "pc.set_sub_enabled.emit_after_broadcast", body_emit_after_broadcast,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "pc.set_sub_enabled.noop_no_emit", body_noop_no_emit,
            tags=("basic",), bug_ids=(), **kw,
        )

    # ─────────────────────────────────────────────────────────────────
    # 3. PLUGINCORE WRAPPER — EVENT
    # ─────────────────────────────────────────────────────────────────

    async def _pc_event(self, rec: CaseRecorder, kw: Dict) -> None:
        pc = self._plexus

        async def body_toggles_flag(c):
            """Test #42 — wrapper mutates plugin.events[id]["enabled"]."""
            # Ensure starting state True
            self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"] = True
            try:
                # True → False
                result = await pc.set_event_enabled(
                    self.plugin_name, RUNTIME_TOGGLE_EVENT_ID, False
                )
                c.expect(result, True)
                c.expect(self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"], False)
                # False → True
                result2 = await pc.set_event_enabled(
                    self.plugin_name, RUNTIME_TOGGLE_EVENT_ID, True
                )
                c.expect(result2, True)
                c.expect(self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"], True)
            finally:
                self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"] = True

        async def body_emits_bus_event(c):
            """Test #43 — _core/event/state_changed fires with correct payload."""
            self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"] = True
            captured: List[Tuple[str, dict]] = []

            def cb(topic, payload):
                captured.append((topic, dict(payload)))

            self.internal_observe("_core/event/state_changed", cb)
            try:
                await pc.set_event_enabled(
                    self.plugin_name, RUNTIME_TOGGLE_EVENT_ID, False
                )
                c.expect(len(captured), 1)
                topic, payload = captured[0]
                c.expect(topic, "_core/event/state_changed")
                c.expect(payload.get("plugin_name"), self.plugin_name)
                c.expect(payload.get("event_id"), RUNTIME_TOGGLE_EVENT_ID)
                c.expect(payload.get("enabled"), False)
                c.expect(isinstance(payload.get("ts"), float), True)
            finally:
                self.internal_unobserve("_core/event/state_changed", cb)
                self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"] = True

        async def body_unknown_id(c):
            """Test #44 — unknown plugin OR unknown event_id returns
            False. Plus cycle 3 LOW-7 defensive guard: observe
            ``_core/event/state_changed`` and assert NO emit fires on
            either failure path.
            """
            captured: List = []

            def cb(topic, payload):
                captured.append(payload)

            self.internal_observe("_core/event/state_changed", cb)
            try:
                r1 = await pc.set_event_enabled(
                    "NoSuchPlugin", "anything", True
                )
                c.expect(r1, False)
                r2 = await pc.set_event_enabled(
                    self.plugin_name, "no_such_event_id", True
                )
                c.expect(r2, False)
                c.expect(len(captured), 0)
            finally:
                self.internal_unobserve("_core/event/state_changed", cb)

        async def body_idempotent(c):
            """Test #45 — no-op call returns True without emitting."""
            self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"] = True
            captured: List = []

            def cb(topic, payload):
                captured.append(topic)

            self.internal_observe("_core/event/state_changed", cb)
            try:
                # True → True (no-op)
                result = await pc.set_event_enabled(
                    self.plugin_name, RUNTIME_TOGGLE_EVENT_ID, True
                )
                c.expect(result, True)
                c.expect(len(captured), 0)
            finally:
                self.internal_unobserve("_core/event/state_changed", cb)

        async def body_popped_plugin(c):
            """Test #46 — toggling an event on a popped plugin returns
            False (the lookup fails because the plugin is gone from
            self.plugins).
            """
            # Use a fake plugin name that doesn't exist in self.plugins
            # — equivalent to "plugin was popped" at the boundary of
            # the TOCTOU window. The real race (mid-call pop) is not
            # deterministically testable from suite code; this asserts
            # the boundary check fires correctly.
            fake_plugin = f"PoppedPlugin_{_uuid.uuid4().hex[:8]}"
            result = await pc.set_event_enabled(
                fake_plugin, "any_event_id", True
            )
            c.expect(result, False)

        await rec.run_case(
            "pc.set_event_enabled.toggles_flag", body_toggles_flag,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "pc.set_event_enabled.emits_bus_event", body_emits_bus_event,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "pc.set_event_enabled.unknown_id", body_unknown_id,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "pc.set_event_enabled.idempotent", body_idempotent,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "pc.set_event_enabled.popped_plugin", body_popped_plugin,
            tags=("basic",), bug_ids=(), **kw,
        )

    # ─────────────────────────────────────────────────────────────────
    # 4. KIND= FIELD ON _core/request/*
    # ─────────────────────────────────────────────────────────────────

    async def _kind_field(self, rec: CaseRecorder, kw: Dict) -> None:
        pc = self._plexus

        async def body_started_includes_kind(c):
            """Test #47 — fire execute() + publish_event + request_event,
            verify ``kind`` field present and correct on _core/request/started.
            """
            captured: List[Tuple[str, dict]] = []

            def cb(topic, payload):
                captured.append((topic, dict(payload)))

            self.internal_observe("_core/request/started", cb)
            try:
                # 1. execute() — kind="execute"
                await self.execute(EXEC_TARGET, "ea_add", (2, 3))
                # 2. publish_event with at least one matching sub
                #    (runtime_toggle_dispatch_sub from YAML). Per-sub
                #    fan-out creates a Request with kind="publish_event".
                self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"] = True
                await self.publish_event(RUNTIME_TOGGLE_EVENT_ID, payload={"x": 1})
                # 3. request_event — kind="request_event"
                try:
                    await self.request_event(RUNTIME_TOGGLE_EVENT_ID, payload={"y": 2})
                except Exception:
                    # request_event's handler is the no-op which returns
                    # None; that's fine. The kind emit still fires.
                    pass

                # Settle: per-sub fan-out tasks run as asyncio tasks.
                # Poll instead of fixed sleep so we don't flake on slow
                # CI (cycle-1 code-review L4 fix).
                deadline = 2.0
                step = 0.02
                elapsed = 0.0
                while elapsed < deadline:
                    kinds_so_far = {p.get("kind") for _t, p in captured}
                    if {"execute", "publish_event", "request_event"} <= kinds_so_far:
                        break
                    await asyncio.sleep(step)
                    elapsed += step

                # Pull out kinds seen
                kinds = [p.get("kind") for _t, p in captured]
                if "execute" not in kinds:
                    raise AssertionError(
                        f"missing kind=execute in {kinds}"
                    )
                if "publish_event" not in kinds:
                    raise AssertionError(
                        f"missing kind=publish_event in {kinds}"
                    )
                if "request_event" not in kinds:
                    raise AssertionError(
                        f"missing kind=request_event in {kinds}"
                    )
            finally:
                self.internal_unobserve("_core/request/started", cb)
                self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"] = True

        async def body_completed_includes_kind(c):
            """Test #48 — same coverage for _core/request/completed."""
            captured: List[Tuple[str, dict]] = []

            def cb(topic, payload):
                captured.append((topic, dict(payload)))

            self.internal_observe("_core/request/completed", cb)
            try:
                await self.execute(EXEC_TARGET, "ea_add", (4, 5))
                self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"] = True
                await self.publish_event(RUNTIME_TOGGLE_EVENT_ID, payload={"x": 1})
                try:
                    await self.request_event(RUNTIME_TOGGLE_EVENT_ID, payload={"y": 2})
                except Exception:
                    pass

                # Poll for completed emits — cycle-1 code-review L4 fix.
                deadline = 2.0
                step = 0.02
                elapsed = 0.0
                while elapsed < deadline:
                    kinds_so_far = {p.get("kind") for _t, p in captured}
                    if {"execute", "publish_event", "request_event"} <= kinds_so_far:
                        break
                    await asyncio.sleep(step)
                    elapsed += step

                kinds = [p.get("kind") for _t, p in captured]
                if "execute" not in kinds:
                    raise AssertionError(f"missing kind=execute in {kinds}")
                if "publish_event" not in kinds:
                    raise AssertionError(f"missing kind=publish_event in {kinds}")
                if "request_event" not in kinds:
                    raise AssertionError(f"missing kind=request_event in {kinds}")
            finally:
                self.internal_unobserve("_core/request/completed", cb)
                self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"] = True

        async def body_existing_keys_preserved(c):
            """Test #49 — regression guard. After adding kind=, the
            pre-existing keys (request_id, plugin, method, author, ts)
            must still be present on _core/request/started. Locks the
            contract so a future refactor cannot strip them silently.

            Cycle 4 LOW-1 fix: filter captured emits by method so
            unrelated concurrent /started emits (other suites' traffic,
            internal framework calls) cannot pollute ``captured[0]``.
            """
            captured: List[dict] = []

            def cb(topic, payload):
                if payload.get("method") == "ea_add":
                    captured.append(dict(payload))

            self.internal_observe("_core/request/started", cb)
            try:
                await self.execute(EXEC_TARGET, "ea_add", (1, 1))
                if not captured:
                    raise AssertionError(
                        "no _core/request/started emitted for ea_add"
                    )
                payload = captured[0]
                for key in ("request_id", "plugin", "method", "author", "ts"):
                    if key not in payload:
                        raise AssertionError(
                            f"missing key {key!r} from /started payload {payload}"
                        )
                # And the new key:
                if "kind" not in payload:
                    raise AssertionError(
                        f"missing key 'kind' from /started payload {payload}"
                    )
            finally:
                self.internal_unobserve("_core/request/started", cb)

        async def body_completed_existing_keys_preserved(c):
            """Test #49b (cycle 2 LOW-1 addition) — parallel regression
            guard for _core/request/completed. Pre-existing keys
            (request_id, latency, error, ts) plus the new kind must be
            present. Symmetric with #49 — without this guard a refactor
            could strip latency or error from /completed without
            tripping any existing test.

            Cycle 4 LOW-2 fix: filter by request_id so unrelated
            /completed emits don't pollute ``captured[0]``. We learn
            the request_id from the matching /started emit.
            """
            captured_started: List[dict] = []
            captured_completed: List[dict] = []

            def cb_started(topic, payload):
                if payload.get("method") == "ea_add":
                    captured_started.append(dict(payload))

            def cb_completed(topic, payload):
                # Match by request_id once we know it from /started.
                if not captured_started:
                    return
                target_id = captured_started[0].get("request_id")
                if payload.get("request_id") == target_id:
                    captured_completed.append(dict(payload))

            self.internal_observe("_core/request/started", cb_started)
            self.internal_observe("_core/request/completed", cb_completed)
            try:
                await self.execute(EXEC_TARGET, "ea_add", (1, 1))
                # Settle to give /completed a chance to fire (the emit
                # happens in _process_request's finally block — sync
                # after the execute's await returns).
                deadline = 1.0
                step = 0.02
                elapsed = 0.0
                while elapsed < deadline and not captured_completed:
                    await asyncio.sleep(step)
                    elapsed += step
                if not captured_completed:
                    raise AssertionError(
                        "no _core/request/completed emitted for our ea_add"
                    )
                payload = captured_completed[0]
                for key in ("request_id", "latency", "error", "ts", "kind"):
                    if key not in payload:
                        raise AssertionError(
                            f"missing key {key!r} from /completed payload {payload}"
                        )
            finally:
                self.internal_unobserve("_core/request/started", cb_started)
                self.internal_unobserve("_core/request/completed", cb_completed)

        await rec.run_case(
            "kind_field.started_includes_kind", body_started_includes_kind,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "kind_field.completed_includes_kind", body_completed_includes_kind,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "kind_field.existing_keys_preserved", body_existing_keys_preserved,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "kind_field.completed_existing_keys_preserved",
            body_completed_existing_keys_preserved,
            tags=("basic",), bug_ids=(), **kw,
        )

    # ─────────────────────────────────────────────────────────────────
    # 5. DISPATCH READS THE FLAG
    # ─────────────────────────────────────────────────────────────────

    async def _dispatch(self, rec: CaseRecorder, kw: Dict) -> None:
        pc = self._plexus

        async def body_event_disabled_drops_publish(c):
            """Test #50 — set_event_enabled(False) makes publish_event
            return 0 (silent-drop on disabled event per core.py:5447).
            """
            self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"] = True
            try:
                # Sanity baseline: publish should reach the YAML sub
                n_before = await self.publish_event(
                    RUNTIME_TOGGLE_EVENT_ID, payload={"x": "before"}
                )
                if n_before < 1:
                    raise AssertionError(
                        f"baseline publish should match >=1 sub; got {n_before}"
                    )

                # Disable the event
                await pc.set_event_enabled(
                    self.plugin_name, RUNTIME_TOGGLE_EVENT_ID, False
                )
                n_disabled = await self.publish_event(
                    RUNTIME_TOGGLE_EVENT_ID, payload={"x": "disabled"}
                )
                c.expect(n_disabled, 0)

                # Re-enable: count returns
                await pc.set_event_enabled(
                    self.plugin_name, RUNTIME_TOGGLE_EVENT_ID, True
                )
                n_after = await self.publish_event(
                    RUNTIME_TOGGLE_EVENT_ID, payload={"x": "after"}
                )
                if n_after != n_before:
                    raise AssertionError(
                        f"after re-enable expected {n_before}, got {n_after}"
                    )
            finally:
                self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"] = True

        async def body_sub_disabled_drops_match(c):
            """Test #51 — set_subscription_enabled(False) makes the sub
            invisible to find_all. publish_event's target_count drops
            accordingly.
            """
            # Register a second matching sub at runtime so we can disable
            # one and verify target_count.
            second_uuid = await self._make_runtime_sub(RUNTIME_TOGGLE_TOPIC)
            try:
                self.events[RUNTIME_TOGGLE_EVENT_ID]["enabled"] = True
                # Baseline: YAML sub + runtime sub = 2 matches
                n_before = await self.publish_event(
                    RUNTIME_TOGGLE_EVENT_ID, payload={"x": "before"}
                )
                if n_before < 2:
                    raise AssertionError(
                        f"baseline expected >=2 matches; got {n_before}"
                    )

                # Disable the runtime sub
                ok = await pc.set_subscription_enabled(second_uuid, False)
                c.expect(ok, True)
                n_disabled = await self.publish_event(
                    RUNTIME_TOGGLE_EVENT_ID, payload={"x": "disabled"}
                )
                if n_disabled != n_before - 1:
                    raise AssertionError(
                        f"after disable expected {n_before - 1}; got {n_disabled}"
                    )

                # Re-enable: count returns
                await pc.set_subscription_enabled(second_uuid, True)
                n_after = await self.publish_event(
                    RUNTIME_TOGGLE_EVENT_ID, payload={"x": "after"}
                )
                if n_after != n_before:
                    raise AssertionError(
                        f"after re-enable expected {n_before}; got {n_after}"
                    )
            finally:
                await self._drop_runtime_sub(second_uuid)

        await rec.run_case(
            "dispatch.event_disabled_drops_publish",
            body_event_disabled_drops_publish,
            tags=("basic",), bug_ids=(), **kw,
        )
        await rec.run_case(
            "dispatch.sub_disabled_drops_match",
            body_sub_disabled_drops_match,
            tags=("basic",), bug_ids=(), **kw,
        )

    # ─────────────────────────────────────────────────────────────────
    # 6. EMIT DEPTH UNDER MAX
    # ─────────────────────────────────────────────────────────────────

    async def _emit_depth(self, rec: CaseRecorder, kw: Dict) -> None:
        pc = self._plexus

        async def body_under_max(c):
            """Test #55 — chained toggle should not extend the
            ``_EMIT_DEPTH`` chain toward ``_MAX_EMIT_DEPTH=5``. Post-
            cycle-1 review tightened to a concrete-value assertion (was
            ``<= 2``, too loose to catch regressions).

            Observer registered on ``_core/subscription/state_changed``;
            when the outer toggle fires its emit, the observer schedules
            an inner toggle on a different sub via
            ``asyncio.ensure_future`` (cannot directly await — sync
            observer contract). The inner toggle runs as a separate
            asyncio task, which copies the current Context at scheduling
            time (Python ContextVar semantics). The captured Context
            includes the outer emit's ``_EMIT_DEPTH=1`` token state.

            However, by the time the new task actually runs, the OUTER
            emit's ``_EMIT_DEPTH.reset(token)`` has executed in the
            original task — but the new task has its OWN Context copy
            with ``_EMIT_DEPTH=1`` still active. So when the inner
            ``set_subscription_enabled`` calls ``_internal_emit``, the
            inner emit reads ``_EMIT_DEPTH.get() = 1``, sets it to 2,
            and the inner observer sees depth=2.

            Expected: outer observer sees depth=1; inner observer sees
            depth=2. Strict assertion: ``observed_depths == [1, 2]``
            (in order). Any deviation indicates either a depth-tracking
            bug or a chain-extension regression.
            """
            outer_uuid = await self._make_runtime_sub(RUNTIME_TOGGLE_TOPIC)
            inner_uuid = await self._make_runtime_sub(RUNTIME_TOGGLE_TOPIC)
            observed_depths: List[int] = []
            inner_fired = {"v": False}

            def outer_observer(topic, payload):
                # Filter BEFORE appending depth — concurrent state_changed
                # emits from unrelated subs (other tests' teardowns, future
                # observers) must NOT pollute observed_depths and break the
                # strict [1, 2] assertion. cycle 3 LOW-3 fix.
                sub_uuid = payload.get("sub_uuid")
                if sub_uuid not in (outer_uuid, inner_uuid):
                    return
                observed_depths.append(_EMIT_DEPTH.get())
                if inner_fired["v"]:
                    return
                if sub_uuid != outer_uuid:
                    return
                inner_fired["v"] = True
                # Observer fires on loop thread already; no loop= kwarg
                # needed (deprecated since Python 3.10, removed in 3.12).
                # cycle 2 LOW-6 fix.
                try:
                    asyncio.ensure_future(
                        pc.set_subscription_enabled(inner_uuid, False),
                    )
                except RuntimeError:
                    pass  # no running loop (shutdown race) — drop silently

            self.internal_observe(
                "_core/subscription/state_changed", outer_observer
            )
            try:
                await pc.set_subscription_enabled(outer_uuid, False)
                # Wait for the scheduled inner task to land and fire
                # its emit. Poll loop instead of fixed sleep to be
                # robust to scheduler jitter on slow CI.
                deadline = 2.0
                step = 0.02
                elapsed = 0.0
                while elapsed < deadline and len(observed_depths) < 2:
                    await asyncio.sleep(step)
                    elapsed += step
                if len(observed_depths) < 2:
                    raise AssertionError(
                        f"timed out waiting for inner emit; observed "
                        f"depths={observed_depths}"
                    )
                # Strict invariant under current depth-tracking design:
                # outer observer sees depth=1, inner observer sees
                # depth=2 (ContextVar propagates across ensure_future).
                # Any other shape is a regression — either of the
                # depth bookkeeping in _internal_emit, OR of the
                # chain-extension boundary (e.g. ratcheting toward
                # _MAX_EMIT_DEPTH=5).
                c.expect(observed_depths[0], 1)
                c.expect(observed_depths[1], 2)
                max_depth = max(observed_depths)
                if max_depth >= _MAX_EMIT_DEPTH:
                    raise AssertionError(
                        f"chain reached _MAX_EMIT_DEPTH={_MAX_EMIT_DEPTH}; "
                        f"depths={observed_depths}"
                    )
            finally:
                self.internal_unobserve(
                    "_core/subscription/state_changed", outer_observer
                )
                await self._drop_runtime_sub(outer_uuid)
                await self._drop_runtime_sub(inner_uuid)

        await rec.run_case(
            "emit_depth.under_max", body_under_max,
            tags=("basic",), bug_ids=(), **kw,
        )
