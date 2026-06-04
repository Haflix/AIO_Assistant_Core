"""TestDepResolutionUnitSuite — pure-function unit tests for plexus.dependencies.

Self-contained: imports `resolve`, `parse_dependencies`, `DependencySpec`,
`PLEXUS_SELF_NAME` and exercises them with synthetic dicts. Does NOT depend
on other plugins being loaded.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Any, Dict, List, Optional  # noqa: E402

from packaging.specifiers import SpecifierSet  # noqa: E402

from plexus.utils import Plugin  # noqa: E402
from plexus.decorators import async_log_errors, log_errors  # noqa: E402
from plexus.dependencies import (  # noqa: E402
    DependencySpec,
    PLEXUS_SELF_NAME,
    parse_dependencies,
    resolve,
)

from _test_helpers import CaseRecorder  # noqa: E402


SUITE_VERSION = "0.1.0"


def _spec(name: str, version: str = "", optional: bool = False) -> DependencySpec:
    return DependencySpec(name=name, version=SpecifierSet(version), optional=optional)


class TestDepResolutionUnitSuite(Plugin):
    """Pure-function unit suite for plexus.dependencies."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestDepResolutionUnitSuite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestDepResolutionUnitSuite disabled")

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
            "TestDepResolutionUnitSuite", SUITE_VERSION, self._plexus
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

        # resolve.* cases
        await self._u1_valid_chain(rec, kw)
        await self._u2_missing_required(rec, kw)
        await self._u3_version_mismatch_required(rec, kw)
        await self._u4_optional_missing(rec, kw)
        await self._u5_optional_version_mismatch(rec, kw)
        await self._u6_cycle(rec, kw)
        await self._u7_self_loop(rec, kw)
        await self._u8_plexus_satisfied(rec, kw)
        await self._u9_plexus_mismatch(rec, kw)
        await self._u10_cascade(rec, kw)
        await self._u11_optional_saves_dependent(rec, kw)
        await self._u12_non_pep440_target(rec, kw)
        await self._u13_empty_spec_matches_any(rec, kw)
        await self._u14_optional_no_ordering_edge(rec, kw)
        await self._u15_disabled_in_config(rec, kw)
        await self._u16_failed_load_names(rec, kw)
        await self._u17_cycle_wins_precedence(rec, kw)
        await self._u18_invalid_fallback_version(rec, kw)
        await self._u19_first_failure_wins(rec, kw)
        await self._u20_plexus_prerelease(rec, kw)
        await self._u21_plexus_only_deps(rec, kw)

        # parse.* cases
        await self._p1_non_dict_top_level(rec, kw)
        await self._p2_null_entry(rec, kw)
        await self._p3_missing_version(rec, kw)
        await self._p4_invalid_specifier(rec, kw)
        await self._p5_optional_not_bool(rec, kw)
        await self._p6_strip_whitespace(rec, kw)
        await self._p7_int_key_rejected(rec, kw)
        await self._p8_float_version_rejected(rec, kw)
        await self._p9_empty_inputs(rec, kw)
        await self._p10_string_shorthand_rejected(rec, kw)
        await self._p11_string_at_top_level(rec, kw)

        return rec.to_dict()

    # ----- resolve cases -----

    async def _u1_valid_chain(self, rec, kw):
        async def body(c):
            deps = {"A": [], "B": [_spec("A", ">=1.0")]}
            r = resolve(deps, {"A": "1.0.0", "B": "1.0.0"}, "0.41.1")
            c.expect(r.topo_order, ["A", "B"])
            c.expect(dict(r.failed), {})

        await rec.run_case(
            "resolve.chain.orders_deps_first", body,
            tags=("resolve", "topo"), category="resolve", **kw
        )

    async def _u2_missing_required(self, rec, kw):
        async def body(c):
            r = resolve({"B": [_spec("A", ">=1.0")]}, {"B": "1.0"}, "0.41.1")
            assert "B" in r.failed and "missing" in r.failed["B"], r.failed
            c.expect("B" in r.topo_order, False)

        await rec.run_case(
            "resolve.missing.required_fails", body,
            tags=("resolve", "missing"), category="resolve", **kw
        )

    async def _u3_version_mismatch_required(self, rec, kw):
        async def body(c):
            deps = {"A": [], "B": [_spec("A", ">=2.0")]}
            r = resolve(deps, {"A": "1.0", "B": "1.0"}, "0.41.1")
            assert "B" in r.failed and "satisfy" in r.failed["B"], r.failed

        await rec.run_case(
            "resolve.mismatch.required_fails", body,
            tags=("resolve", "version"), category="resolve", **kw
        )

    async def _u4_optional_missing(self, rec, kw):
        async def body(c):
            r = resolve({"B": [_spec("A", ">=1.0", True)]}, {"B": "1.0"}, "0.41.1")
            c.expect(dict(r.failed), {})
            c.expect("B" in r.topo_order, True)
            c.expect(len(r.optional_warnings), 1)

        await rec.run_case(
            "resolve.optional.missing_warns", body,
            tags=("resolve", "optional"), category="resolve", **kw
        )

    async def _u5_optional_version_mismatch(self, rec, kw):
        async def body(c):
            deps = {"A": [], "B": [_spec("A", ">=2.0", True)]}
            r = resolve(deps, {"A": "1.0", "B": "1.0"}, "0.41.1")
            c.expect(dict(r.failed), {})
            c.expect(len(r.optional_warnings), 1)

        await rec.run_case(
            "resolve.optional.mismatch_warns", body,
            tags=("resolve", "optional"), category="resolve", **kw
        )

    async def _u6_cycle(self, rec, kw):
        async def body(c):
            deps = {"A": [_spec("B")], "B": [_spec("A")], "C": []}
            r = resolve(deps, {"A": "1", "B": "1", "C": "1"}, "0.41.1")
            assert "A" in r.failed and "B" in r.failed, r.failed
            assert "cycle" in r.failed["A"].lower()
            c.expect(r.topo_order, ["C"])

        await rec.run_case(
            "resolve.cycle.members_fail", body,
            tags=("resolve", "cycle"), category="resolve", **kw
        )

    async def _u7_self_loop(self, rec, kw):
        async def body(c):
            r = resolve({"A": [_spec("A")]}, {"A": "1"}, "0.41.1")
            assert "A" in r.failed and "cycle" in r.failed["A"].lower(), r.failed

        await rec.run_case(
            "resolve.cycle.self_loop_fails", body,
            tags=("resolve", "cycle"), category="resolve", **kw
        )

    async def _u8_plexus_satisfied(self, rec, kw):
        async def body(c):
            r = resolve(
                {"A": [_spec(PLEXUS_SELF_NAME, ">=0.41")]},
                {"A": "1"}, "0.41.1",
            )
            c.expect(dict(r.failed), {})
            c.expect("A" in r.topo_order, True)

        await rec.run_case(
            "resolve.plexus.satisfied", body,
            tags=("resolve", "plexus"), category="resolve", **kw
        )

    async def _u9_plexus_mismatch(self, rec, kw):
        async def body(c):
            r = resolve(
                {"A": [_spec(PLEXUS_SELF_NAME, ">=99.0")]},
                {"A": "1"}, "0.41.1",
            )
            assert "A" in r.failed and "plexus" in r.failed["A"].lower(), r.failed

        await rec.run_case(
            "resolve.plexus.mismatch_fails", body,
            tags=("resolve", "plexus"), category="resolve", **kw
        )

    async def _u10_cascade(self, rec, kw):
        async def body(c):
            deps = {"A": [_spec("Missing")], "B": [_spec("A")]}
            r = resolve(deps, {"A": "1", "B": "1"}, "0.41.1")
            assert "A" in r.failed and "B" in r.failed
            # Short-form reason references upstream by name.
            assert "A" in r.failed["B"], r.failed["B"]

        await rec.run_case(
            "resolve.cascade.required_propagates", body,
            tags=("resolve", "cascade"), category="resolve", **kw
        )

    async def _u11_optional_saves_dependent(self, rec, kw):
        async def body(c):
            deps = {"A": [_spec("Missing")], "B": [_spec("A", optional=True)]}
            r = resolve(deps, {"A": "1", "B": "1"}, "0.41.1")
            assert "A" in r.failed and "B" not in r.failed
            c.expect("B" in r.topo_order, True)

        await rec.run_case(
            "resolve.cascade.optional_saves", body,
            tags=("resolve", "cascade", "optional"),
            category="resolve", **kw
        )

    async def _u12_non_pep440_target(self, rec, kw):
        async def body(c):
            deps = {"A": [], "B": [_spec("A", ">=1.0")]}
            r = resolve(deps, {"A": "banana", "B": "1.0"}, "0.41.1")
            assert "B" in r.failed, r.failed

        await rec.run_case(
            "resolve.target_version.non_pep440_fails", body,
            tags=("resolve", "version"), category="resolve", **kw
        )

    async def _u13_empty_spec_matches_any(self, rec, kw):
        async def body(c):
            deps = {"A": [], "B": [_spec("A", "")]}
            r = resolve(deps, {"A": "banana", "B": "1.0"}, "0.41.1")
            c.expect(dict(r.failed), {})
            c.expect("B" in r.topo_order, True)

        await rec.run_case(
            "resolve.spec.empty_matches_any", body,
            tags=("resolve", "version"), category="resolve", **kw
        )

    async def _u14_optional_no_ordering_edge(self, rec, kw):
        async def body(c):
            deps = {"A": [_spec("B", optional=True)], "B": []}
            r = resolve(deps, {"A": "1", "B": "1"}, "0.41.1")
            c.expect(dict(r.failed), {})
            assert "A" in r.topo_order and "B" in r.topo_order

        await rec.run_case(
            "resolve.optional.no_ordering_edge", body,
            tags=("resolve", "optional", "topo"),
            category="resolve", **kw
        )

    async def _u15_disabled_in_config(self, rec, kw):
        async def body(c):
            r = resolve(
                {"B": [_spec("A")]}, {"B": "1"}, "0.41.1",
                disabled_in_config={"A"},
            )
            assert "B" in r.failed
            assert "disabled in config.yml" in r.failed["B"], r.failed["B"]

        await rec.run_case(
            "resolve.classify.disabled_in_config", body,
            tags=("resolve", "classify"), category="resolve", **kw
        )

    async def _u16_failed_load_names(self, rec, kw):
        async def body(c):
            r = resolve(
                {"B": [_spec("A")]}, {"B": "1"}, "0.41.1",
                failed_load_names={"A"},
            )
            assert "B" in r.failed
            assert "failed to load" in r.failed["B"], r.failed["B"]

        await rec.run_case(
            "resolve.classify.failed_load_upstream", body,
            tags=("resolve", "classify"), category="resolve", **kw
        )

    async def _u17_cycle_wins_precedence(self, rec, kw):
        async def body(c):
            deps = {"A": [_spec("B")], "B": [_spec("A")]}
            r = resolve(
                deps, {"A": "1", "B": "1"}, "0.41.1",
                failed_load_names={"A"},  # claim A failed-to-load
            )
            # Cycle reason should win since step 1 runs first.
            assert "cycle" in r.failed["A"].lower(), r.failed["A"]
            assert "cycle" in r.failed["B"].lower(), r.failed["B"]

        await rec.run_case(
            "resolve.classify.cycle_wins_precedence", body,
            tags=("resolve", "classify", "cycle"),
            category="resolve", **kw
        )

    async def _u18_invalid_fallback_version(self, rec, kw):
        async def body(c):
            # Framework fallback "0.0.0 - not given" is not valid PEP 440
            deps = {"A": [], "B": [_spec("A", ">=1.0")]}
            r = resolve(
                deps, {"A": "0.0.0 - not given", "B": "1"}, "0.41.1",
            )
            assert "B" in r.failed, r.failed

        await rec.run_case(
            "resolve.target_version.invalid_fallback", body,
            tags=("resolve", "version"), category="resolve", **kw
        )

    async def _u19_first_failure_wins(self, rec, kw):
        async def body(c):
            deps = {
                "A": [],
                "C": [_spec("Missing1"), _spec("A", ">=99.0")],
            }
            r = resolve(deps, {"A": "1", "C": "1"}, "0.41.1")
            assert "C" in r.failed
            assert "Missing1" in r.failed["C"], r.failed["C"]

        await rec.run_case(
            "resolve.plugin.first_failure_wins", body,
            tags=("resolve",), category="resolve", **kw
        )

    async def _u20_plexus_prerelease(self, rec, kw):
        async def body(c):
            r = resolve(
                {"A": [_spec(PLEXUS_SELF_NAME, ">=0.41,<1.0")]},
                {"A": "1"}, "0.42.0a1",
            )
            c.expect(dict(r.failed), {})

        await rec.run_case(
            "resolve.plexus.prerelease_satisfies", body,
            tags=("resolve", "plexus", "prerelease"),
            category="resolve", **kw
        )

    async def _u21_plexus_only_deps(self, rec, kw):
        async def body(c):
            deps = {"A": [_spec(PLEXUS_SELF_NAME, ">=0.41")]}
            r = resolve(deps, {"A": "1"}, "0.41.1")
            c.expect(dict(r.failed), {})
            c.expect(r.topo_order, ["A"])

        await rec.run_case(
            "resolve.plugin.plexus_only_deps", body,
            tags=("resolve", "plexus"), category="resolve", **kw
        )

    # ----- parse cases -----

    async def _p1_non_dict_top_level(self, rec, kw):
        async def body(c):
            specs, field, reason = parse_dependencies(["oops"])
            c.expect(specs, [])
            c.expect(field, "")
            assert reason is not None

        await rec.run_case(
            "parse.shape.non_dict_top_level_rejected", body,
            tags=("parse", "shape"), category="parse", **kw
        )

    async def _p2_null_entry(self, rec, kw):
        async def body(c):
            specs, field, reason = parse_dependencies({"A": None})
            c.expect(field, "A")
            assert reason is not None and "null" in reason, reason

        await rec.run_case(
            "parse.shape.null_entry_rejected", body,
            tags=("parse", "shape"), category="parse", **kw
        )

    async def _p3_missing_version(self, rec, kw):
        async def body(c):
            specs, field, reason = parse_dependencies({"A": {}})
            c.expect(field, "A.version")

        await rec.run_case(
            "parse.shape.missing_version_rejected", body,
            tags=("parse", "shape"), category="parse", **kw
        )

    async def _p4_invalid_specifier(self, rec, kw):
        async def body(c):
            specs, field, reason = parse_dependencies(
                {"A": {"version": "not-a-spec"}}
            )
            c.expect(field, "A.version")

        await rec.run_case(
            "parse.shape.invalid_specifier_rejected", body,
            tags=("parse", "shape"), category="parse", **kw
        )

    async def _p5_optional_not_bool(self, rec, kw):
        async def body(c):
            specs, field, reason = parse_dependencies(
                {"A": {"version": ">=1.0", "optional": "definitely"}}
            )
            c.expect(field, "A.optional")

        await rec.run_case(
            "parse.shape.optional_not_bool_rejected", body,
            tags=("parse", "shape"), category="parse", **kw
        )

    async def _p6_strip_whitespace(self, rec, kw):
        async def body(c):
            specs, field, reason = parse_dependencies(
                {"  A  ": {"version": ">=1.0"}}
            )
            c.expect(len(specs), 1)
            c.expect(specs[0].name, "A")

        await rec.run_case(
            "parse.target.whitespace_stripped", body,
            tags=("parse", "shape"), category="parse", **kw
        )

    async def _p7_int_key_rejected(self, rec, kw):
        async def body(c):
            specs, field, reason = parse_dependencies({42: {"version": "1.0"}})
            assert reason is not None and "str" in reason, reason

        await rec.run_case(
            "parse.target.non_string_key_rejected", body,
            tags=("parse", "shape"), category="parse", **kw
        )

    async def _p8_float_version_rejected(self, rec, kw):
        async def body(c):
            specs, field, reason = parse_dependencies({"A": {"version": 1.0}})
            c.expect(field, "A.version")
            assert reason is not None and "string" in reason.lower(), reason

        await rec.run_case(
            "parse.version.float_rejected", body,
            tags=("parse", "shape"), category="parse", **kw
        )

    async def _p9_empty_inputs(self, rec, kw):
        async def body(c):
            c.expect(parse_dependencies({}), ([], None, None))
            c.expect(parse_dependencies(None), ([], None, None))

        await rec.run_case(
            "parse.empty.returns_empty_list", body,
            tags=("parse", "shape"), category="parse", **kw
        )

    async def _p10_string_shorthand_rejected(self, rec, kw):
        async def body(c):
            specs, field, reason = parse_dependencies({"A": ">=1.0"})
            c.expect(field, "A")
            assert reason is not None and "version:" in reason, reason

        await rec.run_case(
            "parse.shape.string_shorthand_hint", body,
            tags=("parse", "shape"), category="parse", **kw
        )

    async def _p11_string_at_top_level(self, rec, kw):
        async def body(c):
            specs, field, reason = parse_dependencies("None")
            c.expect(field, "")
            assert reason is not None

        await rec.run_case(
            "parse.shape.string_top_level_rejected", body,
            tags=("parse", "shape"), category="parse", **kw
        )
