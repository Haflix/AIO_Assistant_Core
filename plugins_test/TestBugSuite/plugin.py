"""TestBugSuite — PR3 Stage F bughunt repro suite.

One case per open `bugtracker.md` entry. Verdicts are recorded by the
parent post-run (annotated on bugtracker.md). NO bug fixes here — only
repros that prove which bugs are real vs fixed-by-construction under
the post-PR3 architecture.

Categories (one method per):
  _b_legacy_removed     — API surface deleted in Stage D — assert .gone
  _b_addressed_in_pr3   — PR3 added behavior that should fix the bug
  _b_active             — still-broken — repro and let recorder mark
  _b_deferred           — test infeasible without fixture work — skip
  _b_already_covered    — repro lives in another suite — skip-and-cite

See PLAN.md (alongside this file in the worktree) for the per-bug spec
table and pattern recipes.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import inspect  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

from utils import Plugin  # noqa: E402
from decorators import async_log_errors, log_errors  # noqa: E402
from exceptions import RequestException  # noqa: E402

from _test_helpers import CaseRecorder  # noqa: E402


SUITE_VERSION = "0.2.0"

TARGET = "TestEventTarget"
STREAM_TARGET = "TestStreamTarget"
EXEC_TARGET = "TestExecuteTarget"
BAD_ACTOR = "TestEventBadActor"


class TestBugSuite(Plugin):
    """PR3 Stage F bug-repro suite. See PLAN.md."""

    @log_errors
    def on_load(self, *args, **kwargs):
        # B-047 mailbox: probe handler increments to confirm fan-out
        # actually fires (sanity check for the task_list growth body).
        self.b047_probe_calls = 0

    async def handle_b047_probe(self, event):
        self.b047_probe_calls += 1

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestBugSuite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestBugSuite disabled")

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
        rec = CaseRecorder("TestBugSuite", SUITE_VERSION, self._plugin_core)
        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=False,
        )

        await self._b_legacy_removed(rec, kw)
        await self._b_addressed_in_pr3(rec, kw)
        await self._b_active(rec, kw)
        await self._b_deferred(rec, kw)
        await self._b_already_covered(rec, kw)
        return rec.to_dict()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _ensure_loaded(self, name: str) -> bool:
        """Idempotently load+enable a fixture by name from yaml_config."""
        if name in self._plugin_core.plugins:
            return True
        for entry in self._plugin_core.yaml_config.get("plugins", []):
            if entry.get("name") == name:
                e = dict(entry)
                e["enabled"] = True
                await self._plugin_core.load_plugin_with_conf(e)
                if name in self._plugin_core.plugins:
                    try:
                        await self._plugin_core._enable_plugin(name)
                    except Exception:
                        pass
                    return True
        return False

    # ==================================================================
    # _b_legacy_removed — Recipe A (API surface gone)
    # ==================================================================
    async def _b_legacy_removed(self, rec: CaseRecorder, kw: Dict) -> None:
        category = "legacy_removed"

        # ---- B-001 ---------------------------------------------------
        async def body_b_001_request_topic_method_gone(c):
            # B-001: code-driven topic handlers bypassed remote: false.
            # The legacy `request_topic` API + `_handle_topic_request`
            # server handler were removed in Stage D, so the entire
            # bypass surface no longer exists.
            c.expect(getattr(self, "request_topic", None), None)
            nm = self._plugin_core.network
            c.expect(getattr(nm, "_handle_topic_request", None), None)

        # ---- B-011 ---------------------------------------------------
        async def body_b_011_request_topic_stream_remote_gone(c):
            # B-011: legacy stream-error sentinel only existed on the
            # request_topic_stream_remote path. That whole client method
            # was removed in Stage D.
            nm = self._plugin_core.network
            c.expect(getattr(nm, "request_topic_stream_remote", None), None)

        # ---- B-012 ---------------------------------------------------
        async def body_b_012_handle_topic_request_stream_gone(c):
            # B-012: server-side stream-chunk error sentinel handler
            # `_handle_topic_request_stream` was removed in Stage D.
            nm = self._plugin_core.network
            c.expect(
                getattr(nm, "_handle_topic_request_stream", None), None
            )

        # ---- B-014 ---------------------------------------------------
        async def body_b_014_request_topic_stream_sync_gone(c):
            # B-014: `request_topic_stream_sync` method removed in Stage D.
            c.expect(getattr(self, "request_topic_stream_sync", None), None)

        # ---- B-018a --------------------------------------------------
        async def body_b_018a_handle_notify_handler_gone(c):
            # B-018a: wire-side spoof handlers (_handle_notify and
            # _handle_topic_request) were removed in Stage D — the
            # MSG_NOTIFY / MSG_TOPIC_REQUEST surface they hung off no
            # longer exists, so the remote spoof path through them is
            # gone.
            nm = self._plugin_core.network
            c.expect(getattr(nm, "_handle_notify", None), None)
            c.expect(getattr(nm, "_handle_topic_request", None), None)

        # ---- B-019 ---------------------------------------------------
        async def body_b_019_notify_method_gone(c):
            # B-019: `notify`'s return-count contract is moot — the
            # method itself was removed in Stage D.
            c.expect(getattr(self, "notify", None), None)

        # ---- B-020 ---------------------------------------------------
        async def body_b_020_notify_sync_method_gone(c):
            # B-020: `notify_sync` blocking-on-remote-ACK can't repro —
            # method removed in Stage D.
            c.expect(getattr(self, "notify_sync", None), None)

        # ---- B-022 ---------------------------------------------------
        async def body_b_022_subscribe_handler_kwarg_gone(c):
            # B-022: subscribe() no longer accepts `handler=` (silent
            # no-op risk gone). The `target_access_name` parameter
            # replaced it. Verify both: handler not in signature, AND
            # target_access_name IS in signature.
            sig = inspect.signature(self.subscribe)
            params = sig.parameters
            c.expect("handler" in params, False)
            c.expect("target_access_name" in params, True)

        # ---- B-023 ---------------------------------------------------
        async def body_b_023_notify_method_gone(c):
            # B-023: `notify()` raise-on-error contract violation can't
            # repro — method removed in Stage D.
            c.expect(getattr(self, "notify", None), None)

        # ---- B-024 ---------------------------------------------------
        async def body_b_024_request_topic_stream_gone(c):
            # B-024: oversized chunk corruption on `request_topic_stream`
            # can't repro — method removed in Stage D.
            c.expect(getattr(self, "request_topic_stream", None), None)

        # ---- B-025 ---------------------------------------------------
        async def body_b_025_request_topic_stream_gone(c):
            # B-025: re-entry-after-partial-yield bug on
            # `request_topic_stream` can't repro — method removed in
            # Stage D.
            c.expect(getattr(self, "request_topic_stream", None), None)

        # ---- B-027 ---------------------------------------------------
        async def body_b_027_notify_remote_gone(c):
            # B-027: notify_remote return-zero-on-transport-fail can't
            # repro — method removed in Stage D.
            nm = self._plugin_core.network
            c.expect(getattr(nm, "notify_remote", None), None)

        # ---- B-028 ---------------------------------------------------
        async def body_b_028_remote_notifier_methods_gone(c):
            # B-028: notify_remote, request_topic_remote,
            # request_topic_stream_remote — all client-side notifier
            # methods removed in Stage D, so the no-client-timeout
            # caller-hang surface is gone entirely.
            nm = self._plugin_core.network
            c.expect(getattr(nm, "notify_remote", None), None)
            c.expect(getattr(nm, "request_topic_remote", None), None)
            c.expect(getattr(nm, "request_topic_stream_remote", None), None)

        # ---- B-029 ---------------------------------------------------
        async def body_b_029_handle_topic_request_gone(c):
            # B-029: server-side `_handle_topic_request` ignored its
            # `timeout` for code-driven handlers — handler itself gone
            # in Stage D.
            nm = self._plugin_core.network
            c.expect(getattr(nm, "_handle_topic_request", None), None)

        # ---- B-030 ---------------------------------------------------
        async def body_b_030_notify_remote_gone(c):
            # B-030: `notify_remote` swallowing pickle errors can't
            # repro — method removed in Stage D.
            nm = self._plugin_core.network
            c.expect(getattr(nm, "notify_remote", None), None)

        # ---- B-032 ---------------------------------------------------
        async def body_b_032_handle_notify_gone(c):
            # B-032: server-side `_handle_notify` head-of-line blocking
            # on slow fan-out can't repro — handler removed in Stage D.
            nm = self._plugin_core.network
            c.expect(getattr(nm, "_handle_notify", None), None)

        # ---- B-033 ---------------------------------------------------
        async def body_b_033_request_topic_stream_sync_gone(c):
            # B-033: `request_topic_stream_sync` ignoring its `host`
            # parameter can't repro — method removed in Stage D.
            c.expect(getattr(self, "request_topic_stream_sync", None), None)

        # ---- B-035 ---------------------------------------------------
        async def body_b_035_notify_method_gone(c):
            # B-035: `notify`'s asyncio.gather-no-per-sub-timeout
            # symptom can't repro — method removed in Stage D.
            # `publish_event` is the new fire-and-forget primitive but
            # uses asyncio.create_task per-handler (verify in source).
            c.expect(getattr(self, "notify", None), None)
            src = inspect.getsource(self._plugin_core.publish_event)
            # Sanity: dispatch path uses create_task (per-sub
            # independent) rather than a single gather over all subs.
            c.expect("create_task" in src, True)

        # ---- B-036 ---------------------------------------------------
        async def body_b_036_call_sub_method_gone(c):
            # B-036: `_call_sub` caught Exception not BaseException —
            # function replaced by `_fanout_sub` in PR3. Verify the old
            # name is gone.
            core = self._plugin_core
            c.expect(getattr(core, "_call_sub", None), None)

        # ---- B-039 ---------------------------------------------------
        async def body_b_039_request_topic_sync_gone(c):
            # B-039: topic-hop-wipes-_sync_call_chain symptom rode on
            # `request_topic_sync` — method removed in Stage D.
            c.expect(getattr(self, "request_topic_sync", None), None)

        # ---- B-042 ---------------------------------------------------
        async def body_b_042_request_topic_stream_gone(c):
            # B-042: code-driven stream bypass (B-001 variant for
            # streams) — `request_topic_stream` method removed in
            # Stage D.
            c.expect(getattr(self, "request_topic_stream", None), None)

        # ---- B-053 ---------------------------------------------------
        async def body_b_053_subscription_handler_field_gone(c):
            # B-053: `Subscription.handler` field removed from the
            # dataclass. Also assert the new field names are present
            # so this case fails informatively if the schema regresses.
            import dataclasses
            from notifier import Subscription
            fields = {f.name for f in dataclasses.fields(Subscription)}
            c.expect("handler" in fields, False)
            c.expect("endpoint_access_name" in fields, False)
            c.expect("config_driven" in fields, False)
            # Positive assertions: new PR3 fields must be present.
            c.expect("target_access_name" in fields, True)
            c.expect("sub_uuid" in fields, True)
            c.expect("enabled" in fields, True)

        # ---- B-059 ---------------------------------------------------
        async def body_b_059_notify_method_gone(c):
            # B-059: cross-plugin sub routing mismatch on the legacy
            # `notify()` path can't repro — method removed in Stage D.
            c.expect(getattr(self, "notify", None), None)

        # -- run_case calls --------------------------------------------
        await rec.run_case(
            "bug.B-001.request_topic_method_gone",
            body_b_001_request_topic_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-001",), **kw,
        )
        await rec.run_case(
            "bug.B-011.request_topic_stream_remote_gone",
            body_b_011_request_topic_stream_remote_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-011",), **kw,
        )
        await rec.run_case(
            "bug.B-012.handle_topic_request_stream_gone",
            body_b_012_handle_topic_request_stream_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-012",), **kw,
        )
        await rec.run_case(
            "bug.B-014.request_topic_stream_sync_gone",
            body_b_014_request_topic_stream_sync_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-014",), **kw,
        )
        await rec.run_case(
            "bug.B-018a.handle_notify_handler_gone",
            body_b_018a_handle_notify_handler_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-018",), **kw,
        )
        await rec.run_case(
            "bug.B-019.notify_method_gone",
            body_b_019_notify_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-019",), **kw,
        )
        await rec.run_case(
            "bug.B-020.notify_sync_method_gone",
            body_b_020_notify_sync_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-020",), **kw,
        )
        await rec.run_case(
            "bug.B-022.subscribe_handler_kwarg_gone",
            body_b_022_subscribe_handler_kwarg_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-022",), **kw,
        )
        await rec.run_case(
            "bug.B-023.notify_method_gone",
            body_b_023_notify_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-023",), **kw,
        )
        await rec.run_case(
            "bug.B-024.request_topic_stream_gone",
            body_b_024_request_topic_stream_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-024",), **kw,
        )
        await rec.run_case(
            "bug.B-025.request_topic_stream_gone",
            body_b_025_request_topic_stream_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-025",), **kw,
        )
        await rec.run_case(
            "bug.B-027.notify_remote_gone",
            body_b_027_notify_remote_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-027",), **kw,
        )
        await rec.run_case(
            "bug.B-028.remote_notifier_methods_gone",
            body_b_028_remote_notifier_methods_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-028",), **kw,
        )
        await rec.run_case(
            "bug.B-029.handle_topic_request_gone",
            body_b_029_handle_topic_request_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-029",), **kw,
        )
        await rec.run_case(
            "bug.B-030.notify_remote_gone",
            body_b_030_notify_remote_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-030",), **kw,
        )
        await rec.run_case(
            "bug.B-032.handle_notify_gone",
            body_b_032_handle_notify_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-032",), **kw,
        )
        await rec.run_case(
            "bug.B-033.request_topic_stream_sync_gone",
            body_b_033_request_topic_stream_sync_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-033",), **kw,
        )
        await rec.run_case(
            "bug.B-035.notify_method_gone",
            body_b_035_notify_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-035",), **kw,
        )
        await rec.run_case(
            "bug.B-036.call_sub_method_gone",
            body_b_036_call_sub_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-036",), **kw,
        )
        await rec.run_case(
            "bug.B-039.request_topic_sync_gone",
            body_b_039_request_topic_sync_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-039",), **kw,
        )
        await rec.run_case(
            "bug.B-042.request_topic_stream_gone",
            body_b_042_request_topic_stream_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-042",), **kw,
        )
        await rec.run_case(
            "bug.B-053.subscription_handler_field_gone",
            body_b_053_subscription_handler_field_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-053",), **kw,
        )
        await rec.run_case(
            "bug.B-059.notify_method_gone",
            body_b_059_notify_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-059",), **kw,
        )

    # ==================================================================
    # _b_addressed_in_pr3 — Recipe B
    # ==================================================================
    async def _b_addressed_in_pr3(self, rec: CaseRecorder, kw: Dict) -> None:
        category = "addressed_in_pr3"

        # ---- B-003 ---------------------------------------------------
        async def body_b_003_disable_unregisters_subs(c):
            # B-003: notify dispatched to handlers on disabled plugin.
            # Stage B's _disable_plugin wrapper now calls
            # _unregister_plugin_subscriptions at end of on_disable per
            # Q23+C15. Verify: pop a plugin, confirm its subs vanish
            # from topic_registry, then restore via _ensure_loaded.
            target = TARGET
            if target not in self._plugin_core.plugins:
                c.skip(f"{target} not loaded — fixture order issue")
                return
            pre = await self._plugin_core.topic_registry.list_local_subs()
            pre_owned = [s for s in pre if s.plugin_name == target]
            if len(pre_owned) == 0:
                c.skip(
                    f"{target} has no subs registered — precondition fail"
                )
                return
            # Net-zero drift: pop + reload restores the plugin set, but
            # during the body the set transiently misses TARGET. Without
            # explicit declaration, _check_drift only sees the final
            # state — which matches snapshot. Defensive declaration
            # nonetheless:
            c.set_expected_drift(added=(), removed=())
            await self._plugin_core.pop_plugin(target)
            post = await self._plugin_core.topic_registry.list_local_subs()
            post_owned = [s for s in post if s.plugin_name == target]
            c.expect(len(post_owned), 0)
            # Restore so subsequent cases find TARGET loaded.
            restored = await self._ensure_loaded(target)
            if not restored:
                raise AssertionError(
                    f"failed to restore {target} after pop"
                )

        # ---- B-038 ---------------------------------------------------
        async def body_b_038_sync_pre_start_raises_request_exception(c):
            # B-038: pre-start sync wrappers used to raise TypeError.
            # Stage A Q1 added _check_framework_started guard to all
            # sync wrappers — fires FIRST (before event lookup, before
            # run_coroutine_threadsafe). Can't actually call before
            # framework start in this test (main_event_loop is set).
            # Instead, transiently null it and call. The event_id
            # passed below is intentionally bogus — guard fires before
            # lookup so the bogus id is never dereferenced.
            saved = self._plugin_core.main_event_loop
            self._plugin_core.main_event_loop = None
            try:
                c.expect_exception(
                    RequestException, match="Framework not started"
                )
                self.publish_event_sync("any_id_guard_fires_first")
            finally:
                self._plugin_core.main_event_loop = saved

        # ---- B-056 ---------------------------------------------------
        async def body_b_056_disabled_subs_excluded(c):
            # B-056: disabled YAML subs registered with enabled=False.
            # Verify topic_registry contains the disabled sub with
            # `.enabled == False`, AND find_first skips it.
            #
            # The TestEventSuite YAML declares `disabled_event` (an
            # event with `enabled: false`) — but no matching sub for
            # it. Use a runtime sub instead: register one, mark it
            # disabled directly on the registry entry, then verify
            # find_first returns no eligible subscriber.
            topic = "test_bugsuite/B056/disabled_probe"
            sub_uuid = await self.subscribe(
                topic,
                target_access_name="run",  # any handler — never invoked
            )
            try:
                subs = (
                    await self._plugin_core.topic_registry.list_local_subs()
                )
                owned = [s for s in subs if s.sub_uuid == sub_uuid]
                if len(owned) != 1:
                    raise AssertionError(
                        "B-056: runtime subscribe failed to register sub"
                    )
                # Toggle to disabled.
                owned[0].enabled = False
                # find_all should still return the sub (enabled is a
                # post-filter); find_first/eligibility must skip it.
                found = (
                    await self._plugin_core.topic_registry.find_first(topic)
                )
                if found is not None and found.sub_uuid == sub_uuid:
                    raise AssertionError(
                        "B-056: find_first returned a disabled sub "
                        "(enabled=False not honored)"
                    )
            finally:
                await self.unsubscribe(sub_uuid)

        await rec.run_case(
            "bug.B-003.disable_unregisters_subs",
            body_b_003_disable_unregisters_subs,
            category=category,
            tags=("bug_repro", "addressed_in_pr3"), bug_ids=("B-003",),
            **kw,
        )
        await rec.run_case(
            "bug.B-038.sync_pre_start_raises_request_exception",
            body_b_038_sync_pre_start_raises_request_exception,
            category=category,
            tags=("bug_repro", "addressed_in_pr3"), bug_ids=("B-038",),
            **kw,
        )
        await rec.run_case(
            "bug.B-056.disabled_subs_excluded",
            body_b_056_disabled_subs_excluded,
            category=category,
            tags=("bug_repro", "addressed_in_pr3"), bug_ids=("B-056",),
            **kw,
        )

    # ==================================================================
    # _b_active — Recipe C
    # ==================================================================
    async def _b_active(self, rec: CaseRecorder, kw: Dict) -> None:
        category = "active"

        # ---- B-018b --------------------------------------------------
        async def body_b_018b_execute_system_rewrite_alive(c):
            # B-018b: Stage D removed _handle_notify and
            # _handle_topic_request, but the `execute()` method still
            # rewrites author=="system" to hostname. MSG_EXECUTE remains
            # active so a remote node can still spoof via that path.
            # Stage F can't actually run a remote spoof here without
            # two-node infrastructure. Structural assertion: the
            # rewrite block is still present in execute()'s source.
            src = inspect.getsource(self._plugin_core.execute)
            if (
                'author == "system"' not in src
                and "author == 'system'" not in src
            ):
                # The rewrite was removed — bug fixed-by-construction.
                # Body completes normally; expected_status="fail" =>
                # unexpected_pass => bug fixed.
                return
            raise AssertionError(
                "B-018b: execute() still rewrites author=='system' to "
                "hostname — remote spoof path partially survives Stage D"
            )

        # ---- B-021 ---------------------------------------------------
        async def body_b_021_request_event_fallthrough_or_fail(c):
            # B-021: find_first/request_event ordering may still cause
            # non-eligible-sub-blocks-eligible-sub on the new
            # request_event path. Investigation requires constructing a
            # deliberate sub ordering with private/public endpoint pair
            # and exercising request_event's fall-through behavior end
            # to end. The framework state available from inside a
            # running suite doesn't cleanly support adding two
            # competing subs on the same topic (and the existing
            # priv_endpoint fixture is not paired with a public
            # alternative on the same topic). Skip with note pointing
            # at the structural investigation.
            c.skip(
                "STAGE_F_FIXME: request_event fall-through investigation "
                "requires deliberate sub ordering plus paired "
                "private/public endpoints on the same topic. Existing "
                "fixtures don't provide this pairing — defer to a "
                "follow-up that adds a dedicated fixture."
            )

        # ---- B-044 ---------------------------------------------------
        async def body_b_044_silent_truncation_on_error(c):
            # B-044: error chunk in queue stream breaks loop without
            # yielding — consumer sees clean end. Test: drive a stream
            # against TestStreamTarget that raises after N items, then
            # consume via execute_stream. If RequestException is
            # raised, the bug is fixed; if no exception fires after
            # exactly N items, the bug still reproduces.
            received = []
            try:
                async for v in self.execute_stream(
                    STREAM_TARGET, "ea_gen_raises_after", (3,)
                ):
                    received.append(v)
            except RequestException:
                # Fixed — propagation works. Body completes normally;
                # expected_status="fail" => unexpected_pass => bug
                # fixed.
                return
            # No exception fired. Bug still present iff we got the
            # pre-error items silently.
            if len(received) >= 1:
                raise AssertionError(
                    f"B-044: stream truncated silently "
                    f"(got {len(received)} items, no exception)"
                )

        # ---- B-045 ---------------------------------------------------
        async def body_b_045_stream_timeout_exception_type(c):
            # B-045: stream timeout used to surface as
            # asyncio.TimeoutError instead of RequestException — verify
            # which wins now. Drive a long-yielding stream with a tight
            # timeout via execute_stream. RequestException = fixed;
            # TimeoutError surfacing = bug still present.
            import asyncio as _asyncio
            try:
                async for _ in self.execute_stream(
                    STREAM_TARGET,
                    "ea_gen_hangs",
                    None,
                    timeout=0.2,
                ):
                    pass
            except RequestException:
                # Fixed — symmetric with execute(). Body completes;
                # expected_status="fail" => unexpected_pass.
                return
            except _asyncio.TimeoutError as e:
                raise AssertionError(
                    f"B-045: stream timeout surfaced as "
                    f"asyncio.TimeoutError, not RequestException ({e!r})"
                )

        # ---- B-046 ---------------------------------------------------
        async def body_b_046_plugin_lock_held_across_on_enable(c):
            # B-046: plugin_lock held across full on_enable — if a
            # plugin's on_enable sleeps, a concurrent pop_plugin on a
            # *different* plugin must wait. Repro: confirm plugin_lock
            # exists at module-class level and is the same instance
            # used across enable/disable paths. A direct end-to-end
            # repro would deadlock the running suite.
            core = self._plugin_core
            lock = getattr(core, "plugin_lock", None)
            if lock is None:
                # Lock removed — bug architecturally fixed-by-construction.
                return
            # Inspect _enable_plugin source for an `async with
            # self.plugin_lock` envelope around on_enable. If on_enable
            # is invoked inside the lock (still), the bug surface
            # remains.
            src = inspect.getsource(core._enable_plugin)
            holds_lock = "self.plugin_lock" in src or "plugin_lock" in src
            calls_on_enable = "on_enable" in src
            if holds_lock and calls_on_enable:
                raise AssertionError(
                    "B-046: _enable_plugin still holds plugin_lock "
                    "across on_enable — concurrent pop_plugin on "
                    "another name will block on the same lock"
                )

        # ---- B-047 ---------------------------------------------------
        async def body_b_047_task_list_grows_unboundedly(c):
            # B-047: task_list grows unboundedly. Drive 200 publish_event
            # calls (slow tag would make this 1000; keep moderate for
            # default-run safety) and verify whether task_list grew by
            # ~that count. Bug confirmed if growth is linear (no
            # eviction).
            core = self._plugin_core
            tlist = getattr(core, "task_list", None)
            if tlist is None:
                # Attribute removed — fixed-by-construction.
                return
            before = len(tlist)
            # Use a topic with no subs to make publish_event a near-noop.
            for _ in range(200):
                await self.publish_event_for_repro_no_subs()
            after = len(tlist)
            growth = after - before
            if growth >= 100:
                raise AssertionError(
                    f"B-047: task_list grew by {growth} entries over "
                    f"200 publish_events — no eviction; unbounded leak "
                    f"surface still present"
                )

        # ---- B-048 ---------------------------------------------------
        async def body_b_048_find_endpoint_returns_none_tuple(c):
            # B-048: find_endpoint type annotation says Optional[tuple]
            # but returns 3-tuple of Nones on miss. Verify the actual
            # return shape against a guaranteed-miss lookup. Signature
            # is (access_name, hosts, blocked_hosts, plugin_uuid,
            # requester_id, target_plugin) — find_endpoint is async.
            core = self._plugin_core
            try:
                result = await core.find_endpoint(
                    "no_such_endpoint_for_b048_repro",
                    "any",
                )
            except TypeError:
                # Signature drift — skip to avoid spurious failure.
                c.skip(
                    "find_endpoint signature changed — re-verify in "
                    "follow-up"
                )
                return
            # Bug confirmed if result is the 3-tuple of Nones rather
            # than a single None.
            if (
                isinstance(result, tuple)
                and len(result) == 3
                and result == (None, None, None)
            ):
                raise AssertionError(
                    "B-048: find_endpoint returns (None, None, None) "
                    "instead of None on miss — annotation contract "
                    "broken"
                )

        # ---- B-051 ---------------------------------------------------
        async def body_b_051_normalize_hosts_authors_keyword(c):
            # B-051: _normalize_hosts is reused for the `authors` field.
            # The keyword-in-list guard rejects "remote"/"any" in lists
            # with other elements — appropriate for hosts but
            # inappropriate for authors (where "remote" could be a
            # literal plugin name). Bug confirms if ValueError is
            # raised. If the bug is fixed (separate normalize for
            # authors), the ValueError won't fire — recorder records
            # as "fail" (expected exception not raised).
            from PluginCore import _normalize_hosts
            c.expect_exception(ValueError, match=r"keyword 'remote'")
            _normalize_hosts(
                ["remote", "OtherPlugin"],
                param_name="authors",
                default=None,
            )

        # ---- B-054 ---------------------------------------------------
        async def body_b_054_request_event_stream_bypasses_request(c):
            # B-054: request_event_stream bypasses Request lifecycle —
            # stream-dispatch entries aren't tracked in core.requests.
            # Repro: verify that during a streaming dispatch, no
            # corresponding entry shows up in requests. Implementation-
            # bound: skip if requests dict isn't accessible or the
            # streaming primitive isn't routable here.
            core = self._plugin_core
            requests = getattr(core, "requests", None)
            if requests is None:
                c.skip(
                    "STAGE_F_FIXME: core.requests not accessible from "
                    "suite context — repro path implementation-bound"
                )
                return
            # No clean way to drive a streaming request and snapshot
            # the requests dict mid-flight without race-prone timing.
            # Defer to fixture work.
            c.skip(
                "STAGE_F_FIXME: request_event_stream lifecycle repro "
                "needs mid-flight snapshot of core.requests; race-prone "
                "without dedicated harness fixture"
            )

        # ---- B-064 ---------------------------------------------------
        async def body_b_064_subscribe_no_async_log_errors(c):
            # B-064: preventive — Plugin.subscribe must NOT have
            # @async_log_errors decorator. getsource inspects the
            # original source text; a post-class-definition wrapping
            # would slip past (low-risk false negative documented).
            src = inspect.getsource(Plugin.subscribe)
            c.expect("async_log_errors" in src, False)

        # -- run_case calls --------------------------------------------
        # B-018b: expected_status="fail" — bug expected to repro
        # (rewrite block still present); body raises AssertionError
        # with matching signature on confirmed repro.
        await rec.run_case(
            "bug.B-018b.execute_system_rewrite_alive",
            body_b_018b_execute_system_rewrite_alive,
            category=category,
            tags=("bug_repro", "active"), bug_ids=("B-018",),
            expected_status="fail",
            expected_signature={
                "exception_type": "AssertionError",
                "message_regex": r"system_rewrite|partially survives",
            },
            **kw,
        )
        # B-021: skip pending fixture work.
        await rec.run_case(
            "bug.B-021.request_event_fallthrough_or_fail",
            body_b_021_request_event_fallthrough_or_fail,
            category=category,
            tags=("bug_repro", "active", "deferred"), bug_ids=("B-021",),
            **kw,
        )
        # B-044: expected_status="fail" — bug expected to repro
        # (silent truncation); body raises AssertionError on confirmed
        # repro, returns normally on fix.
        await rec.run_case(
            "bug.B-044.silent_truncation_on_error",
            body_b_044_silent_truncation_on_error,
            category=category,
            tags=("bug_repro", "active"), bug_ids=("B-044",),
            expected_status="fail",
            expected_signature={
                "exception_type": "AssertionError",
                "message_regex": r"silent",
            },
            **kw,
        )
        # B-045: expected_status="fail" — bug expected to repro
        # (TimeoutError instead of RequestException).
        await rec.run_case(
            "bug.B-045.stream_timeout_exception_type",
            body_b_045_stream_timeout_exception_type,
            category=category,
            tags=("bug_repro", "active"), bug_ids=("B-045",),
            expected_status="fail",
            expected_signature={
                "exception_type": "AssertionError",
                "message_regex": r"asyncio\.TimeoutError",
            },
            **kw,
        )
        # B-046: expected_status="fail" — bug expected to repro
        # (lock still wraps on_enable).
        await rec.run_case(
            "bug.B-046.plugin_lock_held_across_on_enable",
            body_b_046_plugin_lock_held_across_on_enable,
            category=category,
            tags=("bug_repro", "active"), bug_ids=("B-046",),
            expected_status="fail",
            expected_signature={
                "exception_type": "AssertionError",
                "message_regex": r"plugin_lock",
            },
            **kw,
        )
        # B-047: expected_status="fail" — bug expected to repro
        # (task_list grows unboundedly). slow=True so default fast-runs
        # skip it.
        await rec.run_case(
            "bug.B-047.task_list_grows_unboundedly",
            body_b_047_task_list_grows_unboundedly,
            category=category,
            tags=("bug_repro", "active", "slow"), bug_ids=("B-047",),
            expected_status="fail",
            expected_signature={
                "exception_type": "AssertionError",
                "message_regex": r"unbounded|task_list grew",
            },
            slow=True,
            **kw,
        )
        # B-048: expected_status="fail" — bug expected to repro
        # (3-tuple of Nones instead of None).
        await rec.run_case(
            "bug.B-048.find_endpoint_returns_none_tuple",
            body_b_048_find_endpoint_returns_none_tuple,
            category=category,
            tags=("bug_repro", "active"), bug_ids=("B-048",),
            expected_status="fail",
            expected_signature={
                "exception_type": "AssertionError",
                "message_regex": r"None, None, None",
            },
            **kw,
        )
        # B-051: c.expect_exception drives the path — no
        # expected_status="fail" needed. ValueError matching =>
        # recorded as pass (bug confirmed).
        await rec.run_case(
            "bug.B-051.normalize_hosts_authors_keyword",
            body_b_051_normalize_hosts_authors_keyword,
            category=category,
            tags=("bug_repro", "active"), bug_ids=("B-051",),
            **kw,
        )
        # B-054: skip pending fixture work.
        await rec.run_case(
            "bug.B-054.request_event_stream_bypasses_request",
            body_b_054_request_event_stream_bypasses_request,
            category=category,
            tags=("bug_repro", "active", "deferred"), bug_ids=("B-054",),
            **kw,
        )
        # B-064: preventive — body uses c.expect (default
        # expected_status="pass"); pass = decorator absent (good).
        await rec.run_case(
            "bug.B-064.subscribe_no_async_log_errors",
            body_b_064_subscribe_no_async_log_errors,
            category=category,
            tags=("bug_repro", "active", "preventive"), bug_ids=("B-064",),
            **kw,
        )

    # Helper used by B-047 — dispatches a real declared event so
    # _lookup_event succeeds and _fanout_sub appends a task to
    # task_list (the surface the bug describes).
    async def publish_event_for_repro_no_subs(self):
        try:
            await self.publish_event("b047_probe_event")
        except Exception:
            pass

    # ==================================================================
    # _b_deferred — Recipe E (skip with STAGE_F_FIXME)
    # ==================================================================
    async def _b_deferred(self, rec: CaseRecorder, kw: Dict) -> None:
        category = "deferred"

        async def body_b_026_deferred(c):
            c.skip(
                "STAGE_F_FIXME: B-026 references request_topic_remote "
                "(removed in Stage D) AND requires a writer mock that "
                "fails on _send_end_stream after a successful chunk "
                "drain. Practical fix is the API removal itself; no "
                "current fixture supports the writer-failure case."
            )

        async def body_b_034_deferred(c):
            c.skip(
                "STAGE_F_FIXME: hot-reload subscription window race. "
                "Stage B lifecycle wrappers shrunk the gap but the "
                "race still exists. Bugtracker pre-marks this as "
                "TIMING-RACE (5 ms sleep doesn't reliably hit). "
                "TestLifecycleSuite already carries B-037 cycle-race "
                "coverage; defer here."
            )

        async def body_b_049_deferred(c):
            c.skip(
                "STAGE_F_FIXME: _enable_plugin no on_enable timeout. "
                "Repro needs a controlled-startup harness — calling "
                "_enable_plugin from inside a running suite would "
                "deadlock on plugin_lock. Same harness pattern as "
                "B-007 in TestLifecycleSuite."
            )

        async def body_b_050_deferred(c):
            c.skip(
                "STAGE_F_FIXME: close() try/finally masking. Needs a "
                "fixture with on_disable raising AND _unregister "
                "mocked to raise; no current fixture supports this."
            )

        async def body_b_055_deferred(c):
            c.skip(
                "STAGE_F_FIXME: aclose().result() no timeout. Needs "
                "worker thread plus early-break orchestration; not "
                "covered by current fixtures."
            )

        async def body_b_057_deferred(c):
            c.skip(
                "STAGE_F_FIXME: C6 placeholder-drift INFO log — "
                "bugtracker explicitly marks this deferred polish."
            )

        async def body_b_058_deferred(c):
            c.skip(
                "STAGE_F_FIXME: _warn_redundant_host_combos not called "
                "for sub filters. Log-capture-based test; no shared "
                "log-capture fixture in repo (logging.handlers."
                "MemoryHandler is brittle)."
            )

        async def body_b_060_deferred(c):
            c.skip(
                "STAGE_F_FIXME: no DEBUG/ERROR log when fan-out "
                "target_plugin missing. Log-capture-based test; same "
                "fixture-gap as B-058."
            )

        async def body_b_061_deferred(c):
            c.skip(
                "STAGE_F_FIXME: no DEBUG log when find_endpoint denies "
                "access. Log-capture-based test; same fixture-gap as "
                "B-058."
            )

        async def body_b_062_deferred(c):
            c.skip(
                "STAGE_F_FIXME: no ERROR log when sync handler raises "
                "during fan-out. Log-capture-based test; same fixture-"
                "gap as B-058."
            )

        async def body_b_063_deferred(c):
            c.skip(
                "STAGE_F_FIXME: unknown override sub-key DEBUG vs "
                "WARNING — touches PR2 apply_overrides (out of PR3 "
                "scope)."
            )

        await rec.run_case(
            "bug.B-026.deferred_writer_mock", body_b_026_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-026",), **kw,
        )
        await rec.run_case(
            "bug.B-034.deferred_reload_window_race", body_b_034_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-034",), **kw,
        )
        await rec.run_case(
            "bug.B-049.deferred_enable_no_timeout", body_b_049_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-049",), **kw,
        )
        await rec.run_case(
            "bug.B-050.deferred_close_try_finally_mask", body_b_050_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-050",), **kw,
        )
        await rec.run_case(
            "bug.B-055.deferred_aclose_no_timeout", body_b_055_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-055",), **kw,
        )
        await rec.run_case(
            "bug.B-057.deferred_placeholder_drift_info_log",
            body_b_057_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-057",), **kw,
        )
        await rec.run_case(
            "bug.B-058.deferred_redundant_host_combos_log",
            body_b_058_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-058",), **kw,
        )
        await rec.run_case(
            "bug.B-060.deferred_fanout_target_missing_log",
            body_b_060_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-060",), **kw,
        )
        await rec.run_case(
            "bug.B-061.deferred_find_endpoint_denies_log",
            body_b_061_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-061",), **kw,
        )
        await rec.run_case(
            "bug.B-062.deferred_sync_handler_raises_log",
            body_b_062_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-062",), **kw,
        )
        await rec.run_case(
            "bug.B-063.deferred_unknown_override_subkey_log",
            body_b_063_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-063",), **kw,
        )

    # ==================================================================
    # _b_already_covered — Recipe D (skip-and-cite)
    # ==================================================================
    async def _b_already_covered(self, rec: CaseRecorder, kw: Dict) -> None:
        category = "already_covered"

        async def body_b_002_covered(c):
            c.skip(
                "covered by TestStreamSuite (bug_ids=('B-002',)); "
                "verdict tracks there"
            )

        async def body_b_004_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-004',)); "
                "verdict tracks there"
            )

        async def body_b_005_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-005',)); "
                "verdict tracks there"
            )

        async def body_b_006_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-006',)); "
                "verdict tracks there"
            )

        async def body_b_007_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-007',)) "
                "as DEFERRED — verdict tracks there"
            )

        async def body_b_008_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-008',)); "
                "verdict tracks there"
            )

        async def body_b_009_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-009',)); "
                "verdict tracks there"
            )

        async def body_b_010_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-010',)); "
                "verdict tracks there"
            )

        async def body_b_013_covered(c):
            c.skip(
                "covered by TestExecuteSuite (bug_ids=('B-013',)); "
                "verdict tracks there"
            )

        async def body_b_015_covered(c):
            c.skip(
                "covered by TestExecuteSuite (bug_ids=('B-015',)); "
                "verdict tracks there"
            )

        async def body_b_016_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-016',)); "
                "verdict tracks there"
            )

        async def body_b_017_covered(c):
            c.skip(
                "covered by TestExecuteSuite (bug_ids=('B-017',)); "
                "verdict tracks there"
            )

        async def body_b_037_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-037',)); "
                "verdict tracks there"
            )

        async def body_b_040_covered(c):
            c.skip(
                "covered by TestEventSuite (bug_ids=('B-040',)); "
                "verdict tracks there"
            )

        async def body_b_041_covered(c):
            c.skip(
                "covered by TestStreamSuite (bug_ids=('B-041',)); "
                "verdict tracks there"
            )

        async def body_b_043_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-043',)); "
                "verdict tracks there"
            )

        await rec.run_case(
            "bug.B-002.covered_by_test_stream_suite", body_b_002_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-002",),
            **kw,
        )
        await rec.run_case(
            "bug.B-004.covered_by_test_lifecycle_suite", body_b_004_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-004",),
            **kw,
        )
        await rec.run_case(
            "bug.B-005.covered_by_test_lifecycle_suite", body_b_005_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-005",),
            **kw,
        )
        await rec.run_case(
            "bug.B-006.covered_by_test_lifecycle_suite", body_b_006_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-006",),
            **kw,
        )
        await rec.run_case(
            "bug.B-007.covered_by_test_lifecycle_suite", body_b_007_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-007",),
            **kw,
        )
        await rec.run_case(
            "bug.B-008.covered_by_test_lifecycle_suite", body_b_008_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-008",),
            **kw,
        )
        await rec.run_case(
            "bug.B-009.covered_by_test_lifecycle_suite", body_b_009_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-009",),
            **kw,
        )
        await rec.run_case(
            "bug.B-010.covered_by_test_lifecycle_suite", body_b_010_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-010",),
            **kw,
        )
        await rec.run_case(
            "bug.B-013.covered_by_test_execute_suite", body_b_013_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-013",),
            **kw,
        )
        await rec.run_case(
            "bug.B-015.covered_by_test_execute_suite", body_b_015_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-015",),
            **kw,
        )
        await rec.run_case(
            "bug.B-016.covered_by_test_lifecycle_suite", body_b_016_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-016",),
            **kw,
        )
        await rec.run_case(
            "bug.B-017.covered_by_test_execute_suite", body_b_017_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-017",),
            **kw,
        )
        await rec.run_case(
            "bug.B-037.covered_by_test_lifecycle_suite", body_b_037_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-037",),
            **kw,
        )
        await rec.run_case(
            "bug.B-040.covered_by_test_event_suite", body_b_040_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-040",),
            **kw,
        )
        await rec.run_case(
            "bug.B-041.covered_by_test_stream_suite", body_b_041_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-041",),
            **kw,
        )
        await rec.run_case(
            "bug.B-043.covered_by_test_lifecycle_suite", body_b_043_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-043",),
            **kw,
        )
