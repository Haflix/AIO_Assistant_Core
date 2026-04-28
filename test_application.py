"""Standalone runner for the AIO Assistant Core test framework.

Loads test_config.yml (only test-framework plugins enabled), drives
TestRunner.run_all, prints a compact summary, dumps full JSON to disk,
then shuts down. Exit code = 0 if no failed/errored cases AND no
unexpected_passes.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from PluginCore import PluginCore


CONFIG_PATH = "test_config.yml"
DUMP_PATH = "_private/test_outputs/phase_1_baseline.json"
RUNNER_PLUGIN = "TestRunner"


def _print_summary(report: Dict[str, Any]) -> None:
    s = report.get("summary", {})
    print()
    print("=" * 72)
    print(
        f"Test framework {report.get('framework_version', '?')}  "
        f"({report.get('duration_ms', 0):.0f} ms)"
    )
    print("=" * 72)
    print(
        f"Suites: {s.get('suites_passed', 0)} passed, "
        f"{s.get('suites_failed', 0)} failed"
    )
    print(
        f"Cases:  {s.get('passed', 0)} passed | "
        f"{s.get('failed', 0)} failed | "
        f"{s.get('errored', 0)} errored | "
        f"{s.get('skipped', 0)} skipped | "
        f"{s.get('unexpected_passes', 0)} unexpected_pass | "
        f"{s.get('total', 0)} total"
    )
    print()

    for suite in report.get("suites", []):
        name = suite.get("suite", "?")
        ver = suite.get("version", "?")
        passed = suite.get("passed", 0)
        failed = suite.get("failed", 0)
        errored = suite.get("errored", 0)
        skipped = suite.get("skipped", 0)
        ups = suite.get("unexpected_passes", 0)
        total = suite.get("total", 0)
        dur = suite.get("duration_ms", 0)
        print(
            f"  [{name} v{ver}] "
            f"{passed}P / {failed}F / {errored}E / {skipped}S / {ups}U "
            f"of {total} ({dur:.0f} ms)"
        )

        # Print non-pass cases for visibility
        non_pass: List[Dict[str, Any]] = [
            c for c in suite.get("cases", []) if c.get("status") != "pass"
        ]
        for c in non_pass:
            cid = c.get("id", "?")
            status = c.get("status", "?")
            detail = c.get("detail", "")
            tag = ""
            if c.get("bug_ids"):
                tag = f" [{','.join(c['bug_ids'])}]"
            print(f"    {status:18} {cid}{tag}")
            if detail and status not in ("skip",):
                # Truncate long details
                d = detail if len(detail) <= 200 else detail[:197] + "..."
                print(f"      └ {d}")

    review = report.get("review_required", [])
    if review:
        print()
        print(f"REVIEW_REQUIRED ({len(review)} cases now passing — flip markers?):")
        for cid in review:
            print(f"  - {cid}")

    print()


def _exit_code(report: Dict[str, Any]) -> int:
    s = report.get("summary", {})
    if s.get("failed", 0) > 0 or s.get("errored", 0) > 0:
        return 1
    if s.get("unexpected_passes", 0) > 0:
        return 2  # bugs may be fixed; review needed
    return 0


async def run_tests() -> int:
    pc = PluginCore(CONFIG_PATH)
    await pc.wait_until_ready()

    try:
        report = await pc.execute(
            RUNNER_PLUGIN,
            "run_all",
            args={
                # Only Phase 1 is built; later phases will extend this list.
                "suites": ["TestExecuteSuite"],
                "category": None,        # both basic and edge
                "host": "local",         # no peer node available
                "dump_path": DUMP_PATH,
                "skip_slow": False,
                "allow_destructive": True,
            },
        )

        if not isinstance(report, dict):
            print(
                f"ERROR: TestRunner returned non-dict: {type(report).__name__}",
                file=sys.stderr,
            )
            return 3

        _print_summary(report)
        print(f"Full report: {Path(DUMP_PATH).resolve()}")
        return _exit_code(report)
    finally:
        await pc.graceful_shutdown()


if __name__ == "__main__":
    try:
        rc = asyncio.run(run_tests())
    except KeyboardInterrupt:
        print("Interrupted by user", file=sys.stderr)
        rc = 130
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        rc = 4
    sys.exit(rc)
