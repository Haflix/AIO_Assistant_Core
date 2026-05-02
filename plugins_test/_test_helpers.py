"""
Shared helper for AIO Assistant Core test framework.

Usage from a suite plugin:

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from _test_helpers import CaseRecorder, RecorderError  # noqa: E402

    rec = CaseRecorder("TestExecuteSuite", "0.0.1", self._plugin_core)

    async def body(c):
        result = await self.execute("TestExecuteTarget", "ea_add", (2, 3), hosts=c.hosts)
        c.expect(result, 5)

    await rec.run_case("exec.value.aa.tuple", body, hosts=("local",))
    return rec.to_dict()

The recorder enforces:
- Five-state status (pass / fail / error / skip / unexpected_pass)
- Required `expected_signature` for `expected_status="fail"` cases
- Hard outer timeout per case (default 30s) so the runner cannot itself hang
- Plugin-set drift detection between cases (via `core.plugins.keys()` snapshot)
- Host matrix expansion: a case body runs once per host in `hosts`; sub-case IDs are
  always `<base_id>.<host>` at runtime (filterable; documentation uses base IDs)
"""

import asyncio
import re
import time
import traceback
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple


FRAMEWORK_VERSION = "0.1.0"


class RecorderError(Exception):
    """Raised when a case is mis-declared (e.g. expected_status='fail' without signature)."""


class _SkipSignal(BaseException):
    """Internal signal raised by c.skip() to exit the case body cleanly."""


