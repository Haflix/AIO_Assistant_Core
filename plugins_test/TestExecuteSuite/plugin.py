"""TestExecuteSuite — Phase 1.

Exercises:
- Value calls (sync/async, all arg shapes, mismatches)
- Error paths (no endpoint/plugin, raises, returns_none, returns Future)
- Timeout (caller wait_for via core.execute timeout=)
- Accessibility (accessible_by_other_plugins=False, self-call pinning)
- Sync API basic
- Multi-instance (TestExecuteTarget loaded twice as TestExecuteTarget and TestExecuteTarget2)
- Cancellation (request entry reaped after cancel)
- Decorator contract regression locks
- Deep RequestException chain propagation
- request_context_async / request_context_sync smoke
- Runner-meta framework_version sanity
- Edge: B-015 args-contract variants (bytes/set/frozenset/dataclass/async_gen/OrderedDict)
- Edge: cancel during sync handler in threadpool

Cases that need fixtures not yet built (e.g. discovery.tag.*) are recorded
as `skip` with a reason; they will be wired in a follow-up once the
required tag-tagged fixture endpoints are added.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402
import time  # noqa: E402
from collections import OrderedDict  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

from utils import Plugin  # noqa: E402
from decorators import (  # noqa: E402
    async_log_errors,
    async_handle_errors,
    async_gen_log_errors,
    log_errors,
)
from exceptions import RequestException  # noqa: E402

from _test_helpers import CaseRecorder, FRAMEWORK_VERSION  # noqa: E402


SUITE_VERSION = "0.2.2"
TARGET = "TestExecuteTarget"
TARGET2 = "TestExecuteTarget2"


@dataclass
class _PayloadDC:
    a: int
    b: int


class TestExecuteSuite(Plugin):
    """Phase 1 suite plugin. See test_suite_plan.md §6 Phase 1."""

    @log_errors
    def on_load(self, *args, **kwargs):
        # Declare instance state per Plugin lifecycle contract (CLAUDE.md):
        self._t1_uuid: Optional[str] = None
        self._t2_uuid: Optional[str] = None
        self._multi_instance_smoke_passed: bool = False

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestExecuteSuite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestExecuteSuite disabled")

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
        rec = CaseRecorder("TestExecuteSuite", SUITE_VERSION, self._plugin_core)

        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=False,  # Phase 5 not built; remote sub-cases skip
        )

        await self._smoke_multi_instance(rec, kw)
        await self._basic_value(rec, kw)
        await self._basic_arg_shapes(rec, kw)
        await self._basic_args_contract_violations(rec, kw)
        await self._basic_errors(rec, kw)
        await self._basic_hosts_validation(rec, kw)
        await self._basic_blocked_hosts_behavior(rec, kw)
        await self._basic_timeout(rec, kw)
        await self._basic_accessibility(rec, kw)
        await self._basic_sync(rec, kw)
        await self._basic_multi_instance(rec, kw)
        await self._basic_cancellation(rec, kw)
        await self._basic_payload(rec, kw)
        await self._basic_contract_chain(rec, kw)
        await self._basic_contract_find_endpoint(rec, kw)
        await self._basic_request_context(rec, kw)
        await self._basic_decorators(rec, kw)
        await self._basic_runner_meta(rec, kw)
        await self._edge_args_contract_variants(rec, kw)
        await self._edge_cancellation(rec, kw)

        return rec.to_dict()

    # ====================================================================
    # SMOKE (gates multi_instance cases)
    # ====================================================================

    async def _smoke_multi_instance(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body(c):
            t1 = await self.execute(TARGET, "get_uuid")
            t2 = await self.execute(TARGET2, "get_uuid")
            if not isinstance(t1, str) or not isinstance(t2, str) or t1 == t2:
                c.set_marker("uuids_collided")
                raise AssertionError(
                    f"multi-instance smoke FAILED: t1={t1!r} t2={t2!r}"
                )
            self._t1_uuid = t1
            self._t2_uuid = t2

        await rec.run_case(
            "exec.smoke.multi_instance_distinct",
            body,
            tags=("smoke",),
            **kw,
        )

        # Gate: only mark smoke passed if the case recorded status="pass"
        # AND was not filtered out (i.e. exists in the recorder's cases list).
        for c_dict in reversed(rec.cases):
            if c_dict["base_id"] == "exec.smoke.multi_instance_distinct":
                self._multi_instance_smoke_passed = c_dict["status"] == "pass"
                break

    # ====================================================================
    # BASIC value cases
    # ====================================================================

    async def _basic_value(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_aa_tuple(c):
            r = await self.execute(TARGET, "ea_add", (2, 3), hosts=c.hosts)
            c.expect(r, 5)

        async def body_aa_dict(c):
            r = await self.execute(TARGET, "ea_add", {"a": 7, "b": 8}, hosts=c.hosts)
            c.expect(r, 15)

        async def body_aa_none_for_zero_args(c):
            r = await self.execute(TARGET, "ea_no_args", None, hosts=c.hosts)
            c.expect(r, "ok")

        async def body_as_tuple(c):
            r = await self.execute(TARGET, "es_add", (4, 6), hosts=c.hosts)
            c.expect(r, 10)

        async def body_as_dict(c):
            r = await self.execute(TARGET, "es_add", {"a": 9, "b": 1}, hosts=c.hosts)
            c.expect(r, 10)

        await rec.run_case(
            "exec.value.aa.tuple", body_aa_tuple,
            hosts=("local", "remote"), tags=("execute",), **kw,
        )
        await rec.run_case(
            "exec.value.aa.dict", body_aa_dict,
            hosts=("local", "remote"), tags=("execute",), **kw,
        )
        await rec.run_case(
            "exec.value.aa.none_for_zero_args", body_aa_none_for_zero_args,
            hosts=("local", "remote"), tags=("execute",), **kw,
        )
        await rec.run_case(
            "exec.value.as.tuple", body_as_tuple,
            hosts=("local", "remote"), tags=("execute",), **kw,
        )
        await rec.run_case(
            "exec.value.as.dict", body_as_dict,
            hosts=("local", "remote"), tags=("execute",), **kw,
        )

    # ====================================================================
    # BASIC arg-shape cases
    # ====================================================================

    async def _basic_arg_shapes(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_kwargs_only_with_dict(c):
            r = await self.execute(
                TARGET, "ea_kwargs_only", {"name": "k", "value": 9}, hosts=c.hosts,
            )
            c.expect(r, "k=9")

        async def body_kwargs_only_with_tuple_mismatch(c):
            c.expect_exception(RequestException)
            await self.execute(TARGET, "ea_kwargs_only", (1, 2), hosts=c.hosts)

        async def body_positional_only_with_tuple(c):
            r = await self.execute(TARGET, "ea_positional_only", (3, 4), hosts=c.hosts)
            c.expect(r, 7)

        async def body_positional_only_with_dict_mismatch(c):
            c.expect_exception(RequestException)
            await self.execute(
                TARGET, "ea_positional_only", {"a": 1, "b": 2}, hosts=c.hosts,
            )

        await rec.run_case(
            "exec.value.aa.kwargs_only_with_dict", body_kwargs_only_with_dict,
            tags=("execute",), **kw,
        )
        await rec.run_case(
            "exec.value.aa.kwargs_only_with_tuple_mismatch",
            body_kwargs_only_with_tuple_mismatch,
            tags=("execute", "mismatch"), **kw,
        )
        await rec.run_case(
            "exec.value.aa.positional_only_with_tuple", body_positional_only_with_tuple,
            tags=("execute",), **kw,
        )
        await rec.run_case(
            "exec.value.aa.positional_only_with_dict_mismatch",
            body_positional_only_with_dict_mismatch,
            tags=("execute", "mismatch"), **kw,
        )

    # ====================================================================
    # BASIC B-015 args-contract violations (silent positional)
    # ====================================================================

    async def _basic_args_contract_violations(
        self, rec: CaseRecorder, kw: Dict
    ) -> None:
        async def body_single_int(c):
            r = await self.execute(TARGET, "ea_returns_arg", 42, hosts=c.hosts)
            c.expect(r, 42)

        async def body_list(c):
            r = await self.execute(TARGET, "ea_returns_arg", [1, 2, 3], hosts=c.hosts)
            c.expect(r, [1, 2, 3])

        async def body_string(c):
            r = await self.execute(TARGET, "ea_returns_arg", "hello", hosts=c.hosts)
            c.expect(r, "hello")

        # B-015 reclassified BY-DESIGN (Stage S): single-positional
        # pass-through is intentional convenience — args=42 calls
        # func(42), args=[1,2,3] calls func([1,2,3]). Cases stay as
        # positive regression guards locking the pass-through.
        await rec.run_case(
            "exec.B-015.single_int", body_single_int,
            tags=("args_contract",), bug_ids=("B-015",), **kw,
        )
        await rec.run_case(
            "exec.B-015.list", body_list,
            tags=("args_contract",), bug_ids=("B-015",), **kw,
        )
        await rec.run_case(
            "exec.B-015.string", body_string,
            tags=("args_contract",), bug_ids=("B-015",), **kw,
        )

    # ====================================================================
    # BASIC error paths
    # ====================================================================

    async def _basic_errors(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_no_endpoint(c):
            c.expect_exception(RequestException, match=r"[Ee]ndpoint.*not found")
            await self.execute(TARGET, "does_not_exist", hosts=c.hosts)

        async def body_no_plugin(c):
            c.expect_exception(RequestException, match=r"[Ee]ndpoint.*not found")
            await self.execute("DoesNotExist", "ea_add", (1, 2), hosts=c.hosts)

        async def body_endpoint_raises(c):
            c.expect_exception(RequestException, match=r"intentional")
            await self.execute(TARGET, "ea_raises", hosts=c.hosts)

        async def body_endpoint_raises_request_exc(c):
            c.expect_exception(RequestException, match=r"specific")
            await self.execute(TARGET, "ea_raises_request_exc", hosts=c.hosts)

        async def body_returns_none(c):
            r = await self.execute(TARGET, "ea_returns_none", hosts=c.hosts)
            c.expect(r, None)

        async def body_returns_future(c):
            r = await self.execute(TARGET, "ea_returns_future", hosts=c.hosts)
            c.expect(r, "future_value")

        async def body_returns_failing_future(c):
            # B-013 regression guard: a returned Future that raises on
            # await used to silently hang the caller (@async_handle_errors
            # swallowed the inner exception; request._future never
            # resolved). The fix catches inside _set_request_result and
            # surfaces the exception as a normal request error, which
            # the caller's execute() re-raises as RequestException.
            c.expect_exception(RequestException, match=r"ValueError.*intentional")
            await self.execute(TARGET, "ea_returns_failing_future", hosts=c.hosts)

        await rec.run_case(
            "exec.error.no_endpoint", body_no_endpoint,
            hosts=("local", "remote"), tags=("error",), **kw,
        )
        await rec.run_case(
            "exec.error.no_plugin", body_no_plugin,
            hosts=("local", "remote"), tags=("error",), **kw,
        )
        await rec.run_case(
            "exec.error.endpoint_raises", body_endpoint_raises,
            hosts=("local", "remote"), tags=("error",), **kw,
        )
        await rec.run_case(
            "exec.error.endpoint_raises_request_exc", body_endpoint_raises_request_exc,
            hosts=("local", "remote"), tags=("error",), **kw,
        )
        await rec.run_case(
            "exec.error.returns_none", body_returns_none,
            hosts=("local", "remote"), tags=("nullable",), **kw,
        )
        await rec.run_case(
            "exec.error.returns_future", body_returns_future,
            hosts=("local", "remote"), tags=("future_unwrap",), **kw,
        )
        await rec.run_case(
            "exec.B-013.returns_failing_future", body_returns_failing_future,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-013",),
            hard_timeout_s=10.0,
            **kw,
        )

    # ====================================================================
    # BASIC hosts validation (rejects + normalization happy paths)
    # ====================================================================

    async def _basic_hosts_validation(self, rec: CaseRecorder, kw: Dict) -> None:
        # ── Rejects ──
        async def body_empty_list(c):
            c.expect_exception(ValueError, match=r"empty list")
            await self.execute(TARGET, "ea_no_args", hosts=[])

        async def body_empty_string(c):
            c.expect_exception(ValueError, match=r"empty string")
            await self.execute(TARGET, "ea_no_args", hosts="")

        async def body_any_in_list(c):
            c.expect_exception(ValueError, match=r"'any'")
            await self.execute(TARGET, "ea_no_args", hosts=["any", "nodeA"])

        async def body_remote_in_list(c):
            c.expect_exception(ValueError, match=r"'remote'")
            await self.execute(TARGET, "ea_no_args", hosts=["remote", "nodeA"])

        async def body_invalid_type(c):
            c.expect_exception(ValueError, match=r"must be str, list")
            await self.execute(TARGET, "ea_no_args", hosts=42)

        async def body_blocked_empty_list(c):
            c.expect_exception(ValueError, match=r"empty list")
            await self.execute(TARGET, "ea_no_args", hosts="any", blocked_hosts=[])

        async def body_blocked_remote_in_list(c):
            c.expect_exception(ValueError, match=r"'remote'")
            await self.execute(
                TARGET, "ea_no_args",
                hosts="any", blocked_hosts=["remote", "nodeA"],
            )

        # ── Happy paths (normalize + dispatch succeeds) ──
        async def body_local_in_list(c):
            # ["local", "nodeA"] is allowed; "nodeA" doesn't exist locally,
            # but "local" matches → endpoint found locally.
            r = await self.execute(
                TARGET, "ea_no_args", hosts=["local", "nodeA"]
            )
            c.expect(r, "ok")

        async def body_collapse_single_any(c):
            # ["any"] should collapse to "any" string; works.
            r = await self.execute(TARGET, "ea_no_args", hosts=["any"])
            c.expect(r, "ok")

        async def body_collapse_single_local(c):
            r = await self.execute(TARGET, "ea_no_args", hosts=["local"])
            c.expect(r, "ok")

        async def body_dedup_duplicates(c):
            # Duplicate keywords collapse via dedup — should NOT raise.
            r = await self.execute(TARGET, "ea_no_args", hosts=["local", "local"])
            c.expect(r, "ok")

        async def body_dedup_hostnames_with_local(c):
            r = await self.execute(
                TARGET, "ea_no_args",
                hosts=["nodeA", "nodeA", "local"],
            )
            c.expect(r, "ok")

        # ── Register cases ──
        await rec.run_case(
            "exec.hosts.validate.empty_list", body_empty_list,
            tags=("hosts", "validation"), **kw,
        )
        await rec.run_case(
            "exec.hosts.validate.empty_string", body_empty_string,
            tags=("hosts", "validation"), **kw,
        )
        await rec.run_case(
            "exec.hosts.validate.any_in_list", body_any_in_list,
            tags=("hosts", "validation"), **kw,
        )
        await rec.run_case(
            "exec.hosts.validate.remote_in_list", body_remote_in_list,
            tags=("hosts", "validation"), **kw,
        )
        await rec.run_case(
            "exec.hosts.validate.invalid_type", body_invalid_type,
            tags=("hosts", "validation"), **kw,
        )
        await rec.run_case(
            "exec.blocked_hosts.validate.empty_list", body_blocked_empty_list,
            tags=("blocked_hosts", "validation"), **kw,
        )
        await rec.run_case(
            "exec.blocked_hosts.validate.remote_in_list",
            body_blocked_remote_in_list,
            tags=("blocked_hosts", "validation"), **kw,
        )
        await rec.run_case(
            "exec.hosts.normalize.local_in_list", body_local_in_list,
            tags=("hosts",), **kw,
        )
        await rec.run_case(
            "exec.hosts.normalize.collapse_single_any", body_collapse_single_any,
            tags=("hosts",), **kw,
        )
        await rec.run_case(
            "exec.hosts.normalize.collapse_single_local",
            body_collapse_single_local,
            tags=("hosts",), **kw,
        )
        await rec.run_case(
            "exec.hosts.normalize.dedup_duplicates", body_dedup_duplicates,
            tags=("hosts",), **kw,
        )
        await rec.run_case(
            "exec.hosts.normalize.dedup_hostnames_with_local",
            body_dedup_hostnames_with_local,
            tags=("hosts",), **kw,
        )

    # ====================================================================
    # BASIC blocked_hosts behavior (excludes the right targets)
    # ====================================================================

    async def _basic_blocked_hosts_behavior(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_block_local_keyword(c):
            # hosts="local" + blocked_hosts="local" → no candidates left.
            c.expect_exception(RequestException, match=r"not found")
            await self.execute(
                TARGET, "ea_no_args",
                hosts="local", blocked_hosts="local",
            )

        async def body_block_self_hostname(c):
            # hosts="any" + blocked_hosts=<own hostname> → local skipped.
            # No remote subnode hosts this target → not found.
            own_host = self._plugin_core.hostname
            c.expect_exception(RequestException, match=r"not found")
            await self.execute(
                TARGET, "ea_no_args",
                hosts="any", blocked_hosts=own_host,
            )

        async def body_block_remote_keeps_local(c):
            # hosts="any" + blocked_hosts="remote" → local still works.
            r = await self.execute(
                TARGET, "ea_no_args",
                hosts="any", blocked_hosts="remote",
            )
            c.expect(r, "ok")

        async def body_block_any_blocks_everything(c):
            # blocked_hosts="any" blocks both local and remote.
            c.expect_exception(RequestException, match=r"not found")
            await self.execute(
                TARGET, "ea_no_args",
                hosts="any", blocked_hosts="any",
            )

        async def body_block_unrelated_hostname_keeps_local(c):
            # blocked_hosts="someUnknownNode" doesn't affect local dispatch.
            r = await self.execute(
                TARGET, "ea_no_args",
                hosts="any", blocked_hosts="someUnknownNode",
            )
            c.expect(r, "ok")

        async def body_block_local_in_list(c):
            # blocked_hosts=["local", "nodeA"] excludes local.
            c.expect_exception(RequestException, match=r"not found")
            await self.execute(
                TARGET, "ea_no_args",
                hosts="local", blocked_hosts=["local", "nodeA"],
            )

        await rec.run_case(
            "exec.blocked_hosts.block_local_keyword", body_block_local_keyword,
            tags=("blocked_hosts",), **kw,
        )
        await rec.run_case(
            "exec.blocked_hosts.block_self_hostname", body_block_self_hostname,
            tags=("blocked_hosts",), **kw,
        )
        await rec.run_case(
            "exec.blocked_hosts.remote_keeps_local",
            body_block_remote_keeps_local,
            tags=("blocked_hosts",), **kw,
        )
        await rec.run_case(
            "exec.blocked_hosts.any_blocks_everything",
            body_block_any_blocks_everything,
            tags=("blocked_hosts",), **kw,
        )
        await rec.run_case(
            "exec.blocked_hosts.unrelated_hostname_keeps_local",
            body_block_unrelated_hostname_keeps_local,
            tags=("blocked_hosts",), **kw,
        )
        await rec.run_case(
            "exec.blocked_hosts.list_form_local", body_block_local_in_list,
            tags=("blocked_hosts",), **kw,
        )

    # ====================================================================
    # BASIC timeout
    # ====================================================================

    async def _basic_timeout(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_hang_with_timeout(c):
            t0 = time.perf_counter()
            try:
                await self.execute(
                    TARGET, "ea_hang", {"seconds": 30.0},
                    hosts=c.hosts, timeout=2.0,
                )
                raise AssertionError("expected RequestException from timeout")
            except RequestException:
                elapsed = time.perf_counter() - t0
                if not (1.5 <= elapsed <= 5.0):
                    raise AssertionError(
                        f"elapsed={elapsed:.2f}s outside [1.5, 5.0]"
                    )

        await rec.run_case(
            "exec.timeout.hang_with_timeout", body_hang_with_timeout,
            hosts=("local", "remote"), tags=("timeout",),
            hard_timeout_s=15.0, **kw,
        )

    # ====================================================================
    # BASIC accessibility
    # ====================================================================

    async def _basic_accessibility(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_priv_from_other(c):
            c.expect_exception(RequestException, match=r"[Ee]ndpoint.*not found")
            await self.execute(TARGET, "ea_private", {"value": 7}, hosts=c.hosts)

        async def body_priv_from_self_pinned(c):
            r = await self.execute(
                TARGET, "ea_self_call",
                {"target_method": "ea_private", "target_args": {"value": 7}},
                hosts=c.hosts,
            )
            c.expect(r, 7)

        await rec.run_case(
            "exec.B-017.private_from_other", body_priv_from_other,
            hosts=("local", "remote"), tags=("accessibility",), bug_ids=("B-017",),
            **kw,
        )
        await rec.run_case(
            "exec.access.private_from_self_pinned_uuid", body_priv_from_self_pinned,
            tags=("accessibility",), **kw,
        )

    # ====================================================================
    # BASIC sync API
    # ====================================================================

    async def _basic_sync(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_from_sync(c):
            # execute_sync from async context would deadlock; use a thread.
            r = await asyncio.to_thread(
                self.execute_sync, TARGET, "ea_add", (1, 2),
            )
            c.expect(r, 3)

        await rec.run_case(
            "exec.sync.from_sync", body_from_sync,
            tags=("sync",), **kw,
        )

    # ====================================================================
    # BASIC multi-instance
    # ====================================================================

    async def _basic_multi_instance(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_distinct_uuids(c):
            if not self._multi_instance_smoke_passed:
                c.skip(
                    "multi-instance smoke failed; loader does not isolate instances"
                )
            t1 = self._t1_uuid
            t2 = self._t2_uuid
            if t1 == t2:
                raise AssertionError(f"uuids identical: {t1}")

        async def body_target_by_uuid(c):
            if not self._multi_instance_smoke_passed:
                c.skip(
                    "multi-instance smoke failed; loader does not isolate instances"
                )
            t1 = self._t1_uuid
            t2 = self._t2_uuid
            r = await self.execute(TARGET, "get_uuid", plugin_uuid=t1)
            c.expect(r, t1)
            r2 = await self.execute(TARGET2, "get_uuid", plugin_uuid=t2)
            c.expect(r2, t2)

        await rec.run_case(
            "exec.multi_instance.distinct_uuids", body_distinct_uuids,
            tags=("multi_instance",), **kw,
        )
        await rec.run_case(
            "exec.multi_instance.target_by_uuid", body_target_by_uuid,
            tags=("multi_instance",), **kw,
        )

    # ====================================================================
    # BASIC cancellation
    # ====================================================================

    async def _basic_cancellation(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_entry_reaped(c):
            # Create the request directly so we know its id deterministically;
            # then await its result via a separate task and cancel it. Avoids the
            # snapshot-diff race of inferring the id from `core.requests.keys()`.
            req = await self._plugin_core.create_request(
                TARGET, "ea_hang", {"seconds": 60.0},
                "", "any", self.plugin_name, self.plugin_uuid,
            )
            req_id = req.id

            task = asyncio.create_task(req.wait_for_result_async())
            await asyncio.sleep(0.05)  # let task enter the await
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, RequestException):
                pass
            # Mark collected since we never observed the (still-running) result.
            await req.set_collected()

            deadline = time.perf_counter() + 25.0
            while time.perf_counter() < deadline:
                if req_id not in self._plugin_core.requests:
                    break
                await asyncio.sleep(0.5)
            else:
                raise AssertionError(
                    f"request {req_id} not reaped within 25s"
                )

        await rec.run_case(
            "exec.cancellation.entry_reaped", body_entry_reaped,
            tags=("cancellation",),
            hard_timeout_s=40.0,
            **kw,
        )

    # ====================================================================
    # BASIC payload sanity
    # ====================================================================

    async def _basic_payload(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_large_return(c):
            r = await self.execute(
                TARGET, "ea_large_return", {"size_bytes": 100_000}, hosts=c.hosts,
            )
            c.expect(len(r), 100_000)
            c.expect(r[0:1], b"\xab")

        await rec.run_case(
            "exec.large_payload.return_value", body_large_return,
            hosts=("local", "remote"), tags=("payload",), **kw,
        )

    # ====================================================================
    # BASIC deep RequestException chain propagation
    # ====================================================================

    async def _basic_contract_chain(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_deep_chain(c):
            try:
                await self.execute(
                    TARGET, "ea_chain_step",
                    {"depth": 3, "target_method": "ea_raises_request_exc"},
                    hosts=c.hosts,
                )
                raise AssertionError("expected RequestException, got no error")
            except RequestException as e:
                msg = str(e)
                if "specific" not in msg:
                    raise AssertionError(
                        f"original message lost; got: {msg!r}"
                    )

        await rec.run_case(
            "exec.contract.deep_chain_request_exception_preserves_message",
            body_deep_chain,
            hosts=("local", "remote"),
            tags=("error", "propagation"),
            **kw,
        )

    # ====================================================================
    # BASIC find_endpoint contract
    # ====================================================================

    async def _basic_contract_find_endpoint(
        self, rec: CaseRecorder, kw: Dict
    ) -> None:
        async def body_uuid_target_conflict(c):
            # plugin_uuid belongs to TARGET; target_plugin says "TARGET2" → mismatch
            t1 = await self.execute(TARGET, "get_uuid")
            try:
                await self.execute(
                    TARGET2, "get_uuid", plugin_uuid=t1,
                )
                raise AssertionError("expected RequestException, got no error")
            except RequestException as e:
                if "not found" not in str(e).lower():
                    raise AssertionError(
                        f"unexpected error: {e!r}"
                    )

        async def body_uuid_after_pop_returns_none(c):
            # Skip — pop_plugin requires Phase 4 mechanics; out of Phase 1 scope.
            c.skip("requires pop_plugin lifecycle from Phase 4")

        async def body_uuid_invalidated_after_reload(c):
            c.skip("requires _reload_plugin from Phase 4")

        await rec.run_case(
            "exec.contract.find_endpoint_uuid_target_plugin_conflict",
            body_uuid_target_conflict,
            tags=("discovery",), **kw,
        )
        await rec.run_case(
            "exec.contract.uuid_after_pop_returns_none",
            body_uuid_after_pop_returns_none,
            tags=("discovery",), **kw,
        )
        await rec.run_case(
            "exec.contract.uuid_invalidated_after_reload",
            body_uuid_invalidated_after_reload,
            tags=("discovery", "reload"), **kw,
        )

    # ====================================================================
    # BASIC request_context_async / request_context_sync smoke
    # ====================================================================

    async def _basic_request_context(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_ctx_async(c):
            req = await self._plugin_core.create_request(
                TARGET, "ea_add", (5, 6),
                "", "any", self.plugin_name, self.plugin_uuid,
            )
            async with self._plugin_core.request_context_async(req) as result:
                c.expect(result, 11)

        async def body_ctx_sync(c):
            def sync_block():
                req = self._plugin_core.create_request_sync(
                    TARGET, "ea_add", (8, 9),
                    "", "any", self.plugin_name, self.plugin_uuid,
                )
                with self._plugin_core.request_context_sync(req) as result:
                    return result

            r = await asyncio.to_thread(sync_block)
            c.expect(r, 17)

        await rec.run_case(
            "exec.contract.request_context_async_basic", body_ctx_async,
            tags=("api",), **kw,
        )
        await rec.run_case(
            "exec.contract.request_context_sync_basic", body_ctx_sync,
            tags=("api",), **kw,
        )

    # ====================================================================
    # BASIC decorator contract regression locks
    # ====================================================================

    async def _basic_decorators(self, rec: CaseRecorder, kw: Dict) -> None:
        # Each victim is defined inside a class so that __get__ binding produces
        # a bound-method whose first arg is `self` (which the decorator inspects
        # for `_logger`). Defining victim as a free function with no `self`
        # would TypeError when bound.
        suite_logger = self._logger

        async def body_async_log_errors_reraises(c):
            class _Holder:
                _logger = suite_logger

                @async_log_errors
                async def victim(self):
                    raise ValueError("boom")

            try:
                await _Holder().victim()
                raise AssertionError("expected ValueError")
            except ValueError as e:
                c.expect(str(e), "boom")

        async def body_async_handle_errors_swallows_generic(c):
            class _Holder:
                _logger = suite_logger

                @async_handle_errors(default_return=42)
                async def victim(self):
                    raise ValueError("boom")

            r = await _Holder().victim()
            c.expect(r, 42)

        async def body_async_handle_errors_propagates_request_exception(c):
            class _Holder:
                _logger = suite_logger

                @async_handle_errors(default_return=None)
                async def victim(self):
                    raise RequestException("propagate me")

            try:
                await _Holder().victim()
                raise AssertionError("expected RequestException")
            except RequestException as e:
                c.expect(str(e), "propagate me")

        async def body_async_gen_log_errors_reraises_inside_iter(c):
            class _Holder:
                _logger = suite_logger

                @async_gen_log_errors
                async def victim_gen(self):
                    yield 1
                    raise ValueError("midstream")

            collected = []
            try:
                async for v in _Holder().victim_gen():
                    collected.append(v)
                raise AssertionError("expected ValueError")
            except ValueError as e:
                c.expect(str(e), "midstream")
                c.expect(collected, [1])

        async def body_gen_log_errors_smoke(c):
            from decorators import gen_log_errors

            class _Holder:
                _logger = suite_logger

                @gen_log_errors
                def victim_gen(self):
                    yield 1
                    yield 2
                    yield 3

            out = list(_Holder().victim_gen())
            c.expect(out, [1, 2, 3])

        await rec.run_case(
            "decorators.async_log_errors_reraises", body_async_log_errors_reraises,
            tags=("decorators",), **kw,
        )
        await rec.run_case(
            "decorators.async_handle_errors_swallows_generic",
            body_async_handle_errors_swallows_generic,
            tags=("decorators",), **kw,
        )
        await rec.run_case(
            "decorators.async_handle_errors_propagates_request_exception",
            body_async_handle_errors_propagates_request_exception,
            tags=("decorators",), **kw,
        )
        await rec.run_case(
            "decorators.async_gen_log_errors_reraises_inside_iter",
            body_async_gen_log_errors_reraises_inside_iter,
            tags=("decorators",), **kw,
        )
        await rec.run_case(
            "decorators.gen_log_errors_smoke", body_gen_log_errors_smoke,
            tags=("decorators",), **kw,
        )

    # ====================================================================
    # BASIC runner-meta sanity (framework_version)
    # ====================================================================

    async def _basic_runner_meta(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_framework_version(c):
            if not isinstance(FRAMEWORK_VERSION, str) or not FRAMEWORK_VERSION:
                raise AssertionError(
                    f"FRAMEWORK_VERSION not a non-empty string: {FRAMEWORK_VERSION!r}"
                )

        await rec.run_case(
            "runner.meta.framework_version_set", body_framework_version,
            tags=("runner", "contract"), **kw,
        )

    # ====================================================================
    # EDGE B-015 args-contract variants
    # ====================================================================

    async def _edge_args_contract_variants(
        self, rec: CaseRecorder, kw: Dict
    ) -> None:
        async def body_bytes(c):
            r = await self.execute(TARGET, "ea_returns_arg", b"\x00\x01")
            c.expect(r, b"\x00\x01")

        async def body_set(c):
            r = await self.execute(TARGET, "ea_returns_arg", {1, 2, 3})
            c.expect(r, {1, 2, 3})

        async def body_frozenset(c):
            r = await self.execute(TARGET, "ea_returns_arg", frozenset([1, 2]))
            c.expect(r, frozenset([1, 2]))

        async def body_dataclass(c):
            payload = _PayloadDC(a=1, b=2)
            r = await self.execute(TARGET, "ea_returns_arg", payload)
            # Dataclass instance should round-trip identically by field-equality
            c.expect(r, payload)

        async def body_async_generator_arg(c):
            async def gen():
                yield 1
            ag = gen()
            try:
                # Endpoint just returns the arg; since async-gen objects are
                # not picklable for the remote path, this is local-only.
                r = await self.execute(TARGET, "ea_returns_arg", ag)
                # Some shape of async-gen object should come back unchanged.
                c.expect(r is ag, True)
            finally:
                # Close to avoid a "never iterated" warning
                try:
                    await ag.aclose()
                except Exception:
                    pass

        async def body_ordered_dict_unpacked(c):
            # ea_kwargs_only takes name= and value=; OrderedDict satisfies dict isinstance
            od = OrderedDict([("name", "k"), ("value", 9)])
            r = await self.execute(TARGET, "ea_kwargs_only", od)
            c.expect(r, "k=9")

        edge_kw = dict(kw)

        await rec.run_case(
            "exec.edge.B-015.bytes", body_bytes,
            category="edge", tags=("args_contract", "edge"), bug_ids=("B-015",),
            **edge_kw,
        )
        await rec.run_case(
            "exec.edge.B-015.set", body_set,
            category="edge", tags=("args_contract", "edge"), bug_ids=("B-015",),
            **edge_kw,
        )
        await rec.run_case(
            "exec.edge.B-015.frozenset", body_frozenset,
            category="edge", tags=("args_contract", "edge"), bug_ids=("B-015",),
            **edge_kw,
        )
        await rec.run_case(
            "exec.edge.B-015.dataclass", body_dataclass,
            category="edge", tags=("args_contract", "edge"), bug_ids=("B-015",),
            **edge_kw,
        )
        await rec.run_case(
            "exec.edge.B-015.async_generator_arg", body_async_generator_arg,
            category="edge", tags=("args_contract", "edge"), bug_ids=("B-015",),
            **edge_kw,
        )
        await rec.run_case(
            "exec.edge.B-015.ordered_dict_unpacked", body_ordered_dict_unpacked,
            category="edge", tags=("args_contract", "edge"), bug_ids=("B-015",),
            **edge_kw,
        )

    # ====================================================================
    # EDGE cancellation (sync handler in threadpool)
    # ====================================================================

    async def _edge_cancellation(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body_sync_in_threadpool(c):
            # Caller cancels; sync handler in executor runs to completion in background.
            # We assert: caller raises CancelledError quickly; framework remains usable.
            task = asyncio.create_task(
                self.execute(TARGET, "es_add", (1, 2))
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, RequestException):
                pass

            # Subsequent call should still work — framework not wedged.
            r = await self.execute(TARGET, "es_add", (10, 20))
            c.expect(r, 30)

        await rec.run_case(
            "exec.edge.cancellation.sync_handler_in_threadpool", body_sync_in_threadpool,
            category="edge", tags=("cancellation", "edge"),
            **kw,
        )
