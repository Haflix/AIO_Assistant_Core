"""TestNotifierSuite — Phase 3.

Exercises notify / request_topic / request_topic_stream surface plus wildcard,
lifecycle (disable/reload windows), bug repros (B-003 / B-014 / B-017 / B-022 /
B-023 / B-034 / B-035 / B-036 / B-039 / B-040), contract regression locks,
plugin-API smoke, and topic / cancellation edge cases.

BadActor handlers (raises / cancels / hangs) are registered on demand by the
suite using the §5.4 idiom — each case loads BadActor in setup, unloads in
finally, and declares set_expected_drift(added=[], removed=[]) so the recorder
sees net-zero plugin churn.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402
import time  # noqa: E402
from typing import Any, Dict, List, Optional, Tuple  # noqa: E402

from utils import Plugin  # noqa: E402
from decorators import async_log_errors, log_errors  # noqa: E402
from exceptions import RequestException  # noqa: E402

from _test_helpers import CaseRecorder  # noqa: E402


SUITE_VERSION = "0.1.0"
TARGET = "TestNotifierTarget"
BAD_ACTOR = "TestNotifierBadActor"
BAD_ACTOR_PATH = "./plugins_test/TestNotifierBadActor"


class TestNotifierSuite(Plugin):
    """Phase 3 suite plugin. See test_suite_plan.md §6 Phase 3."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestNotifierSuite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestNotifierSuite disabled")

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
        rec = CaseRecorder("TestNotifierSuite", SUITE_VERSION, self._plugin_core)

        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=False,
        )

        await self._reset_target()

        await self._basic_notify(rec, kw)
        await self._basic_wildcard_edges(rec, kw)
        await self._basic_request_topic(rec, kw)
        await self._basic_request_topic_stream(rec, kw)
        await self._basic_code_driven(rec, kw)
        await self._basic_b022_silent_no_op(rec, kw)
        await self._basic_contract_handler_wins(rec, kw)
        await self._basic_b023_returns_int(rec, kw)
        await self._basic_b017(rec, kw)
        await self._basic_local_count_correct(rec, kw)
        await self._basic_sync(rec, kw)
        await self._basic_b014_b033_sync_stream(rec, kw)
        await self._basic_b039_chain_via_topic_hop(rec, kw)
        await self._basic_b040_wildcard_tie_after_reload(rec, kw)
        await self._basic_b003_disable_clears_subs(rec, kw)
        await self._basic_b034_reload_window(rec, kw)
        await self._basic_bad_actor_cases(rec, kw)
        await self._basic_contract_misc(rec, kw)
        await self._basic_plugin_api(rec, kw)
        await self._edge_wildcards(rec, kw)
        await self._edge_cancellation(rec, kw)

        return rec.to_dict()

    # ====================================================================
    # Helpers
    # ====================================================================

    async def _reset_target(self) -> None:
        try:
            await self.execute(TARGET, "reset_event_log")
        except Exception:
            pass

    async def _load_bad_actor(self) -> None:
        entry = {
            "name": BAD_ACTOR,
            "enabled": True,
            "path": BAD_ACTOR_PATH,
        }
        await self._plugin_core.load_plugin_with_conf(entry)
        await self._plugin_core._enable_plugin(BAD_ACTOR)

    async def _unload_bad_actor_safe(self) -> None:
        try:
            await self._plugin_core.pop_plugin(BAD_ACTOR)
        except Exception as e:
            self._logger.warning(f"BadActor defensive unload failed: {e}")

    # ====================================================================
    # BASIC notify
    # ====================================================================

    async def _basic_notify(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_no_subs(c):
            count = await self.notify("test/no/subs", host=c.host)
            c.expect(count, 0)

        async def body_exact_one_sub(c):
            await self.execute(TARGET, "reset_event_log")
            count = await self.notify("test/greet", {"name": "X"}, host=c.host)
            c.expect(count, 1)
            log = await self.execute(TARGET, "get_event_log")
            assert ("greet", "X") in log

        async def body_wildcard_match(c):
            await self.execute(TARGET, "reset_event_log")
            count = await self.notify("test/wild/x", host=c.host)
            c.expect(count, 1)
            log = await self.execute(TARGET, "get_event_log")
            assert any(e[0] == "wild_a" for e in log)

        async def body_multiple_subs_via_two_wildcards(c):
            await self.execute(TARGET, "reset_event_log")
            # "test/wild/end" matches BOTH "test/wild/*" and "test/*/end"
            count = await self.notify("test/wild/end", host=c.host)
            c.expect(count, 2)
            log = await self.execute(TARGET, "get_event_log")
            tags = {e[0] for e in log}
            assert "wild_a" in tags and "wild_b" in tags

        async def body_exact_and_wildcard(c):
            await self.execute(TARGET, "reset_event_log")
            # "test/greet" hits the exact greet; the wildcards do not match.
            count = await self.notify("test/greet", {"name": "Y"}, host=c.host)
            c.expect(count, 1)

        await rec.run_case(
            "notif.notify.no_subs", body_no_subs,
            hosts=("local", "remote"), tags=("basic",), **kw,
        )
        await rec.run_case(
            "notif.notify.exact_one_sub", body_exact_one_sub,
            hosts=("local", "remote"), tags=("basic",), **kw,
        )
        await rec.run_case(
            "notif.notify.wildcard_match", body_wildcard_match,
            hosts=("local", "remote"), tags=("wildcards",), **kw,
        )
        await rec.run_case(
            "notif.notify.multiple_subs", body_multiple_subs_via_two_wildcards,
            hosts=("local", "remote"), tags=("fan_out",), **kw,
        )
        await rec.run_case(
            "notif.notify.exact_and_wildcard", body_exact_and_wildcard,
            hosts=("local", "remote"), tags=("wildcards",), **kw,
        )

    # ====================================================================
    # BASIC wildcard edges (lock current behavior)
    # ====================================================================

    async def _basic_wildcard_edges(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_empty_segment(c):
            count = await self.notify("", host=c.host)
            c.expect(count, 0)

        async def body_leading_slash(c):
            count = await self.notify("/test/x", host=c.host)
            c.expect(count, 0)

        async def body_double_slash(c):
            count = await self.notify("test//x", host=c.host)
            c.expect(count, 0)

        async def body_mid_segment(c):
            # "test/wi*ld" — literal segment, NOT a wildcard pattern (suite
            # subscribed to "test/wild/*" at config time)
            count = await self.notify("test/wi*ld", host=c.host)
            c.expect(count, 0)

        await rec.run_case(
            "notif.notify.wildcard.empty_segment", body_empty_segment,
            tags=("wildcards", "contract"), **kw,
        )
        await rec.run_case(
            "notif.notify.wildcard.leading_slash", body_leading_slash,
            tags=("wildcards", "contract"), **kw,
        )
        await rec.run_case(
            "notif.notify.wildcard.double_slash", body_double_slash,
            tags=("wildcards", "contract"), **kw,
        )
        await rec.run_case(
            "notif.notify.wildcard.mid_segment", body_mid_segment,
            tags=("wildcards", "contract"), **kw,
        )

    # ====================================================================
    # BASIC request_topic
    # ====================================================================

    async def _basic_request_topic(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_basic(c):
            r = await self.request_topic(
                "test/math/add", {"a": 3, "b": 4}, host=c.host,
            )
            c.expect(r, 7)

        async def body_no_sub(c):
            c.expect_exception(RequestException, match=r"[Nn]o handler")
            await self.request_topic("test/no/handler", host=c.host)

        async def body_priority_config_first(c):
            # Suite registers a code-driven sub on "test/priority"; the target
            # has no config sub on that topic. find_first should still return
            # the code-driven sub (config doesn't beat code if there is no
            # config-driven candidate).
            sub_id = await self._plugin_core.subscribe(
                "test/priority",
                self.plugin_name,
                self.plugin_uuid,
                handler=lambda *a, **kw_: "code-priority",
            )
            try:
                r = await self.request_topic("test/priority", host=c.host)
                c.expect(r, "code-priority")
            finally:
                await self._plugin_core.unsubscribe(sub_id)

        await rec.run_case(
            "notif.request_topic.basic", body_basic,
            hosts=("local", "remote"), tags=("basic",), **kw,
        )
        await rec.run_case(
            "notif.request_topic.no_sub", body_no_sub,
            hosts=("local", "remote"), tags=("error",), **kw,
        )
        await rec.run_case(
            "notif.request_topic.priority_config_first",
            body_priority_config_first,
            tags=("priority",), **kw,
        )

    # ====================================================================
    # BASIC request_topic_stream
    # ====================================================================

    async def _basic_request_topic_stream(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body_stream_basic(c):
            items = []
            async for chunk in self.request_topic_stream(
                "test/stream", {"n": 3}, host=c.host,
            ):
                items.append(chunk)
            c.expect(items, ["async_0", "async_1", "async_2"])

        async def body_stream_sync_gen(c):
            items = []
            async for chunk in self.request_topic_stream(
                "test/sync_stream", {"n": 3}, host=c.host,
            ):
                items.append(chunk)
            c.expect(items, ["sync_0", "sync_1", "sync_2"])

        await rec.run_case(
            "notif.request_topic_stream.basic", body_stream_basic,
            hosts=("local", "remote"), tags=("stream",), **kw,
        )
        await rec.run_case(
            "notif.request_topic_stream.sync_gen", body_stream_sync_gen,
            hosts=("local", "remote"), tags=("stream", "sync_target"), **kw,
        )

    # ====================================================================
    # BASIC code-driven
    # ====================================================================

    async def _basic_code_driven(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_code_driven_fires(c):
            await self.execute(TARGET, "reset_event_log")
            count = await self.notify("test/count", {"x": 1}, host=c.host)
            c.expect(count, 1)
            n = await self.execute(TARGET, "get_count")
            c.expect(n, 1)

        async def body_unsubscribe_then_no_fire(c):
            sub_id = await self._plugin_core.subscribe(
                "test/once",
                self.plugin_name,
                self.plugin_uuid,
                handler=lambda *a, **kw_: "ok",
            )
            assert await self._plugin_core.unsubscribe(sub_id)
            count = await self.notify("test/once")
            c.expect(count, 0)

        async def body_unsubscribe_idempotent(c):
            sub_id = await self._plugin_core.subscribe(
                "test/once_b",
                self.plugin_name,
                self.plugin_uuid,
                handler=lambda *a, **kw_: "ok",
            )
            r1 = await self._plugin_core.unsubscribe(sub_id)
            r2 = await self._plugin_core.unsubscribe(sub_id)
            c.expect((r1, r2), (True, False))

        async def body_duplicate_topic_same_plugin(c):
            counter = {"n": 0}

            async def h(*a, **kw_):
                counter["n"] += 1

            sid_a = await self._plugin_core.subscribe(
                "test/dup", self.plugin_name, self.plugin_uuid, handler=h,
            )
            sid_b = await self._plugin_core.subscribe(
                "test/dup", self.plugin_name, self.plugin_uuid, handler=h,
            )
            try:
                count = await self.notify("test/dup")
                c.expect(count, 2)
                c.expect(counter["n"], 2)
            finally:
                await self._plugin_core.unsubscribe(sid_a)
                await self._plugin_core.unsubscribe(sid_b)

        await rec.run_case(
            "notif.notify.code_driven", body_code_driven_fires,
            hosts=("local", "remote"), tags=("code_driven",), **kw,
        )
        await rec.run_case(
            "notif.unsubscribe.code_driven", body_unsubscribe_then_no_fire,
            tags=("unsubscribe",), **kw,
        )
        await rec.run_case(
            "notif.unsubscribe.idempotent", body_unsubscribe_idempotent,
            tags=("unsubscribe",), **kw,
        )
        await rec.run_case(
            "notif.subscribe.duplicate_topic_same_plugin",
            body_duplicate_topic_same_plugin,
            tags=("edge",), **kw,
        )

    # ====================================================================
    # BASIC B-022 silent no-op
    # ====================================================================

    async def _basic_b022_silent_no_op(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body_neither(c):
            # Subscribe with neither handler nor endpoint_access_name.
            # Bug: PluginCore.subscribe accepts the registration silently,
            # and notify increments `called` even though _call_sub falls
            # through both branches (no real delivery). Test detects this
            # by asserting subscribe returned a valid id and notify counts
            # the sub but no exception/warning surfaces to the caller.
            sub_id = await self._plugin_core.subscribe(
                "test/silent_no_op",
                self.plugin_name,
                self.plugin_uuid,
            )
            try:
                if not isinstance(sub_id, str) or not sub_id:
                    raise AssertionError(
                        f"subscribe returned non-id: {sub_id!r}"
                    )
                count = await self.notify("test/silent_no_op")
                # The bug: count > 0 but no work was done. The mere fact
                # that the registration was accepted is the silent failure.
                c.set_marker("silent_unusable_sub_registered")
                raise AssertionError(
                    f"B-022: subscribe accepted neither-handler-nor-access "
                    f"silently; sub_id={sub_id[:8]}…, notify count={count}"
                )
            finally:
                await self._plugin_core.unsubscribe(sub_id)

        await rec.run_case(
            "notif.B-022.subscribe_neither_handler_nor_access", body_neither,
            tags=("bug_repro",), bug_ids=("B-022",),
            expected_status="fail",
            expected_signature={"marker": "silent_unusable_sub_registered"},
            **kw,
        )

    # ====================================================================
    # BASIC contract — handler-wins
    # ====================================================================

    async def _basic_contract_handler_wins(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body(c):
            # When BOTH handler and endpoint_access_name are passed, the
            # handler wins (verified by counting handler calls).
            handler_calls = {"n": 0}

            async def h(*args, **kw_):
                handler_calls["n"] += 1

            sub_id = await self._plugin_core.subscribe(
                "test/handler_wins",
                self.plugin_name,
                self.plugin_uuid,
                endpoint_access_name="n_handle_greet",  # ignored
                handler=h,
            )
            try:
                await self.notify("test/handler_wins", {"name": "X"})
                c.expect(handler_calls["n"], 1)
            finally:
                await self._plugin_core.unsubscribe(sub_id)

        await rec.run_case(
            "notif.contract.both_handler_and_access_handler_wins", body,
            tags=("contract",), **kw,
        )

    # ====================================================================
    # BASIC B-023 notify returns int
    # ====================================================================

    async def _basic_b023_returns_int(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body(c):
            r = await self.notify("test/no/subs", host=c.host)
            if not isinstance(r, int):
                raise AssertionError(
                    f"notify did not return int; got {type(r).__name__}: {r!r}"
                )

        await rec.run_case(
            "notif.B-023.notify_returns_int_not_raises", body,
            hosts=("local", "remote"), tags=("bug_repro",), bug_ids=("B-023",),
            **kw,
        )

    # ====================================================================
    # BASIC B-017 — accessibility via topic
    # ====================================================================

    async def _basic_b017(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_priv_via_topic_from_other(c):
            # The suite is "another plugin" relative to TestNotifierTarget.
            # Notify with the suite's own author_id (Plugin.notify default).
            await self.execute(TARGET, "reset_event_log")
            count = await self.notify(
                "test/priv", {"data": "from_other"}, host=c.host,
            )
            log = await self.execute(TARGET, "get_event_log")
            priv_calls = [e for e in log if e[0] == "priv"]
            if priv_calls:
                # Bug fixed (or not present in this delivery path)? unexpected_pass
                return
            c.set_marker("silently_dropped")
            raise AssertionError(
                f"private endpoint silently dropped notify (count={count})"
            )

        async def body_priv_via_self_pinned(c):
            # Target calls self.notify("test/priv") with its own author_id.
            await self.execute(TARGET, "reset_event_log")
            await self.execute(
                TARGET, "notify_with_own_author",
                {"topic": "test/priv", "args": {"data": "from_self"}},
            )
            log = await self.execute(TARGET, "get_event_log")
            priv_calls = [e for e in log if e[0] == "priv"]
            if not priv_calls:
                raise AssertionError(
                    "self-publish to private endpoint should be allowed"
                )

        async def body_priv_via_raw_core_notify(c):
            # Raw core.notify with default author="system" — execute() rewrites
            # author_id to hostname which never matches any plugin uuid →
            # accessibility check rejects → handler not invoked.
            await self.execute(TARGET, "reset_event_log")
            await self._plugin_core.notify("test/priv", {"data": "raw_core"})
            log = await self.execute(TARGET, "get_event_log")
            priv_calls = [e for e in log if e[0] == "priv"]
            if priv_calls:
                return  # bug fixed
            c.set_marker("silently_dropped")
            raise AssertionError(
                "raw core.notify silently dropped on private endpoint"
            )

        await rec.run_case(
            "notif.B-017.priv_via_topic_from_other", body_priv_via_topic_from_other,
            hosts=("local", "remote"), tags=("bug_repro",), bug_ids=("B-017",),
            expected_status="fail",
            expected_signature={"marker": "silently_dropped"},
            **kw,
        )
        await rec.run_case(
            "notif.access.priv_via_topic_from_self_pinned", body_priv_via_self_pinned,
            tags=("access", "contract"), **kw,
        )
        await rec.run_case(
            "notif.B-017.priv_via_raw_core_notify", body_priv_via_raw_core_notify,
            tags=("bug_repro",), bug_ids=("B-017",),
            expected_status="fail",
            expected_signature={"marker": "silently_dropped"},
            **kw,
        )

    # ====================================================================
    # BASIC local count correct
    # ====================================================================

    async def _basic_local_count_correct(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body(c):
            await self.execute(TARGET, "reset_event_log")
            count = await self.notify("test/wild/end")  # matches both wildcards
            c.expect(count, 2)
            log = await self.execute(TARGET, "get_event_log")
            relevant = [e for e in log if e[0] in ("wild_a", "wild_b")]
            c.expect(len(relevant), 2)

        await rec.run_case(
            "notif.basic.local_count_correct", body,
            tags=("basic",), **kw,
        )

    # ====================================================================
    # BASIC sync APIs
    # ====================================================================

    async def _basic_sync(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_notify_sync(c):
            r = await asyncio.to_thread(self.notify_sync, "test/no/subs")
            c.expect(r, 0)

        async def body_request_topic_sync(c):
            r = await asyncio.to_thread(
                self.request_topic_sync, "test/math/add", {"a": 1, "b": 2},
            )
            c.expect(r, 3)

        await rec.run_case(
            "notif.sync.notify_sync", body_notify_sync,
            tags=("sync",), **kw,
        )
        await rec.run_case(
            "notif.sync.request_topic_sync", body_request_topic_sync,
            tags=("sync",), **kw,
        )

    # ====================================================================
    # BASIC B-014 / B-033 sync stream
    # ====================================================================

    async def _basic_b014_b033_sync_stream(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body_no_remote(c):
            # No local sub for "test/no/handler"; expect immediate
            # RequestException without remote fallback.
            def sync_block():
                items = []
                try:
                    for chunk in self.request_topic_stream_sync(
                        "test/no/handler",
                    ):
                        items.append(chunk)
                except RequestException:
                    return ("raised",)
                return ("clean", items)

            outcome = await asyncio.to_thread(sync_block)
            c.expect(outcome[0], "raised")

        async def body_host_remote_routes_local(c):
            c.skip(
                "Phase 5 subprocess not up; B-033 verification requires a peer "
                "node so we can prove the host='remote' arg is silently ignored"
            )

        await rec.run_case(
            "notif.sync.request_topic_stream_sync_no_remote", body_no_remote,
            tags=("bug_repro", "sync"), bug_ids=("B-014",),
            **kw,
        )
        await rec.run_case(
            "notif.sync.request_topic_stream_sync_host_remote_routes_local",
            body_host_remote_routes_local,
            tags=("bug_repro", "sync", "requires_remote"), bug_ids=("B-033",),
            **kw,
        )

    # ====================================================================
    # BASIC B-039 sync chain via topic hop
    # ====================================================================

    async def _basic_b039_chain_via_topic_hop(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body(c):
            # trigger_topic_hop sets _sync_call_chain.chain on its thread,
            # then calls request_topic_sync("topic/hop"). topic_hop_observer
            # reads the chain on entry. If the chain was preserved we'd see
            # ("synthetic.outer.call",); since the chain is wiped at the
            # topic boundary (B-039) we expect ().
            await asyncio.to_thread(
                lambda: self.execute_sync(TARGET, "trigger_topic_hop")
            )
            observed = await self.execute(TARGET, "get_observed_chain")
            if observed and observed != ():
                # Chain WAS preserved → bug fixed → unexpected_pass
                return
            c.set_marker("chain_was_empty")
            raise AssertionError(
                f"sync_call_chain wiped at topic boundary: observed={observed!r}"
            )

        await rec.run_case(
            "notif.B-039.sync_chain_via_topic_hop", body,
            tags=("bug_repro",), bug_ids=("B-039",),
            expected_status="fail",
            expected_signature={"marker": "chain_was_empty"},
            **kw,
        )

    # ====================================================================
    # BASIC B-040 wildcard tie after reload
    # ====================================================================

    async def _basic_b040_wildcard_tie_after_reload(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body(c):
            # Capture initial winner — call request_topic on a topic that
            # both wild_a (test/wild/*) and wild_b (test/*/end) match, then
            # check the event log's last-fired-from. find_first picks one;
            # which one depends on registration order in the topic registry.
            await self.execute(TARGET, "reset_event_log")
            await self.request_topic("test/wild/end")
            log_before = await self.execute(TARGET, "get_event_log")
            initial = [e[0] for e in log_before if e[0] in ("wild_a", "wild_b")]
            initial_winner = initial[0] if initial else None
            if initial_winner is None:
                raise AssertionError(
                    "no wildcard handler fired on initial request_topic"
                )

            # Reload the target — wildcard subs are unsubscribed and
            # re-registered, possibly in a different dict-iteration order.
            await self._plugin_core._reload_plugin(TARGET)

            await self.execute(TARGET, "reset_event_log")
            await self.request_topic("test/wild/end")
            log_after = await self.execute(TARGET, "get_event_log")
            after = [e[0] for e in log_after if e[0] in ("wild_a", "wild_b")]
            after_winner = after[0] if after else None
            if after_winner is None:
                raise AssertionError(
                    "no wildcard handler fired after reload"
                )

            if initial_winner != after_winner:
                c.set_marker("winner_flipped")
                raise AssertionError(
                    f"winner flipped: {initial_winner} -> {after_winner}"
                )
            # Winner stayed same — could be bug-absent OR registration order
            # happened to match. The case is informational either way.

        await rec.run_case(
            "notif.B-040.wildcard_tie_after_reload", body,
            tags=("bug_repro",), bug_ids=("B-040",),
            expected_status="fail",
            expected_signature={"marker": "winner_flipped"},
            **kw,
        )

    # ====================================================================
    # BASIC B-003 — disable clears subs (it doesn't, today)
    # ====================================================================

    async def _basic_b003_disable_clears_subs(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body_code_driven(c):
            # Read counter via direct attribute access (not via execute) so
            # the readback works while the plugin is disabled.
            target = self._plugin_core.plugins[TARGET]
            target.count_invocations = 0
            target.event_log = []

            await self.notify("test/count", {"phase": "before"})
            count_before = target.count_invocations

            await self._plugin_core._disable_plugin(TARGET)
            try:
                # If subs are properly cleared on disable, this notify
                # finds no sub → count stays the same. If not (the bug),
                # _call_sub dispatches the code-driven handler directly
                # (no enabled check) → count_after > count_before.
                await self.notify("test/count", {"phase": "after_disable"})
                count_after = target.count_invocations
                if count_after > count_before:
                    c.set_marker("disabled_handler_fired")
                    raise AssertionError(
                        f"disabled handler fired: {count_before} -> {count_after}"
                    )
            finally:
                await self._plugin_core._enable_plugin(TARGET)

        async def body_config_driven(c):
            # For config-driven subs, the sub stays in the registry on
            # disable. notify finds the sub and routes through execute,
            # which checks enabled → "Endpoint not found". The handler
            # doesn't fire. But the sub structurally surviving disable IS
            # the bug.  Detect: after disable, find_all returns the sub.
            registry = self._plugin_core.topic_registry
            subs_before = await registry.find_all("test/greet")
            target_uuid_before = next(
                (s.plugin_uuid for s in subs_before
                 if s.plugin_name == TARGET),
                None,
            )
            if target_uuid_before is None:
                raise AssertionError(
                    "test/greet sub not registered before disable"
                )

            await self._plugin_core._disable_plugin(TARGET)
            try:
                subs_after = await registry.find_all("test/greet")
                target_subs_after = [
                    s for s in subs_after if s.plugin_name == TARGET
                ]
                if target_subs_after:
                    c.set_marker("config_sub_still_in_registry")
                    raise AssertionError(
                        "config-driven sub for disabled plugin still in registry"
                    )
            finally:
                await self._plugin_core._enable_plugin(TARGET)

        await rec.run_case(
            "notif.B-003.disable_clears_code_subs", body_code_driven,
            tags=("bug_repro",), bug_ids=("B-003",),
            expected_status="fail",
            expected_signature={"marker": "disabled_handler_fired"},
            hard_timeout_s=20.0,
            **kw,
        )
        await rec.run_case(
            "notif.B-003.disable_clears_config_subs", body_config_driven,
            tags=("bug_repro",), bug_ids=("B-003",),
            expected_status="fail",
            expected_signature={"marker": "config_sub_still_in_registry"},
            hard_timeout_s=20.0,
            **kw,
        )

    # ====================================================================
    # BASIC B-034 — reload window drops notify
    # ====================================================================

    async def _basic_b034_reload_window(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body_window_code_driven(c):
            # Window B (large): from unsubscribe_plugin in pop_plugin until
            # the new on_enable runs and re-registers the code-driven sub.
            # Race: kick off reload, immediately notify "test/count".
            reload_task = asyncio.create_task(
                self._plugin_core._reload_plugin(TARGET)
            )
            try:
                await asyncio.sleep(0.005)  # land mid-reload
                count = await self.notify("test/count", {"x": "during"})
                if count == 0:
                    c.set_marker("notify_dropped")
                    raise AssertionError(
                        "notify during reload window returned 0 (sub unregistered)"
                    )
            finally:
                await reload_task

        async def body_window_config_driven(c):
            reload_task = asyncio.create_task(
                self._plugin_core._reload_plugin(TARGET)
            )
            try:
                await asyncio.sleep(0.005)
                count = await self.notify("test/greet", {"name": "during"})
                if count == 0:
                    c.set_marker("notify_dropped")
                    raise AssertionError(
                        "notify during reload window returned 0 (config sub gap)"
                    )
            finally:
                await reload_task

        await rec.run_case(
            "notif.B-034.reload_window_drops_notify_code_driven",
            body_window_code_driven,
            tags=("bug_repro",), bug_ids=("B-034",),
            expected_status="fail",
            expected_signature={"marker": "notify_dropped"},
            hard_timeout_s=15.0,
            **kw,
        )
        await rec.run_case(
            "notif.B-034.reload_window_drops_notify_config_driven",
            body_window_config_driven,
            tags=("bug_repro",), bug_ids=("B-034",),
            expected_status="fail",
            expected_signature={"marker": "notify_dropped"},
            hard_timeout_s=15.0,
            **kw,
        )

    # ====================================================================
    # BASIC BadActor cases (B-035 / B-036 / error.sub_raises)
    # ====================================================================

    async def _basic_bad_actor_cases(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body_sub_raises(c):
            await self._load_bad_actor()
            try:
                await self.execute(BAD_ACTOR, "register_handler",
                                   {"topic": "ba/raises", "handler_name": "raises"})
                # Add a second sub that returns OK; verify it still fires
                # despite the bad-actor sub raising.
                ok_count = {"n": 0}

                async def ok_handler(*a, **kw_):
                    ok_count["n"] += 1

                ok_sid = await self._plugin_core.subscribe(
                    "ba/raises",
                    self.plugin_name,
                    self.plugin_uuid,
                    handler=ok_handler,
                )
                try:
                    count = await self.notify("ba/raises")
                    c.expect(ok_count["n"], 1)
                    if count < 1:
                        raise AssertionError(
                            f"good sub did not count; count={count}"
                        )
                finally:
                    await self._plugin_core.unsubscribe(ok_sid)
            finally:
                await self._unload_bad_actor_safe()

        async def body_b036_cancellederror(c):
            await self._load_bad_actor()
            try:
                await self.execute(BAD_ACTOR, "register_handler",
                                   {"topic": "ba/cancels", "handler_name": "cancels"})
                try:
                    await self.notify("ba/cancels")
                except asyncio.CancelledError:
                    c.set_marker("cancellederror_propagated")
                    raise AssertionError(
                        "CancelledError leaked from notify"
                    )
            finally:
                await self._unload_bad_actor_safe()

        async def body_b035_blocks_on_slow(c):
            await self._load_bad_actor()
            try:
                await self.execute(BAD_ACTOR, "register_handler",
                                   {"topic": "ba/hangs", "handler_name": "hangs"})
                await c.assert_hang(
                    self.notify("ba/hangs"),
                    timeout_s=2.0,
                    marker="outer_wait_for_fired",
                )
            finally:
                await self._unload_bad_actor_safe()

        ba_kw = dict(kw)

        await rec.run_case(
            "notif.error.sub_raises", body_sub_raises,
            tags=("error",),
            hard_timeout_s=20.0,
            **ba_kw,
        )
        await rec.run_case(
            "notif.B-036.cancellederror_propagates", body_b036_cancellederror,
            tags=("bug_repro",), bug_ids=("B-036",),
            expected_status="fail",
            expected_signature={"marker": "cancellederror_propagated"},
            hard_timeout_s=20.0,
            **ba_kw,
        )
        await rec.run_case(
            "notif.B-035.notify_blocks_on_slow_sub", body_b035_blocks_on_slow,
            tags=("bug_repro",), bug_ids=("B-035",),
            expected_status="fail",
            expected_signature={"marker": "outer_wait_for_fired"},
            hard_timeout_s=20.0,
            **ba_kw,
        )

    # ====================================================================
    # BASIC contract — misc regression locks
    # ====================================================================

    async def _basic_contract_misc(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_wildcard_no_cross_segments(c):
            # "sensor/*" pattern would NOT match "sensor/x/y" (different
            # segment count). Verify by registering a code sub and notifying.
            seen = {"n": 0}

            async def h(*a, **kw_):
                seen["n"] += 1

            sub_id = await self._plugin_core.subscribe(
                "sensor/*",
                self.plugin_name,
                self.plugin_uuid,
                handler=h,
            )
            try:
                # Match
                await self.notify("sensor/foo")
                c.expect(seen["n"], 1)
                # Should NOT match (3 segments vs 2)
                seen["n"] = 0
                await self.notify("sensor/foo/bar")
                c.expect(seen["n"], 0)
            finally:
                await self._plugin_core.unsubscribe(sub_id)

        async def body_find_all_exact_before_wildcard(c):
            # Both an exact sub and a wildcard sub on overlapping topic.
            calls: List[str] = []

            async def h_exact(*a, **kw_):
                calls.append("exact")

            async def h_wild(*a, **kw_):
                calls.append("wild")

            sid_exact = await self._plugin_core.subscribe(
                "ord/x", self.plugin_name, self.plugin_uuid, handler=h_exact,
            )
            sid_wild = await self._plugin_core.subscribe(
                "ord/*", self.plugin_name, self.plugin_uuid, handler=h_wild,
            )
            try:
                await self.notify("ord/x")
                # Both fire; exact-then-wildcard order in find_all means
                # _call_sub iterates exact first.
                c.expect(calls, ["exact", "wild"])
            finally:
                await self._plugin_core.unsubscribe(sid_exact)
                await self._plugin_core.unsubscribe(sid_wild)

        async def body_find_first_code_driven_registration_order(c):
            results: List[str] = []

            async def h_a(*a, **kw_):
                results.append("a")
                return "A"

            async def h_b(*a, **kw_):
                results.append("b")
                return "B"

            sid_a = await self._plugin_core.subscribe(
                "first/order", self.plugin_name, self.plugin_uuid, handler=h_a,
            )
            sid_b = await self._plugin_core.subscribe(
                "first/order", self.plugin_name, self.plugin_uuid, handler=h_b,
            )
            try:
                r = await self.request_topic("first/order")
                c.expect(r, "A")  # first registered wins
            finally:
                await self._plugin_core.unsubscribe(sid_a)
                await self._plugin_core.unsubscribe(sid_b)

        async def body_self_publish(c):
            await self.execute(TARGET, "reset_event_log")
            await self.execute(TARGET, "trigger_self_publish")
            n = await self.execute(TARGET, "get_self_publish_count")
            c.expect(n, 1)

        async def body_networking_disabled_no_remote_attempt(c):
            # networking_enabled is False in test_config.yml; notify with
            # host="any" should not iterate self.network.nodes (which doesn't
            # even exist when networking off). A clean execution with no
            # exception is the assertion.
            await self.notify("test/greet", {"name": "ND"}, host="any")

        await rec.run_case(
            "notif.contract.wildcard_does_not_cross_segments",
            body_wildcard_no_cross_segments,
            tags=("wildcards", "contract"), **kw,
        )
        await rec.run_case(
            "notif.contract.find_all_exact_before_wildcard",
            body_find_all_exact_before_wildcard,
            tags=("priority", "contract"), **kw,
        )
        await rec.run_case(
            "notif.contract.find_first_code_driven_registration_order",
            body_find_first_code_driven_registration_order,
            tags=("priority", "contract"), **kw,
        )
        await rec.run_case(
            "notif.contract.self_publish_self_delivers", body_self_publish,
            tags=("self_publish", "contract"), **kw,
        )
        await rec.run_case(
            "notif.contract.notify_disabled_networking_no_remote_attempt",
            body_networking_disabled_no_remote_attempt,
            tags=("networking", "contract"), **kw,
        )

    # ====================================================================
    # BASIC plugin API smoke
    # ====================================================================

    async def _basic_plugin_api(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_subscribe_unsubscribe_via_plugin_helpers(c):
            results: List[str] = []

            async def h(*a, **kw_):
                results.append("fired")

            # Plugin.subscribe wrapper: only takes (topic, handler) — uses
            # self.plugin_name / self.plugin_uuid automatically.
            sid = await self.subscribe("plugin_api/x", h)
            assert isinstance(sid, str) and sid
            try:
                await self.notify("plugin_api/x")
                c.expect(results, ["fired"])
            finally:
                ok = await self.unsubscribe(sid)
                c.expect(ok, True)

        async def body_notify_returns_int_through_plugin_wrapper(c):
            r = await self.notify("plugin_api/no_subs")
            c.expect(r, 0)
            assert isinstance(r, int)

        await rec.run_case(
            "notif.plugin_api.subscribe_unsubscribe_via_plugin_helpers",
            body_subscribe_unsubscribe_via_plugin_helpers,
            tags=("api", "contract"), **kw,
        )
        await rec.run_case(
            "notif.plugin_api.notify_returns_int_through_plugin_wrapper",
            body_notify_returns_int_through_plugin_wrapper,
            tags=("api", "contract"), **kw,
        )

    # ====================================================================
    # EDGE wildcards
    # ====================================================================

    async def _edge_wildcards(self, rec: CaseRecorder, kw: Dict) -> None:
        async def with_temp_sub(pattern: str, fn):
            count = {"n": 0}

            async def h(*a, **kw_):
                count["n"] += 1

            sid = await self._plugin_core.subscribe(
                pattern, self.plugin_name, self.plugin_uuid, handler=h,
            )
            try:
                await fn(count)
            finally:
                await self._plugin_core.unsubscribe(sid)

        async def body_unicode(c):
            async def go(count):
                await self.notify("café/x")
                c.expect(count["n"], 1)
            await with_temp_sub("café/*", go)

        async def body_literal_star_topic(c):
            async def go(count):
                # subscribed pattern is "*" (single-segment wildcard);
                # notify "*" should match (single segment, any value).
                await self.notify("*")
                c.expect(count["n"], 1)
            await with_temp_sub("*", go)

        async def body_just_slash(c):
            count = {"n": 0}
            async def h(*a, **kw_):
                count["n"] += 1
            sid = await self._plugin_core.subscribe(
                "/", self.plugin_name, self.plugin_uuid, handler=h,
            )
            try:
                # Lock current behavior — whatever it is.
                await self.notify("/")
                # No assertion on count; the case is purely a regression lock
                # for "subscribe to / + notify / does not crash". The match
                # outcome is implementation-defined.
            finally:
                await self._plugin_core.unsubscribe(sid)

        async def body_topic_with_control_chars(c):
            sid = await self._plugin_core.subscribe(
                "weird\ttopic\nname",
                self.plugin_name,
                self.plugin_uuid,
                handler=lambda *a, **kw_: None,
            )
            try:
                # Lock: subscribe accepts the topic (no exception)
                pass
            finally:
                await self._plugin_core.unsubscribe(sid)

        async def body_long_topic_pattern(c):
            long_pat = "a" * 1024
            sid = await self._plugin_core.subscribe(
                long_pat,
                self.plugin_name,
                self.plugin_uuid,
                handler=lambda *a, **kw_: None,
            )
            try:
                pass
            finally:
                await self._plugin_core.unsubscribe(sid)

        edge_kw = dict(kw)

        await rec.run_case(
            "notif.edge.wildcard.unicode", body_unicode,
            category="edge", tags=("wildcards", "edge"), **edge_kw,
        )
        await rec.run_case(
            "notif.edge.wildcard.literal_star_topic", body_literal_star_topic,
            category="edge", tags=("wildcards", "edge"), **edge_kw,
        )
        await rec.run_case(
            "notif.edge.topic.just_slash", body_just_slash,
            category="edge", tags=("wildcards", "edge"), **edge_kw,
        )
        await rec.run_case(
            "notif.edge.subscribe.topic_with_control_chars",
            body_topic_with_control_chars,
            category="edge", tags=("edge",), **edge_kw,
        )
        await rec.run_case(
            "notif.edge.subscribe.long_topic_pattern", body_long_topic_pattern,
            category="edge", tags=("edge",), **edge_kw,
        )

    # ====================================================================
    # EDGE cancellation
    # ====================================================================

    async def _edge_cancellation(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_caller_cancel(c):
            # Cancel notify() caller while gather runs over normal subs.
            # All subs fire OR the caller cleanly receives CancelledError.
            done = {"x": 0}

            async def h(*a, **kw_):
                await asyncio.sleep(0.05)
                done["x"] += 1

            sid = await self._plugin_core.subscribe(
                "edge/cancel", self.plugin_name, self.plugin_uuid, handler=h,
            )
            try:
                task = asyncio.create_task(self.notify("edge/cancel"))
                await asyncio.sleep(0.01)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                # Whether the sub managed to run or not is racy; the case is
                # a regression-lock that the framework doesn't crash here.
            finally:
                await self._plugin_core.unsubscribe(sid)

        await rec.run_case(
            "notif.edge.cancellation.notify_caller_cancel", body_caller_cancel,
            category="edge", tags=("cancellation", "edge"),
            **kw,
        )
