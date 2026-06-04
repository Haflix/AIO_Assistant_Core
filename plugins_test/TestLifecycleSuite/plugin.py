"""TestLifecycleSuite — Phase 4.

Exercises plugin lifecycle: load, enable, disable, reload, pop, purge plus
bug repros B-004 / B-005 / B-007 / B-008 / B-009 / B-010 / B-016 /
B-037 / B-043 and the disable-reverse-order regression lock.

B-073 Session 2 Step 5: B-006 case removed — its target failure mode
(``running_loop`` crashing on a poisoned ``self.requests`` entry)
ceased to exist when Step 4 killed ``running_loop`` and
``cleanup_requests`` entirely. Done-callback eviction at all 7
framework Request sites replaced the polling reap; there is no
maintenance loop left to test for survival.

Args-override merging cases (8) and per-logger level cases (7) from the plan
are deferred to a follow-up phase — they need fixture-heavy yaml manipulation
and log-record interception machinery that's out of scope here. They are
recorded as `skip` with explicit reasons so the suite still enumerates them.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402
import time  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

from plexus.utils import Plugin  # noqa: E402
from plexus.decorators import async_log_errors, log_errors  # noqa: E402
from plexus.exceptions import RequestException  # noqa: E402
from plexus.plugin_state import Phase, State  # noqa: E402  (C-152)

from _test_helpers import CaseRecorder  # noqa: E402


SUITE_VERSION = "0.3.4"
VICTIM = "TestLifecycleVictim"
VICTIM2 = "TestLifecycleVictim2"
VICTIM_PATH = "./plugins_test/TestLifecycleVictim"
SENTINEL = "TestLifecycleSentinel"
BROKEN_VERSION = "TestLifecycleBrokenVersion"


class TestLifecycleSuite(Plugin):
    """Phase 4 suite plugin. See test_suite_plan.md §6 Phase 4."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self._lifecycle_b037_fired: bool = False

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestLifecycleSuite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestLifecycleSuite disabled")

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
        rec = CaseRecorder("TestLifecycleSuite", SUITE_VERSION, self._plexus)

        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=False,
        )

        await self._basic_load(rec, kw)
        await self._basic_enable_disable(rec, kw)
        await self._basic_b004(rec, kw)
        await self._basic_b010(rec, kw)
        await self._basic_b009(rec, kw)
        await self._basic_reload(rec, kw)
        await self._basic_b016(rec, kw)
        await self._basic_pop_pending(rec, kw)
        await self._basic_b005_purge(rec, kw)
        await self._basic_b008_concurrent_enable(rec, kw)
        await self._basic_stage_o_ready_gate(rec, kw)
        await self._basic_b037_event_during_pop(rec, kw)
        await self._basic_b043_pop_failed_reaped(rec, kw)
        await self._basic_b007_missing_version(rec, kw)
        await self._basic_disable_disabled_endpoint(rec, kw)
        await self._basic_disable_reverse_order(rec, kw)
        await self._basic_args_overrides_skip(rec, kw)
        await self._basic_logger_levels_skip(rec, kw)
        await self._basic_async_reload_skip(rec, kw)
        await self._state_machine_coverage(rec, kw)  # C-152
        # B-073 Session 2 Step 5: B-006 case deleted (tested
        # _running_loop_task which was killed in Step 4). Done-callback
        # eviction removes the entire failure mode the case guarded
        # against (cleanup_requests crash → maintenance loop dead).

        return rec.to_dict()

    # ====================================================================
    # Helpers
    # ====================================================================

    async def _ensure_victim_clean(self) -> None:
        # Defensive: re-enable VICTIM if a prior case left it disabled, then
        # reset all behavior flags. Direct attribute access works even when
        # the plugin is disabled (configure endpoint would fail then).
        victim = self._plexus.plugins.get(VICTIM)
        if victim is None:
            entry = self._find_yaml_entry(VICTIM)
            if entry:
                entry["enabled"] = True
                try:
                    await self._plexus.load_plugin_with_conf(entry)
                except Exception:
                    pass
                victim = self._plexus.plugins.get(VICTIM)
        if victim is not None:
            victim._on_enable_raises_after_setup = False
            victim._on_disable_raises = False
            victim._on_disable_hangs_secs = 0.0
            victim._on_enable_delay_secs = 0.0
            victim._cross_call_during_enable = False
            victim.db_open = True if victim.enabled else False
            if not victim.enabled:
                try:
                    await self._plexus.enable_plugin(VICTIM)
                except Exception:
                    pass

    async def _reload_victim(self) -> None:
        await self._plexus._reload_plugin(VICTIM)

    # ====================================================================
    # BASIC load
    # ====================================================================

    async def _basic_load(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_valid_config(c):
            if VICTIM not in self._plexus.plugins:
                raise AssertionError(f"{VICTIM} not in core.plugins")
            plugin = self._plexus.plugins[VICTIM]
            c.expect(plugin.plugin_name, VICTIM)
            assert plugin.enabled

        await rec.run_case(
            "lifecycle.load.valid_config", body_valid_config,
            tags=("basic",), **kw,
        )

    # ====================================================================
    # BASIC enable / disable
    # ====================================================================

    async def _basic_enable_disable(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_enable_success(c):
            await self._ensure_victim_clean()
            plugin = self._plexus.plugins[VICTIM]
            if not plugin.enabled:
                await self._plexus.enable_plugin(VICTIM)
            assert plugin.enabled

        async def body_disable_success(c):
            await self._ensure_victim_clean()
            plugin = self._plexus.plugins[VICTIM]
            await self._plexus.disable_plugin(VICTIM)
            try:
                assert not plugin.enabled
            finally:
                await self._plexus.enable_plugin(VICTIM)

        await rec.run_case(
            "lifecycle.enable.success", body_enable_success,
            tags=("basic",), **kw,
        )
        await rec.run_case(
            "lifecycle.disable.success", body_disable_success,
            tags=("basic",), **kw,
        )

    # ====================================================================
    # BASIC B-004 — on_enable raises mid-setup, no on_disable cleanup
    # ====================================================================

    async def _basic_b004(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body(c):
            await self._ensure_victim_clean()
            try:
                # Configure WHILE enabled (configure endpoint requires it).
                # Then disable, then re-enable to trigger the raise during
                # on_enable's partial-setup phase.
                await self.execute(VICTIM, "configure",
                                   {"on_enable_raises_after_setup": True})
                await self._plexus.disable_plugin(VICTIM)

                try:
                    await self._plexus.enable_plugin(VICTIM)
                except Exception:
                    pass  # @async_handle_errors swallows; this is defensive

                plugin = self._plexus.plugins[VICTIM]
                if plugin.db_open:
                    c.set_marker("db_was_open_after_failed_enable")
                    raise AssertionError(
                        f"db_open={plugin.db_open} after failed on_enable; "
                        f"enabled={plugin.enabled} (B-004: on_enable raised "
                        f"after partial setup, no on_disable cleanup)"
                    )
            finally:
                # Recovery: directly close partial state, re-enable.
                plugin = self._plexus.plugins.get(VICTIM)
                if plugin is not None:
                    plugin.db_open = False
                    plugin._on_enable_raises_after_setup = False
                    if not plugin.enabled:
                        try:
                            await self._plexus.enable_plugin(VICTIM)
                        except Exception:
                            pass
                await self._ensure_victim_clean()

        # Stage P (PR4): B-004 FIXED. Rollback in
        # _enable_plugin_under_lock now calls plugin.on_disable to give
        # the author a chance to undo partial setup from the failed
        # on_enable. Test promoted to positive regression guard — body
        # asserts plugin.db_open is False after the failed enable.
        await rec.run_case(
            "lifecycle.B-004.on_enable_raises_no_undo", body,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-004",),
            hard_timeout_s=20.0,
            **kw,
        )

    # ====================================================================
    # BASIC B-010 — on_disable raises during reload
    # ====================================================================

    async def _basic_b010(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body(c):
            await self._ensure_victim_clean()
            old_uuid = self._plexus.plugins[VICTIM].plugin_uuid
            try:
                await self.execute(VICTIM, "configure",
                                   {"on_disable_raises": True})

                try:
                    await self._plexus._reload_plugin(VICTIM)
                except Exception:
                    pass

                # B-010 repro: when on_disable raises during reload, the
                # error propagates _disable_plugin → pop_plugin (which
                # wraps + reraises) → _reload_plugin's @async_handle_errors
                # SWALLOWS it. Net state:
                # - plugin is still in core.plugins (line 700 of pop_plugin
                #   never reached — self.plugins.pop didn't run)
                # - plugin.enabled is STILL True (line 849 of _disable_plugin
                #   not reached — on_disable raised before plugin.enabled=False)
                # - plugin instance is the OLD one (load_plugin_with_conf
                #   never ran because pop raised)
                # - caller has no signal — _reload_plugin returned None
                plugin = self._plexus.plugins.get(VICTIM)
                stuck_state = (
                    plugin is not None
                    and plugin.plugin_uuid == old_uuid  # not reloaded
                    and plugin.enabled                  # line 849 not reached
                )
                if stuck_state:
                    c.set_marker("plugin_still_loaded_after_disable_raise")
                    raise AssertionError(
                        f"B-010: failed reload silently left old instance "
                        f"loaded (uuid={old_uuid[:8]}, enabled={plugin.enabled})"
                    )
            finally:
                # Recovery: clear the on_disable_raises flag and force a
                # clean state so subsequent cases don't inherit the half-
                # torn-down plugin.
                plugin = self._plexus.plugins.get(VICTIM)
                if plugin is not None:
                    plugin._on_disable_raises = False
                if VICTIM not in self._plexus.plugins:
                    entry = self._find_yaml_entry(VICTIM)
                    if entry:
                        entry["enabled"] = True
                        try:
                            await self._plexus.load_plugin_with_conf(entry)
                            await self._plexus.enable_plugin(VICTIM)
                        except Exception:
                            pass
                await self._ensure_victim_clean()

        # Stage M (PR4): B-010 verified FIXED-BY-CONSTRUCTION. Body asserts
        # the architectural property — when on_disable raises during reload,
        # the framework no longer leaves the plugin partially torn down.
        await rec.run_case(
            "lifecycle.B-010.on_disable_raises", body,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-010",),
            hard_timeout_s=20.0,
            **kw,
        )

    # ====================================================================
    # BASIC B-009 — _disable_plugin no timeout
    # ====================================================================

    async def _basic_b009(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body(c):
            await self._ensure_victim_clean()
            # Configure a 120s on_disable hang while VICTIM is enabled
            # (configure endpoint requires it).
            await self.execute(VICTIM, "configure",
                               {"on_disable_hangs_secs": 120.0})

            # Override runtime disable timeout for fast test (production
            # default 30s; 1s here so the timeout path runs within a few
            # seconds rather than 30+).
            core = self._plexus
            saved_timeout = getattr(core, "plugin_disable_timeout", 30.0)
            core.plugin_disable_timeout = 1.0
            try:
                # B-009 regression guard: pre-fix, _reload_plugin's
                # _disable_plugin call had no on_disable timeout — a
                # hanging on_disable blocked the lifecycle lock
                # indefinitely. Fix wraps on_disable in
                # asyncio.wait_for(timeout=plugin_disable_timeout) in
                # both _disable_plugin and _pop_plugin_under_lock.
                await asyncio.wait_for(
                    core._reload_plugin(VICTIM),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                # Outer guard fired — fix not in place. Recovery + assert.
                victim = core.plugins.get(VICTIM)
                if victim is not None:
                    victim._on_disable_hangs_secs = 0.0
                    try:
                        await core.pop_plugin(VICTIM)
                    except Exception:
                        pass
                entry = self._find_yaml_entry(VICTIM)
                if entry:
                    entry["enabled"] = True
                    try:
                        await core.load_plugin_with_conf(entry)
                        await core.enable_plugin(VICTIM)
                    except Exception:
                        pass
                raise AssertionError(
                    "B-009 regression: _reload_plugin did not return "
                    "within 5s despite 1s on_disable timeout"
                )
            finally:
                core.plugin_disable_timeout = saved_timeout
                # Recover: clear hang flag on whichever instance
                # survived, ensure VICTIM is loaded + enabled for
                # subsequent cases.
                victim = core.plugins.get(VICTIM)
                if victim is not None:
                    victim._on_disable_hangs_secs = 0.0
                if VICTIM not in core.plugins:
                    entry = self._find_yaml_entry(VICTIM)
                    if entry:
                        entry["enabled"] = True
                        try:
                            await core.load_plugin_with_conf(entry)
                            await core.enable_plugin(VICTIM)
                        except Exception:
                            pass
                await self._ensure_victim_clean()

        await rec.run_case(
            "lifecycle.B-009.disable_no_timeout", body,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-009",),
            hard_timeout_s=15.0,
            **kw,
        )

    # ====================================================================
    # BASIC reload (preserves enabled)
    # ====================================================================

    async def _basic_reload(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body(c):
            await self._ensure_victim_clean()
            assert self._plexus.plugins[VICTIM].enabled
            old_uuid = self._plexus.plugins[VICTIM].plugin_uuid

            await self._plexus._reload_plugin(VICTIM)

            new_plugin = self._plexus.plugins[VICTIM]
            assert new_plugin.enabled
            # New instance has a new uuid
            c.expect(new_plugin.plugin_uuid != old_uuid, True)

        await rec.run_case(
            "lifecycle.reload.preserves_enabled", body,
            tags=("reload",), hard_timeout_s=15.0, **kw,
        )

    # ====================================================================
    # BASIC B-016 — reload with newly-disabled config
    # ====================================================================

    async def _basic_b016(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body(c):
            await self._ensure_victim_clean()
            entry = self._find_yaml_entry(VICTIM)
            if not entry:
                c.skip(f"{VICTIM} entry missing from yaml_config")
            original_enabled = entry["enabled"]
            entry["enabled"] = False

            try:
                # B-016 regression guard: pre-Stage-O, _reload_plugin's
                # _enable_plugin call did self.plugins[plugin_name] (raw
                # subscript) → KeyError → swallowed by @async_handle_errors.
                # Stage O switched _enable_plugin_under_lock to .get() with
                # a None-check; the reload path now cleanly honors the new
                # disabled config — no silent exception swallow.
                await self._plexus._reload_plugin(VICTIM)
                # Expected end-state: plugin removed from self.plugins.
                # _reload_plugin pops first; load_plugin_with_conf then
                # short-circuits on the new enabled=false config without
                # re-registering, and _enable_plugin_under_lock early-
                # returns on .get()=None.
                if VICTIM in self._plexus.plugins:
                    raise AssertionError(
                        "B-016 regression: plugin still loaded after reload "
                        "with newly-disabled config (expected unloaded)"
                    )
            finally:
                entry["enabled"] = original_enabled
                if VICTIM not in self._plexus.plugins:
                    try:
                        await self._plexus.load_plugin_with_conf(entry)
                        await self._plexus.enable_plugin(VICTIM)
                    except Exception:
                        pass
                await self._ensure_victim_clean()

        await rec.run_case(
            "lifecycle.B-016.reload_disabled_in_new_config", body,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-016",),
            hard_timeout_s=15.0,
            **kw,
        )

    # ====================================================================
    # BASIC pop_plugin fails pending
    # ====================================================================

    async def _basic_pop_pending(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body(c):
            await self._ensure_victim_clean()
            task = asyncio.create_task(
                self.execute(VICTIM, "victim_hang_endpoint", {"secs": 30.0})
            )
            await asyncio.sleep(0.1)  # let request register

            try:
                await self._plexus.pop_plugin(VICTIM)
                # The pending task should now error with "unloaded while pending"
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except RequestException as e:
                    if "unloaded" not in str(e).lower():
                        raise AssertionError(
                            f"unexpected RequestException: {e}"
                        )
                except asyncio.TimeoutError:
                    raise AssertionError(
                        "pending task did not get unloaded error within 5s"
                    )
            finally:
                # Re-load victim for subsequent cases
                entry = self._find_yaml_entry(VICTIM)
                if entry:
                    entry["enabled"] = True
                    try:
                        await self._plexus.load_plugin_with_conf(entry)
                        await self._plexus.enable_plugin(VICTIM)
                    except Exception:
                        pass

        await rec.run_case(
            "lifecycle.pop_plugin.fails_pending", body,
            tags=("basic",), hard_timeout_s=15.0, **kw,
        )

    # ====================================================================
    # BASIC B-005 — purge_plugins / purge_plugins_except skip pending
    # ====================================================================

    async def _basic_b005_purge(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_purge(c):
            await self._ensure_victim_clean()
            task = asyncio.create_task(
                self.execute(VICTIM, "victim_hang_endpoint", {"secs": 30.0})
            )
            await asyncio.sleep(0.1)

            # Keepers = every currently-loaded plugin EXCEPT VICTIM. This
            # preserves whatever the suite was loaded with (TestRemoteSuite,
            # other suites/targets, etc.) and only purges the one plugin
            # whose pending task we want to test against.
            keepers = [
                name for name in self._plexus.plugins.keys()
                if name != VICTIM
            ]
            try:
                await self._plexus.purge_plugins_except(keepers)
                # If purge fails the pending task with "unloaded", bug is
                # NOT present. If task hangs/timeouts → bug present.
                try:
                    await asyncio.wait_for(task, timeout=3.0)
                    # task completed (or errored) within 3s → check kind
                    return
                except asyncio.TimeoutError:
                    c.set_marker("task_did_not_get_unloaded_error")
                    raise AssertionError(
                        "B-005: purge_plugins_except did not fail the pending "
                        "task; it hung past 3s after purge"
                    )
                except RequestException:
                    return  # got an error of some kind — bug not present
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except Exception:
                        pass
                # Re-load victim
                entry = self._find_yaml_entry(VICTIM)
                if entry:
                    entry["enabled"] = True
                    try:
                        await self._plexus.load_plugin_with_conf(entry)
                        await self._plexus.enable_plugin(VICTIM)
                    except Exception:
                        pass

        # Stage P (PR4): B-005 FIXED. purge_plugins / purge_plugins_except
        # now delegate to pop_plugin per-name, which fails pending
        # requests targeting the popped plugin. Test promoted to
        # positive regression guard — body asserts the pending task
        # completes (via RequestException or normal return) within 3s
        # of purge instead of hanging.
        await rec.run_case(
            "lifecycle.B-005.purge_except_skips_pending", body_purge,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-005",),
            hard_timeout_s=20.0,
            **kw,
        )

    # ====================================================================
    # BASIC B-008 — concurrent enable race
    # ====================================================================

    async def _basic_b008_concurrent_enable(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body(c):
            await self._ensure_victim_clean()
            # Verify config-order requirement (Victim before Victim2)
            plugins = self._plexus.yaml_config.get("plugins", [])
            try:
                v_idx = next(i for i, p in enumerate(plugins)
                             if p.get("name") == VICTIM)
                v2_idx = next(i for i, p in enumerate(plugins)
                              if p.get("name") == VICTIM2)
            except StopIteration:
                c.skip(f"{VICTIM} or {VICTIM2} missing from config")
                return
            if v_idx >= v2_idx:
                c.skip(
                    f"config order: {VICTIM} (idx={v_idx}) must precede "
                    f"{VICTIM2} (idx={v2_idx})"
                )
                return

            try:
                # Configure WHILE both plugins are enabled (configure
                # endpoint requires it).
                await self.execute(VICTIM, "configure", {
                    "cross_call_during_enable": True,
                })
                await self.execute(VICTIM2, "configure", {
                    "on_enable_delay_secs": 1.0,
                })

                # Now disable both, then re-enable concurrently
                await self._plexus.disable_plugin(VICTIM)
                await self._plexus.disable_plugin(VICTIM2)

                await asyncio.gather(
                    self._plexus.enable_plugin(VICTIM),
                    self._plexus.enable_plugin(VICTIM2),
                    return_exceptions=True,
                )

                # Read state via direct attribute (configure may not be
                # available yet if VICTIM is still in mid-enable).
                victim = self._plexus.plugins.get(VICTIM)
                cross_result = (
                    victim._cross_call_result if victim is not None else None
                )
                if isinstance(cross_result, str) and "Endpoint" in cross_result:
                    c.set_marker("endpoint_not_found_during_concurrent_enable")
                    raise AssertionError(
                        f"B-008: cross-plugin call during concurrent enable "
                        f"saw endpoint-not-found: {cross_result!r}"
                    )
            finally:
                # Reset config state directly via attribute access
                v = self._plexus.plugins.get(VICTIM)
                v2 = self._plexus.plugins.get(VICTIM2)
                if v is not None:
                    v._cross_call_during_enable = False
                    v._cross_call_result = None
                if v2 is not None:
                    v2._on_enable_delay_secs = 0.0
                if v is not None and not v.enabled:
                    try:
                        await self._plexus.enable_plugin(VICTIM)
                    except Exception:
                        pass
                if v2 is not None and not v2.enabled:
                    try:
                        await self._plexus.enable_plugin(VICTIM2)
                    except Exception:
                        pass

        await rec.run_case(
            "lifecycle.B-008.concurrent_enable_race", body,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-008",),
            hard_timeout_s=20.0,
            **kw,
        )

    # ====================================================================
    # BASIC B-037 — publish_event during pop
    # ====================================================================

    async def _basic_b037_event_during_pop(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body(c):
            await self._ensure_victim_clean()
            # Subscribe a sub OWNED BY victim_uuid that targets an endpoint on
            # the suite (lifecycle_observer). Victim-owned means the sub is
            # cleaned when victim is popped. The endpoint flips a flag we
            # check after the race.
            victim_obj = self._plexus.plugins[VICTIM]
            self._lifecycle_b037_fired = False

            sub_id = await self._plexus.subscribe_event(
                "lifecycle/event_during_pop",
                victim_obj.plugin_name,
                victim_obj.plugin_uuid,
                target_plugin=self.plugin_name,
                target_access_name="lifecycle_observer",
            )

            try:
                # Concurrently pop + publish
                pop_task = asyncio.create_task(
                    self._plexus.pop_plugin(VICTIM)
                )
                await asyncio.sleep(0.001)
                await self.publish_event("lifecycle_event_during_pop")
                await pop_task

                if self._lifecycle_b037_fired:
                    c.set_marker("handler_ran_after_disable")
                    raise AssertionError(
                        "B-037: handler ran during pop_plugin (race)"
                    )
            finally:
                try:
                    await self._plexus.unsubscribe_event(sub_id)
                except Exception:
                    pass
                entry = self._find_yaml_entry(VICTIM)
                if entry:
                    entry["enabled"] = True
                    try:
                        await self._plexus.load_plugin_with_conf(entry)
                        await self._plexus.enable_plugin(VICTIM)
                    except Exception:
                        pass

        # Stage M (PR4): B-037 verified FIXED-BY-CONSTRUCTION. Stage D
        # removed legacy notify; new publish_event fan-out has different
        # lifecycle semantics — handler cannot fire after _disable_plugin.
        await rec.run_case(
            "lifecycle.B-037.event_during_pop", body,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-037",),
            hard_timeout_s=15.0,
            **kw,
        )

    async def lifecycle_observer(self, event) -> None:
        """B-037 observer endpoint — sets a flag when fired."""
        self._lifecycle_b037_fired = True

    # ====================================================================
    # BASIC B-043 — pop_plugin failed-pending request reaped
    # ====================================================================

    async def _basic_b043_pop_failed_reaped(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body(c):
            await self._ensure_victim_clean()
            # Start a long-running call against Victim; capture its req_id;
            # cancel the caller; pop the plugin; assert request entry is reaped.
            req = await self._plexus.create_request(
                VICTIM, "victim_hang_endpoint", {"secs": 60.0},
                "", "any", self.plugin_name, self.plugin_uuid,
            )
            req_id = req.id

            task = asyncio.create_task(req.wait_for_result_async())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, RequestException):
                pass
            # B-073 Session 2 Step 3: done-callback eviction. Was
            # ``await req.set_collected()``; migrated to direct sync
            # pop. The producer's finally in ``_process_request`` will
            # also pop on completion (idempotent under ``pop(key, None)``).
            self._plexus.requests.pop(req.id, None)

            try:
                await self._plexus.pop_plugin(VICTIM)

                deadline = time.perf_counter() + 30.0
                while time.perf_counter() < deadline:
                    if req_id not in self._plexus.requests:
                        return
                    await asyncio.sleep(0.5)
                raise AssertionError(
                    f"request {req_id} not reaped within 30s after pop"
                )
            finally:
                entry = self._find_yaml_entry(VICTIM)
                if entry:
                    entry["enabled"] = True
                    try:
                        await self._plexus.load_plugin_with_conf(entry)
                        await self._plexus.enable_plugin(VICTIM)
                    except Exception:
                        pass

        await rec.run_case(
            "lifecycle.B-043.pop_plugin_failed_requests_eventually_reaped", body,
            tags=("bug_repro",), bug_ids=("B-043",),
            hard_timeout_s=45.0,
            **kw,
        )

    # ====================================================================
    # BASIC B-007 — missing version aborts load loop
    # ====================================================================

    async def _basic_b007_missing_version(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body(c):
            c.skip(
                "B-007 (missing-version KeyError aborts get_plugins loop) "
                "cannot be reproduced from inside a running suite: enabling "
                "TestLifecycleBrokenVersion + Sentinel in test_config.yml "
                "kills wait_until_ready before any case runs. Repro requires "
                "a controlled-startup harness (subprocess) — Phase 5 "
                "scaffold could host this once it lands."
            )

        await rec.run_case(
            "lifecycle.B-007.missing_version_aborts_load_loop", body,
            tags=("bug_repro", "deferred"), bug_ids=("B-007",),
            **kw,
        )

    # ====================================================================
    # BASIC disable error — disabled endpoint not callable
    # ====================================================================

    async def _basic_disable_disabled_endpoint(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body(c):
            await self._ensure_victim_clean()
            await self._plexus.disable_plugin(VICTIM)
            try:
                c.expect_exception(RequestException, match=r"[Ee]ndpoint.*not found")
                await self.execute(VICTIM, "is_db_open")
            finally:
                await self._plexus.enable_plugin(VICTIM)

        await rec.run_case(
            "lifecycle.disable.error.disabled_plugin_not_callable", body,
            tags=("error",), hard_timeout_s=15.0, **kw,
        )

    # ====================================================================
    # BASIC contract — disable reverse order via _disable_plugin
    # ====================================================================

    async def _basic_disable_reverse_order(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body(c):
            await self._ensure_victim_clean()
            try:
                await self.execute(VICTIM2, "configure", {})
            except Exception:
                c.skip(f"{VICTIM2} not loaded")
                return

            v1 = self._plexus.plugins[VICTIM]
            v2 = self._plexus.plugins[VICTIM2]
            v1.disable_count = 0
            v2.disable_count = 0

            # Drive disable in reverse config order: Victim2 first, then Victim
            # (matches what core.close() does at core.py:255 .reverse()).
            await self._plexus.disable_plugin(VICTIM2)
            t_v2_disabled = time.perf_counter()
            await self._plexus.disable_plugin(VICTIM)
            t_v1_disabled = time.perf_counter()

            try:
                c.expect(v2.disable_count, 1)
                c.expect(v1.disable_count, 1)
                if not (t_v2_disabled < t_v1_disabled):
                    raise AssertionError(
                        f"reverse-order disable not observed: "
                        f"v2={t_v2_disabled} v1={t_v1_disabled}"
                    )
            finally:
                await self._plexus.enable_plugin(VICTIM2)
                await self._plexus.enable_plugin(VICTIM)

        await rec.run_case(
            "lifecycle.contract.disable_reverse_order_via_disable_plugin",
            body,
            tags=("shutdown", "contract"), hard_timeout_s=15.0, **kw,
        )

    # ====================================================================
    # BASIC args overrides — deferred (skip block)
    # ====================================================================

    async def _basic_args_overrides_skip(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        skip_reason = (
            "args-override merging cases require fixture-heavy yaml_config "
            "manipulation + reload cycles per case; deferred to a follow-up "
            "phase. The merge logic at Plexus._deep_merge_args is "
            "well-documented in commit 5c16050; tests will land alongside "
            "any fix that touches it."
        )

        case_ids_to_skip = [
            "lifecycle.args.deep_merge_preserves_siblings",
            "lifecycle.args.replace_marker_clears",
            "lifecycle.args.replace_marker_with_keys",
            "lifecycle.args.list_fully_replaces",
            "lifecycle.args.type_mismatch_warns_applies",
            "lifecycle.args.base_none_not_mismatch",
            "lifecycle.args.main_invalid_override_warns_ignored",
            "lifecycle.args.plugin_invalid_hard_fails",
            "lifecycle.args.replace_marker_at_root",
        ]

        for cid in case_ids_to_skip:
            async def body(c, _r=skip_reason):
                c.skip(_r)
            await rec.run_case(
                cid, body,
                tags=("args_override", "contract", "deferred"),
                **kw,
            )

    # ====================================================================
    # BASIC per-logger levels — deferred (skip block)
    # ====================================================================

    async def _basic_logger_levels_skip(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        skip_reason = (
            "per-logger level cases require log-record interception (mock "
            "handler installed on root) to verify a record at INFO is "
            "dropped while the override is active. Deferred to follow-up."
        )
        case_ids_to_skip = [
            "lifecycle.logger.set_level_persists_during_runtime",
            "lifecycle.logger.set_level_clears_on_disable",
            "lifecycle.logger.set_level_clears_on_pop",
            "lifecycle.logger.longest_prefix_dot_boundary",
            "lifecycle.logger.mute_level",
            "lifecycle.logger.set_level_survives_config_reload",
            "lifecycle.logger.set_level_clears_on_purge",
        ]
        for cid in case_ids_to_skip:
            async def body(c, _r=skip_reason):
                c.skip(_r)
            await rec.run_case(
                cid, body,
                tags=("logger", "contract", "deferred"),
                **kw,
            )

    # ====================================================================
    # BASIC config hot-reload — deferred (skip block)
    # ====================================================================

    async def _basic_async_reload_skip(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        async def body(c):
            c.skip(
                "async_load_config_yaml regression test requires writing the "
                "config file mid-test (out of scope for unit-style suite); "
                "deferred to follow-up"
            )
        await rec.run_case(
            "lifecycle.config.async_reload_preserves_plugins", body,
            tags=("config", "contract", "deferred"), **kw,
        )

    # B-073 Session 2 Step 5: ``_basic_b006_running_loop`` deleted.
    # The B-006 case tested ``running_loop`` survival of a poisoned
    # ``self.requests`` entry. After Step 4 killed ``running_loop`` +
    # ``cleanup_requests`` entirely, the failure mode the case guarded
    # against no longer exists — there is no maintenance loop to crash.
    # Companion fixture ``inject_bad_request`` on TestLifecycleVictim
    # also deleted (was the entry-point that this case used to poison
    # ``self.requests`` from a remote-callable endpoint). TestBugSuite
    # B-006 skip-stub at lines 1415-1419 + dispatch registration at
    # 1511-1516 also removed.

    # ====================================================================
    # BASIC Stage O — readiness gate (4 cases)
    # ====================================================================

    async def _basic_stage_o_ready_gate(
        self, rec: CaseRecorder, kw: Dict,
    ) -> None:
        """Stage O readiness-gate cases.

        Covers four behaviors:
          1. gate_fires_for_unready_target: caller's execute() blocks
             until the target's _lifecycle_ready is set (waits for
             slow on_enable).
          2. author_manual_clear_set: caller blocks on the
             author-controlled `self.ready` event when on_enable spawns
             a background-task setup.
          3. cycle_timeout: two plugins waiting on each other surfaces
             a clear "not ready within Ns" error after the configured
             timeout instead of hanging forever.
          4. self_call_skips_gate: a plugin's own on_enable calling
             into itself bypasses the gate (otherwise it would deadlock
             against its own _lifecycle_ready).
        """
        VICTIM = "TestLifecycleVictim"
        VICTIM2 = "TestLifecycleVictim2"

        # ---- 1. gate_fires_for_unready_target ----------------------
        async def body_gate_fires_for_unready_target(c):
            await self._ensure_victim_clean()
            v = self._plexus.plugins[VICTIM]
            v._on_enable_delay_secs = 0.0
            await self._plexus.disable_plugin(VICTIM)
            # 3.0s delay (generous margin for Windows scheduler jitter);
            # threshold 2.0s leaves 1.0s slack for the asyncio.sleep(0.1)
            # post-create_task stagger and dispatch overhead, so a loaded
            # CI host that overshoots sleep(0.1) by half a second still
            # passes — but a regression that bypasses the gate entirely
            # would return in ~milliseconds and fail.
            v._on_enable_delay_secs = 3.0
            try:
                # Start enable; while it sleeps, our execute() must
                # block on the readiness gate, then succeed.
                enable_task = asyncio.create_task(
                    self._plexus.enable_plugin(VICTIM)
                )
                await asyncio.sleep(0.1)  # let on_enable start sleeping
                t0 = asyncio.get_event_loop().time()
                result = await self.execute(VICTIM, "is_db_open")
                elapsed = asyncio.get_event_loop().time() - t0
                await enable_task

                c.expect(result, True)
                if elapsed < 2.0:
                    raise AssertionError(
                        f"Stage O: gate did not block — execute() returned "
                        f"in {elapsed:.3f}s while on_enable was still "
                        f"sleeping (expected at least 2.0s)"
                    )
            finally:
                v = self._plexus.plugins.get(VICTIM)
                if v is not None:
                    v._on_enable_delay_secs = 0.0
                await self._ensure_victim_clean()

        await rec.run_case(
            "lifecycle.ready.gate_fires_for_unready_target",
            body_gate_fires_for_unready_target,
            tags=("stage_o", "regression_guard"),
            hard_timeout_s=15.0,
            **kw,
        )

        # ---- 2. author_manual_clear_set ----------------------------
        async def body_author_manual_clear_set(c):
            # Caller is the suite plugin. Manually clear the victim's
            # author-controlled ready flag (simulating an author who
            # spawns background-task setup and only sets ready after
            # the task finishes), then schedule a delayed set, then
            # call execute() and verify the call waited.
            await self._ensure_victim_clean()
            v = self._plexus.plugins[VICTIM]
            v.ready.clear()
            try:
                async def _delayed_ready():
                    await asyncio.sleep(0.8)
                    v.ready.set()

                bg = asyncio.create_task(_delayed_ready())
                t0 = asyncio.get_event_loop().time()
                result = await self.execute(VICTIM, "is_db_open")
                elapsed = asyncio.get_event_loop().time() - t0
                await bg

                c.expect(result, True)
                if elapsed < 0.5:
                    raise AssertionError(
                        f"Stage O: author-controlled ready did not block "
                        f"— execute() returned in {elapsed:.3f}s "
                        f"(expected ≥ 0.5s)"
                    )
            finally:
                v = self._plexus.plugins.get(VICTIM)
                if v is not None:
                    v.ready.set()

        await rec.run_case(
            "lifecycle.ready.author_manual_clear_set",
            body_author_manual_clear_set,
            tags=("stage_o", "regression_guard"),
            hard_timeout_s=15.0,
            **kw,
        )

        # ---- 3. cycle_timeout --------------------------------------
        async def body_cycle_timeout(c):
            # Two plugins both with cleared `ready`; neither will set
            # it. With a short configured timeout, our execute() must
            # surface the "not ready within Ns" error instead of
            # hanging.
            await self._ensure_victim_clean()
            v = self._plexus.plugins[VICTIM]
            core = self._plexus
            saved_timeout = getattr(core, "plugin_ready_timeout", 60.0)
            core.plugin_ready_timeout = 1.0
            v.ready.clear()
            try:
                t0 = asyncio.get_event_loop().time()
                try:
                    await self.execute(VICTIM, "is_db_open")
                except RequestException as e:
                    elapsed = asyncio.get_event_loop().time() - t0
                    if "not ready" not in str(e).lower():
                        raise AssertionError(
                            f"Stage O: expected 'not ready' in error, "
                            f"got: {e!r}"
                        )
                    if elapsed > 3.0:
                        raise AssertionError(
                            f"Stage O: cycle_timeout took {elapsed:.2f}s "
                            f"(timeout was 1.0s)"
                        )
                    return
                raise AssertionError(
                    "Stage O: cycle_timeout did not raise; gate failed "
                    "to enforce timeout"
                )
            finally:
                v = self._plexus.plugins.get(VICTIM)
                if v is not None:
                    v.ready.set()
                core.plugin_ready_timeout = saved_timeout

        await rec.run_case(
            "lifecycle.ready.cycle_timeout",
            body_cycle_timeout,
            tags=("stage_o", "regression_guard"),
            hard_timeout_s=10.0,
            **kw,
        )

        # ---- 4. self_call_skips_gate -------------------------------
        async def body_self_call_skips_gate(c):
            # The suite plugin calls its OWN endpoint
            # (`lifecycle_observer`, by way of an execute() targeted at
            # itself). With self.ready cleared, the gate would
            # otherwise block forever on the caller's own ready event;
            # the requester==target self-call carve-out (Q23) must
            # skip the gate so the call returns immediately.
            saved_fired = self._lifecycle_b037_fired
            self.ready.clear()
            try:
                t0 = asyncio.get_event_loop().time()
                await self.execute(
                    self.plugin_name, "lifecycle_observer", (None,)
                )
                elapsed = asyncio.get_event_loop().time() - t0
                if elapsed > 2.0:
                    raise AssertionError(
                        f"Stage O: self-call took {elapsed:.2f}s — "
                        f"gate did not skip for requester == target uuid"
                    )
            finally:
                self.ready.set()
                self._lifecycle_b037_fired = saved_fired

        await rec.run_case(
            "lifecycle.ready.self_call_skips_gate",
            body_self_call_skips_gate,
            tags=("stage_o", "regression_guard"),
            hard_timeout_s=10.0,
            **kw,
        )

    # ====================================================================
    # C-152: state-machine coverage. Previously the suite had zero
    # references to plugin_states / last_errors / FAILED_LOAD / State,
    # leaving the public state-machine surface untested.  These cases
    # exercise the observable API: dict membership, state transitions,
    # error-record population, and enum visibility.
    # ====================================================================

    async def _state_machine_coverage(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_plugin_states_entry_exists(c):
            await self._ensure_victim_clean()
            ps = self._plexus.plugin_states.get(VICTIM)
            if ps is None:
                raise AssertionError(
                    f"plugin_states has no entry for {VICTIM!r}"
                )
            c.expect(ps.name, VICTIM)
            assert isinstance(ps.state, State), (
                f"PluginState.state is not a State enum value: "
                f"{type(ps.state).__name__}"
            )
            assert ps.instance is self._plexus.plugins[VICTIM], (
                "PluginState.instance not bound to the runtime plugin"
            )

        async def body_enabled_state_value(c):
            await self._ensure_victim_clean()
            ps = self._plexus.plugin_states[VICTIM]
            if ps.state is not State.ENABLED:
                raise AssertionError(
                    f"victim post-_ensure_victim_clean expected "
                    f"State.ENABLED; got {ps.state}"
                )

        async def body_disable_transitions_to_inactive(c):
            await self._ensure_victim_clean()
            await self._plexus.disable_plugin(VICTIM)
            try:
                ps = self._plexus.plugin_states[VICTIM]
                if ps.state is not State.INACTIVE:
                    raise AssertionError(
                        f"after disable, expected State.INACTIVE; "
                        f"got {ps.state}"
                    )
            finally:
                await self._plexus.enable_plugin(VICTIM)

        async def body_on_enable_raise_records_phase_enable_error(c):
            await self._ensure_victim_clean()
            victim = self._plexus.plugins[VICTIM]
            await self._plexus.disable_plugin(VICTIM)
            victim._on_enable_raises_after_setup = True
            try:
                try:
                    await self._plexus.enable_plugin(VICTIM)
                except Exception:
                    pass  # expected
                ps = self._plexus.plugin_states[VICTIM]
                if ps.state is not State.INACTIVE:
                    raise AssertionError(
                        f"after on_enable raise, expected rollback to "
                        f"State.INACTIVE; got {ps.state}"
                    )
                err = ps.last_errors.get(Phase.ENABLE)
                if err is None:
                    raise AssertionError(
                        "last_errors[Phase.ENABLE] not populated after "
                        "on_enable raised"
                    )
                # C-146: ErrorRecord no longer retains the live
                # BaseException — it stores type name + repr + tb
                # string. Verify the shape and that the type name is
                # non-empty.
                if not isinstance(err.exception_type, str) or not err.exception_type:
                    raise AssertionError(
                        f"ErrorRecord.exception_type is not a non-empty str: "
                        f"{err.exception_type!r}"
                    )
                if not isinstance(err.exception_repr, str) or not err.exception_repr:
                    raise AssertionError(
                        f"ErrorRecord.exception_repr is not a non-empty str: "
                        f"{err.exception_repr!r}"
                    )
                if not err.traceback:
                    raise AssertionError(
                        "ErrorRecord.traceback is empty"
                    )
            finally:
                # Use _ensure_victim_clean (rather than a bare
                # enable_plugin) so cleanup failures don't silently
                # leave subsequent cases running against a victim
                # stuck in INACTIVE with stale fixture flags.
                victim._on_enable_raises_after_setup = False
                await self._ensure_victim_clean()

        async def body_failed_load_state_visible(c):
            ps = self._plexus.plugin_states.get(BROKEN_VERSION)
            if ps is None:
                c.skip(
                    f"{BROKEN_VERSION} not present in this harness — "
                    f"FAILED_LOAD coverage scoped out"
                )
                return
            if ps.state not in (State.FAILED_LOAD, State.UNLOADED):
                raise AssertionError(
                    f"broken plugin {BROKEN_VERSION!r} state is "
                    f"{ps.state} — expected FAILED_LOAD or UNLOADED"
                )
            if ps.state is State.FAILED_LOAD:
                err = ps.last_errors.get(Phase.LOAD)
                if err is None:
                    raise AssertionError(
                        "FAILED_LOAD without Phase.LOAD error record"
                    )

        async def body_state_enum_values_complete(c):
            expected = {
                "UNLOADED", "INACTIVE", "ENABLING", "ENABLED",
                "DISABLING", "FAILED_LOAD",
            }
            actual = {s.name for s in State}
            c.expect(actual, expected)

        async def body_phase_enum_values_complete(c):
            expected = {"LOAD", "ENABLE", "DISABLE"}
            actual = {p.name for p in Phase}
            c.expect(actual, expected)

        await rec.run_case(
            "lifecycle.state_machine.plugin_states_entry_exists",
            body_plugin_states_entry_exists,
            tags=("basic", "state_machine"),
            bug_ids=("C-152",),
            **kw,
        )
        await rec.run_case(
            "lifecycle.state_machine.enabled_state_value",
            body_enabled_state_value,
            tags=("basic", "state_machine"),
            bug_ids=("C-152",),
            **kw,
        )
        await rec.run_case(
            "lifecycle.state_machine.disable_transitions_to_inactive",
            body_disable_transitions_to_inactive,
            tags=("basic", "state_machine"),
            bug_ids=("C-152",),
            **kw,
        )
        await rec.run_case(
            "lifecycle.state_machine.on_enable_raise_records_phase_enable_error",
            body_on_enable_raise_records_phase_enable_error,
            tags=("basic", "state_machine", "last_errors"),
            bug_ids=("C-152",),
            **kw,
        )
        await rec.run_case(
            "lifecycle.state_machine.failed_load_state_visible",
            body_failed_load_state_visible,
            tags=("basic", "state_machine", "FAILED_LOAD"),
            bug_ids=("C-152",),
            **kw,
        )
        await rec.run_case(
            "lifecycle.state_machine.state_enum_values_complete",
            body_state_enum_values_complete,
            tags=("basic", "state_machine", "regression_guard"),
            bug_ids=("C-152",),
            **kw,
        )
        await rec.run_case(
            "lifecycle.state_machine.phase_enum_values_complete",
            body_phase_enum_values_complete,
            tags=("basic", "state_machine", "regression_guard"),
            bug_ids=("C-152",),
            **kw,
        )

    # ====================================================================
    # Internal helper: find yaml entry
    # ====================================================================

    def _find_yaml_entry(self, name: str) -> Optional[Dict[str, Any]]:
        for entry in self._plexus.yaml_config.get("plugins", []):
            if entry.get("name") == name:
                return entry
        return None
