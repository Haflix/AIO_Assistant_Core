"""TestDepResolutionSuite — integration tests for the dependencies feature.

Verifies behavior of the framework AFTER load_plugins has run, NOT the
pure-function logic (that's in TestDepResolutionUnitSuite). Asserts:
  I-1 topo ordering — A precedes B in _build_topo_levels output
  I-2 cycle integration — CycleA/CycleB FAILED_LOAD, CycleC ENABLED
  I-3 plexus name reserved at framework level (would-be plugin named
      'plexus' is rejected; observable via _RESERVED_IDENTIFIER_NAMES)
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Any, Dict, List, Optional  # noqa: E402

from plexus.utils import Plugin  # noqa: E402
from plexus.decorators import async_log_errors, log_errors  # noqa: E402
from plexus.plugin_state import State  # noqa: E402
from plexus.core import _RESERVED_IDENTIFIER_NAMES  # noqa: E402

from _test_helpers import CaseRecorder  # noqa: E402


SUITE_VERSION = "0.1.0"


class TestDepResolutionSuite(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestDepResolutionSuite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestDepResolutionSuite disabled")

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
        rec = CaseRecorder(
            "TestDepResolutionSuite", SUITE_VERSION, self._plexus
        )
        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=False,
        )

        await self._i1_topo_order(rec, kw)
        await self._i2_cycle_integration(rec, kw)
        await self._i3_enable_time_cascade(rec, kw)
        await self._i4_plexus_name_reserved(rec, kw)

        return rec.to_dict()

    async def _i1_topo_order(self, rec, kw):
        async def body(c):
            # Structural assertion via _build_topo_levels (F5): the dep
            # feature placed A and B in distinct levels with B after A.
            # Timing-based assertion was rejected as redundant — gather
            # semantics already guarantee that.
            levels = self._plexus._build_topo_levels()
            # Find the level of each fixture.
            a_level = None
            b_level = None
            for idx, level in enumerate(levels):
                if "TestDepResolutionA" in level:
                    a_level = idx
                if "TestDepResolutionB" in level:
                    b_level = idx
            assert a_level is not None, "A not placed in any level"
            assert b_level is not None, "B not placed in any level"
            assert a_level < b_level, (
                f"Expected A's level < B's level; got A={a_level} B={b_level}"
            )
            # Both should be ENABLED.
            ps_a = self._plexus.plugin_states["TestDepResolutionA"]
            ps_b = self._plexus.plugin_states["TestDepResolutionB"]
            c.expect(ps_a.state, State.ENABLED)
            c.expect(ps_b.state, State.ENABLED)

        await rec.run_case(
            "deps.integration.topo_order_b_after_a", body,
            tags=("deps", "integration", "topo"),
            category="integration", **kw
        )

    async def _i2_cycle_integration(self, rec, kw):
        async def body(c):
            # CycleA and CycleB are required-cycle; both should be FAILED_LOAD.
            # CycleC is unrelated; should be ENABLED.
            ps_ca = self._plexus.plugin_states.get("TestDepResolutionCycleA")
            ps_cb = self._plexus.plugin_states.get("TestDepResolutionCycleB")
            ps_cc = self._plexus.plugin_states.get("TestDepResolutionCycleC")
            assert ps_ca is not None and ps_cb is not None and ps_cc is not None, (
                "Cycle fixtures must be registered in test_config.yml"
            )
            c.expect(ps_ca.state, State.FAILED_LOAD)
            c.expect(ps_cb.state, State.FAILED_LOAD)
            c.expect(ps_cc.state, State.ENABLED)

        await rec.run_case(
            "deps.integration.cycle_marks_members_failed", body,
            tags=("deps", "integration", "cycle"),
            category="integration", **kw
        )

    async def _i3_enable_time_cascade(self, rec, kw):
        async def body(c):
            # TestDepResolutionCrash crashes on_enable (raise_on_enable=true);
            # framework rollback transitions it to INACTIVE (not FAILED_LOAD).
            # TestDepResolutionCrashDependent required-deps Crash; Hook 5's
            # enable-time precheck should transition the dependent to
            # FAILED_LOAD before its on_enable runs.
            ps_crash = self._plexus.plugin_states.get("TestDepResolutionCrash")
            ps_dep = self._plexus.plugin_states.get(
                "TestDepResolutionCrashDependent"
            )
            assert ps_crash is not None and ps_dep is not None, (
                "Crash + Dependent fixtures must be registered"
            )
            # Crash plugin ends up INACTIVE (rolled back from ENABLING).
            c.expect(ps_crash.state, State.INACTIVE)
            # Dependent gets FAILED_LOAD via Hook 5's enable-time cascade.
            c.expect(ps_dep.state, State.FAILED_LOAD)

        await rec.run_case(
            "deps.integration.enable_time_cascade", body,
            tags=("deps", "integration", "cascade", "enable_time"),
            category="integration", **kw
        )

    async def _i4_plexus_name_reserved(self, rec, kw):
        async def body(c):
            # Frame-level check: "plexus" appears in the reserved set so
            # any attempt to load a plugin named "plexus" via
            # load_plugin_with_conf would be rejected at name validation
            # (raising ValueError containing the "reserved name" phrase).
            assert "plexus" in _RESERVED_IDENTIFIER_NAMES, (
                "'plexus' must be in _RESERVED_IDENTIFIER_NAMES"
            )

        await rec.run_case(
            "deps.integration.plexus_name_reserved", body,
            tags=("deps", "integration", "reserved"),
            category="integration", **kw
        )