class _CaseContext:
    """Single test case context. Created and managed by CaseRecorder."""

    def __init__(
        self,
        recorder: "CaseRecorder",
        base_id: str,
        *,
        category: str,
        hosts: str,
        tags: Tuple[str, ...],
        bug_ids: Tuple[str, ...],
        expected_status: str,
        expected_signature: Optional[Dict],
        hard_timeout_s: float,
        destructive: bool,
        slow: bool,
    ):
        self.recorder = recorder
        self.base_id = base_id
        self.hosts = hosts
        self.category = category
        self.tags = list(tags)
        self.bug_ids = list(bug_ids)
        self.expected_status = expected_status
        self.expected_signature = expected_signature
        self.hard_timeout_s = hard_timeout_s
        self.destructive = destructive
        self.slow = slow

        self.actual_value: Any = None
        self.expected_value: Any = None
        self.marker: Optional[str] = None
        self.skip_reason: Optional[str] = None
        self.expected_drift: Optional[Dict] = None
        self._exception_expectation: Optional[Tuple[type, Optional[str]]] = None
        self._t0: Optional[float] = None

        if expected_status == "fail" and expected_signature is None:
            raise RecorderError(
                f"case {base_id}: expected_status='fail' requires expected_signature"
            )

    @property
    def case_id(self) -> str:
        return f"{self.base_id}.{self.hosts}"

    def expect(self, actual: Any, expected: Any) -> None:
        """Assert equality. Records actual/expected on the case."""
        self.actual_value = actual
        self.expected_value = expected
        if actual != expected:
            raise AssertionError(f"expected {expected!r}, got {actual!r}")

    def expect_exception(self, exc_type: type, *, match: Optional[str] = None) -> None:
        """Register that the body is expected to raise `exc_type` (msg matching `match`)."""
        self._exception_expectation = (exc_type, match)

    async def assert_hang(
        self,
        awaitable: Awaitable,
        *,
        timeout_s: float,
        marker: str,
    ) -> None:
        """Assert that `awaitable` does NOT complete within `timeout_s`. On timeout,
        sets `c.marker = marker` and raises AssertionError.
        """
        try:
            await asyncio.wait_for(awaitable, timeout=timeout_s)
        except asyncio.TimeoutError:
            self.marker = marker
            raise AssertionError(f"hang_guard fired: {marker}")

    def set_marker(self, name: str) -> None:
        """Set a free-form marker on the case (used by `expected_signature.marker`)."""
        self.marker = name

    def skip(self, reason: str) -> None:
        """Skip this case. Body must call this BEFORE doing anything else."""
        self.skip_reason = reason
        raise _SkipSignal()

    def set_expected_drift(
        self,
        *,
        added: Tuple[str, ...] = (),
        removed: Tuple[str, ...] = (),
    ) -> None:
        """Declare that this case will leave the plugin set drifted by these names.
        If empty (default), case must net-zero its plugin-set changes.
        """
        self.expected_drift = {"added": list(added), "removed": list(removed)}

    def __enter__(self) -> "_CaseContext":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.perf_counter() - self._t0) * 1000.0

        # CRITICAL: do not swallow BaseException (CancelledError, KeyboardInterrupt, etc.)
        # except for our own _SkipSignal. Returning True on CancelledError would
        # break asyncio cooperative cancellation and mask the recorder's hard-timeout
        # patch path in CaseRecorder._invoke.
        if (
            exc_type is not None
            and exc_type is not _SkipSignal
            and not issubclass(exc_type, Exception)
        ):
            # Record what we know, then let the BaseException propagate.
            tb_str = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
            self._record(
                status="error",
                detail=f"{exc_type.__name__}: {exc_val}",
                exception=f"{exc_type.__name__}: {exc_val}",
                tb_str=tb_str,
                duration_ms=duration_ms,
            )
            return False  # propagate

        if exc_type is _SkipSignal:
            self._record(
                status="skip",
                detail=self.skip_reason or "",
                exception=None,
                tb_str=None,
                duration_ms=duration_ms,
            )
            return True

        if exc_type is None:
            if self._exception_expectation is not None:
                exc_type_expected, _ = self._exception_expectation
                self._record(
                    status="fail",
                    detail=(
                        f"expected exception {exc_type_expected.__name__} "
                        f"but body completed normally"
                    ),
                    exception=None,
                    tb_str=None,
                    duration_ms=duration_ms,
                )
                return False

            if self.expected_status == "fail":
                self._record(
                    status="unexpected_pass",
                    detail=(
                        "case body completed normally; expected_status was 'fail' "
                        "(bug may be fixed — review and flip marker)"
                    ),
                    exception=None,
                    tb_str=None,
                    duration_ms=duration_ms,
                )
                return False

            self._record(
                status="pass",
                detail="",
                exception=None,
                tb_str=None,
                duration_ms=duration_ms,
            )
            return False

        exc_type_name = exc_type.__name__
        exc_msg = str(exc_val)
        tb_str = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))

        if self._exception_expectation is not None:
            exc_type_expected, match = self._exception_expectation
            type_match = isinstance(exc_val, exc_type_expected)
            msg_match = match is None or re.search(match, exc_msg) is not None
            if type_match and msg_match:
                self._record(
                    status="pass",
                    detail="",
                    exception=f"{exc_type_name}: {exc_msg}",
                    tb_str=tb_str,
                    duration_ms=duration_ms,
                )
            else:
                self._record(
                    status="fail",
                    detail=(
                        f"expected {exc_type_expected.__name__} matching {match!r}, "
                        f"got {exc_type_name}: {exc_msg}"
                    ),
                    exception=f"{exc_type_name}: {exc_msg}",
                    tb_str=tb_str,
                    duration_ms=duration_ms,
                )
            return True

        if self.expected_status == "fail":
            if self._signature_matches(exc_type_name, exc_msg):
                self._record(
                    status="pass",
                    detail="expected failure mode reproduced (signature matched)",
                    exception=f"{exc_type_name}: {exc_msg}",
                    tb_str=tb_str,
                    duration_ms=duration_ms,
                )
            else:
                self._record(
                    status="fail",
                    detail=(
                        f"expected failure signature {self.expected_signature} "
                        f"but got {exc_type_name}: {exc_msg} (marker={self.marker})"
                    ),
                    exception=f"{exc_type_name}: {exc_msg}",
                    tb_str=tb_str,
                    duration_ms=duration_ms,
                )
            return True

        if isinstance(exc_val, AssertionError):
            self._record(
                status="fail",
                detail=exc_msg,
                exception=f"{exc_type_name}: {exc_msg}",
                tb_str=tb_str,
                duration_ms=duration_ms,
            )
        else:
            self._record(
                status="error",
                detail=f"{exc_type_name}: {exc_msg}",
                exception=f"{exc_type_name}: {exc_msg}",
                tb_str=tb_str,
                duration_ms=duration_ms,
            )
        return True

    def _signature_matches(self, exc_type_name: str, exc_msg: str) -> bool:
        sig = self.expected_signature or {}
        if "marker" in sig and self.marker != sig["marker"]:
            return False
        if "exception_type" in sig and exc_type_name != sig["exception_type"]:
            return False
        if (
            "message_regex" in sig
            and re.search(sig["message_regex"], exc_msg) is None
        ):
            return False
        return True

    def _record(
        self,
        *,
        status: str,
        detail: str,
        exception: Optional[str],
        tb_str: Optional[str],
        duration_ms: float,
    ) -> None:
        self.recorder._record_case(
            {
                "id": self.case_id,
                "base_id": self.base_id,
                "host": self.hosts,
                "status": status,
                "category": self.category,
                "expected_status": self.expected_status,
                "tags": list(self.tags),
                "bug_ids": list(self.bug_ids),
                "expected_signature": self.expected_signature,
                "destructive": self.destructive,
                "slow": self.slow,
                "skip_reason": self.skip_reason,
                "detail": detail,
                "duration_ms": duration_ms,
                "expected": self.expected_value,
                "actual": self.actual_value,
                "exception": exception,
                "traceback": tb_str,
                "marker": self.marker,
                "expected_drift": self.expected_drift,
            }
        )


