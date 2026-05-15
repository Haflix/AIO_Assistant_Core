"""TestRunner — orchestrator for the AIO Assistant Core test framework.

Calls each registered suite's `run` endpoint, aggregates results, optionally
dumps JSON to disk, returns a single consolidated report.

The framework's design is documented in plugins_test/test_suite_plan.md.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

from plexus.utils import Plugin  # noqa: E402
from plexus.decorators import async_log_errors, log_errors  # noqa: E402
from _test_helpers import FRAMEWORK_VERSION  # noqa: E402


class TestRunner(Plugin):
    """Orchestrator that drives the per-suite test plugins."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self._configured_suites: List[str] = list(kwargs.get("suites") or [])

    @async_log_errors
    async def on_enable(self):
        self._logger.debug(
            f"TestRunner.on_enable; configured suites={self._configured_suites}"
        )

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("TestRunner.on_disable")

    @async_log_errors
    async def run_all(
        self,
        suites: Optional[List[str]] = None,
        category: Optional[str] = None,
        host: Optional[str] = None,
        case_ids: Optional[List[str]] = None,
        bug_ids: Optional[List[str]] = None,
        dump_path: Optional[str] = None,
        dump_compact: bool = False,
        fail_fast: bool = False,
        skip_slow: bool = False,
        allow_destructive: bool = True,
    ) -> Dict[str, Any]:
        """Run selected suites and return a consolidated report dict."""
        chosen = list(suites) if suites is not None else list(self._configured_suites)

        started_at = datetime.now(timezone.utc).isoformat()
        t0 = time.perf_counter()

        suite_results: List[Dict[str, Any]] = []
        suites_passed = 0
        suites_failed = 0

        for suite_name in chosen:
            suite_result = await self._run_suite_dispatch(
                suite_name,
                category=category,
                hosts=host,
                case_ids=case_ids,
                bug_ids=bug_ids,
                skip_slow=skip_slow,
                allow_destructive=allow_destructive,
            )
            suite_results.append(suite_result)

            suite_clean = (
                suite_result.get("failed", 0) == 0
                and suite_result.get("errored", 0) == 0
                and suite_result.get("unexpected_passes", 0) == 0
            )
            if suite_clean:
                suites_passed += 1
            else:
                suites_failed += 1
                if fail_fast:
                    break

        finished_at = datetime.now(timezone.utc).isoformat()
        duration_ms = (time.perf_counter() - t0) * 1000.0

        summary = {
            "passed": sum(s.get("passed", 0) for s in suite_results),
            "failed": sum(s.get("failed", 0) for s in suite_results),
            "errored": sum(s.get("errored", 0) for s in suite_results),
            "skipped": sum(s.get("skipped", 0) for s in suite_results),
            "unexpected_passes": sum(
                s.get("unexpected_passes", 0) for s in suite_results
            ),
            "total": sum(s.get("total", 0) for s in suite_results),
            "suites_passed": suites_passed,
            "suites_failed": suites_failed,
        }

        review_required: List[str] = []
        for s in suite_results:
            for c in s.get("cases", []):
                if c.get("status") == "unexpected_pass":
                    review_required.append(c["id"])

        report = {
            "framework_version": FRAMEWORK_VERSION,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": duration_ms,
            "summary": summary,
            "suites": suite_results,
            "review_required": review_required,
        }

        if dump_path:
            self._dump(report, dump_path, compact=dump_compact)

        return report

    @async_log_errors
    async def run_suite(
        self,
        suite: str,
        category: Optional[str] = None,
        host: Optional[str] = None,
        case_ids: Optional[List[str]] = None,
        bug_ids: Optional[List[str]] = None,
        skip_slow: bool = False,
        allow_destructive: bool = True,
    ) -> Dict[str, Any]:
        """Dispatch to a single suite's `run` endpoint."""
        return await self._run_suite_dispatch(
            suite,
            category=category,
            hosts=host,
            case_ids=case_ids,
            bug_ids=bug_ids,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
        )

    async def _run_suite_dispatch(
        self,
        suite_name: str,
        *,
        category: Optional[str],
        hosts: Optional[str],
        case_ids: Optional[List[str]],
        bug_ids: Optional[List[str]],
        skip_slow: bool,
        allow_destructive: bool,
    ) -> Dict[str, Any]:
        """Call `core.execute(suite, "run", args=...)` and return its dict.

        On any error during dispatch, return a synthetic suite result with
        status indicating the dispatch failed (so the runner stays stable
        even if a suite plugin is missing or broken).
        """
        try:
            result = await self.execute(
                suite_name,
                "run",
                args={
                    "category": category,
                    "host": hosts,
                    "case_ids": case_ids,
                    "bug_ids": bug_ids,
                    "skip_slow": skip_slow,
                    "allow_destructive": allow_destructive,
                },
            )
            if isinstance(result, dict):
                return result
            self._logger.error(
                f"Suite '{suite_name}' returned non-dict: {type(result).__name__}"
            )
            return self._dispatch_error_result(
                suite_name, f"non-dict return: {type(result).__name__}"
            )
        except Exception as e:
            self._logger.error(f"Suite '{suite_name}' dispatch failed: {e}")
            return self._dispatch_error_result(suite_name, str(e))

    @staticmethod
    def _dispatch_error_result(suite_name: str, detail: str) -> Dict[str, Any]:
        return {
            "suite": suite_name,
            "version": "?",
            "passed": 0,
            "failed": 0,
            "errored": 1,
            "skipped": 0,
            "unexpected_passes": 0,
            "total": 1,
            "duration_ms": 0.0,
            "cases": [
                {
                    "id": f"{suite_name}.dispatch_error",
                    "base_id": f"{suite_name}.dispatch_error",
                    "host": "local",
                    "status": "error",
                    "category": "basic",
                    "expected_status": "pass",
                    "tags": ["runner", "dispatch"],
                    "bug_ids": [],
                    "expected_signature": None,
                    "destructive": False,
                    "slow": False,
                    "skip_reason": None,
                    "detail": f"suite dispatch failed: {detail}",
                    "duration_ms": 0.0,
                    "expected": None,
                    "actual": None,
                    "exception": detail,
                    "traceback": None,
                    "marker": None,
                    "expected_drift": None,
                }
            ],
        }

    def _dump(self, report: Dict[str, Any], path: str, *, compact: bool) -> None:
        """Write report JSON to disk."""
        try:
            target = Path(path).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                if compact:
                    json.dump(report, f, default=_json_default, separators=(",", ":"))
                else:
                    json.dump(report, f, default=_json_default, indent=2)
            self._logger.info(f"Test report written to {target}")
        except Exception as e:
            self._logger.error(f"Failed to write dump_path '{path}': {e}")


def _json_default(obj: Any) -> Any:
    """Best-effort JSON fallback for unusual types in case results."""
    try:
        return repr(obj)
    except Exception:
        return f"<unrepr {type(obj).__name__}>"