class CaseRecorder:
    """Collects case results for a single suite invocation."""

    def __init__(self, suite_name: str, version: str, plugin_core):
        self.suite_name = suite_name
        self.version = version
        self.plugin_core = plugin_core
        self.cases: List[Dict] = []
        self.snapshot: set = set(plugin_core.plugins.keys())
        self._suite_t0 = time.perf_counter()

    async def run_case(
        self,
        base_id: str,
        body: Callable[[_CaseContext], Awaitable[None]],
        *,
        hosts: Tuple[str, ...] = ("local",),
        category: str = "basic",
        tags: Tuple[str, ...] = (),
        bug_ids: Tuple[str, ...] = (),
        expected_status: str = "pass",
        expected_signature: Optional[Dict] = None,
        hard_timeout_s: float = 30.0,
        destructive: bool = False,
        slow: bool = False,
        # Filters (applied per host before body invocation):
        case_ids_filter: Optional[List[str]] = None,
        bug_ids_filter: Optional[List[str]] = None,
        category_filter: Optional[str] = None,
        host_filter: Optional[str] = None,
        skip_slow: bool = False,
        allow_destructive: bool = True,
        remote_available: bool = False,
    ) -> None:
        """Run `body` once per host in `hosts`. Each invocation is a sub-case.

        Filters short-circuit: if a sub-case is filtered out, it is not invoked
        AND not recorded (filtered cases are silent — runner handles reporting
        the filter scope at a higher level).
        """
        if category_filter is not None and category != category_filter:
            return

        if skip_slow and slow:
            return

        if not allow_destructive and destructive:
            return

        for host in hosts:
            if host_filter is not None and host != host_filter:
                continue

            full_id = f"{base_id}.{host}"
            if case_ids_filter is not None:
                if not any(
                    full_id == cid or base_id == cid or full_id.startswith(cid + ".")
                    for cid in case_ids_filter
                ):
                    continue
            if bug_ids_filter is not None:
                if not any(b in bug_ids for b in bug_ids_filter):
                    continue

            c = _CaseContext(
                self,
                base_id,
                category=category,
                hosts=host,
                tags=tags,
                bug_ids=bug_ids,
                expected_status=expected_status,
                expected_signature=expected_signature,
                hard_timeout_s=hard_timeout_s,
                destructive=destructive,
                slow=slow,
            )

            if host != "local" and not remote_available:
                c.skip_reason = "Phase 5 subprocess not up"
                c._t0 = time.perf_counter()
                c._record(
                    status="skip",
                    detail="Phase 5 subprocess not up",
                    exception=None,
                    tb_str=None,
                    duration_ms=0.0,
                )
                continue

            await self._invoke(c, body)
            self._check_drift(c)

    async def _invoke(
        self,
        c: _CaseContext,
        body: Callable[[_CaseContext], Awaitable[None]],
    ) -> None:
        """Run body inside hard-timeout. Body's `with c:` block records the case."""
        try:
            await asyncio.wait_for(self._invoke_body(c, body), timeout=c.hard_timeout_s)
        except asyncio.TimeoutError:
            # If body's __exit__ already recorded (CancelledError path), patch it.
            if self.cases and self.cases[-1]["id"] == c.case_id:
                self.cases[-1]["status"] = "error"
                self.cases[-1]["detail"] = (
                    f"case exceeded hard timeout ({c.hard_timeout_s}s)"
                )
            else:
                # Body never recorded — shouldn't normally happen but be defensive
                c._record(
                    status="error",
                    detail=f"case exceeded hard timeout ({c.hard_timeout_s}s) "
                    f"(no recorded outcome)",
                    exception=None,
                    tb_str=None,
                    duration_ms=c.hard_timeout_s * 1000.0,
                )

    async def _invoke_body(
        self,
        c: _CaseContext,
        body: Callable[[_CaseContext], Awaitable[None]],
    ) -> None:
        with c:
            await body(c)

    def _check_drift(self, c: _CaseContext) -> None:
        """After a case, verify plugin-set drift matches expected_drift.

        Updates the baseline snapshot if the declared drift was non-zero.
        """
        current = set(self.plugin_core.plugins.keys())
        added = current - self.snapshot
        removed = self.snapshot - current

        expected = c.expected_drift or {"added": [], "removed": []}
        expected_added = set(expected.get("added", ()))
        expected_removed = set(expected.get("removed", ()))

        if added != expected_added or removed != expected_removed:
            if self.cases and self.cases[-1]["id"] == c.case_id:
                self.cases[-1]["status"] = "error"
                self.cases[-1]["detail"] = (
                    f"plugin set drifted unexpectedly: "
                    f"added={sorted(added)} (expected {sorted(expected_added)}), "
                    f"removed={sorted(removed)} (expected {sorted(expected_removed)})"
                )
            return

        if expected_added or expected_removed:
            self.snapshot = current

    def _record_case(self, case_dict: Dict) -> None:
        self.cases.append(case_dict)

    def to_dict(self) -> Dict:
        passed = sum(1 for c in self.cases if c["status"] == "pass")
        failed = sum(1 for c in self.cases if c["status"] == "fail")
        errored = sum(1 for c in self.cases if c["status"] == "error")
        skipped = sum(1 for c in self.cases if c["status"] == "skip")
        unexpected_passes = sum(
            1 for c in self.cases if c["status"] == "unexpected_pass"
        )
        return {
            "suite": self.suite_name,
            "version": self.version,
            "passed": passed,
            "failed": failed,
            "errored": errored,
            "skipped": skipped,
            "unexpected_passes": unexpected_passes,
            "total": len(self.cases),
            "duration_ms": (time.perf_counter() - self._suite_t0) * 1000.0,
            "cases": list(self.cases),
        }
