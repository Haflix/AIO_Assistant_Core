"""Plugin dependency parsing, validation, topo-sort, version checking.

Pure helpers called by core.py during plugin load + enable. No side
effects, no Plexus reference, easy to unit-test in isolation.

Public surface:
    DependencySpec        — frozen dataclass for one parsed dep
    DepResolutionResult   — dataclass returned by resolve()
    PLEXUS_SELF_NAME      — sentinel for framework self-version
    parse_dependencies    — YAML dict -> List[DependencySpec]
    resolve               — end-to-end resolution

Everything else (leading underscore) is module-private.
"""
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


PLEXUS_SELF_NAME = "plexus"


@dataclass(frozen=True)
class DependencySpec:
    name: str
    version: SpecifierSet
    optional: bool


@dataclass
class DepResolutionResult:
    topo_order: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    optional_warnings: List[str] = field(default_factory=list)


def parse_dependencies(
    raw: object,
) -> Tuple[List[DependencySpec], Optional[str], Optional[str]]:
    """Parse a raw `dependencies:` YAML value into list of DependencySpec.

    Returns (specs, None, None) on success.
    Returns ([], field, reason) on shape error. `field` is the location
    of the error: "" for top-level, "<target>" for entry-level,
    "<target>.<subfield>" for sub-field errors. Caller composes the log
    line as f"dependencies{'.' if field else ''}{field}: {reason}".
    Accepts None / empty dict (returns ([], None, None)).

    Shape rules:
    - top-level: None or dict
    - each KEY must be isinstance(key, str); reject int/float/bool keys
      BEFORE .strip() (which crashes on non-str)
    - target name is whitespace-stripped; empty rejected
    - each VALUE is a dict with required `version` (str) and optional
      `optional` (bool, default False)
    - VERSION must pass isinstance(version_raw, str) BEFORE SpecifierSet()
      since YAML `version: 1.0` (unquoted) parses as float -> SpecifierSet(1.0)
      raises AttributeError. Reject with "version must be a string; got <type>"
    - null entry values are rejected; operator removes the line
    - string-as-entry-value (operator wrote shorthand `Foo: ">=1.0"` instead
      of dict form) is rejected with a fix-suggesting hint
    """
    if raw is None:
        return [], None, None
    if not isinstance(raw, dict):
        return (
            [],
            "",
            f"expected dict or None; got {type(raw).__name__} ({raw!r})",
        )

    specs: List[DependencySpec] = []
    for target, entry in raw.items():
        if not isinstance(target, str):
            return (
                [],
                "",
                f"dependency target name must be str; got "
                f"{type(target).__name__} ({target!r})",
            )
        target_stripped = target.strip()
        if not target_stripped:
            return [], "", "dependency target name cannot be empty/whitespace"

        if entry is None:
            return (
                [],
                target_stripped,
                "entry value is null; remove the line if not needed",
            )
        if isinstance(entry, str):
            return (
                [],
                target_stripped,
                f"expected dict, got str — did you mean "
                f"'version: \"{entry}\"'?",
            )
        if not isinstance(entry, dict):
            return (
                [],
                target_stripped,
                f"expected dict, got {type(entry).__name__}",
            )

        if "version" not in entry:
            return (
                [],
                f"{target_stripped}.version",
                "required field missing",
            )
        version_raw = entry["version"]
        if not isinstance(version_raw, str):
            return (
                [],
                f"{target_stripped}.version",
                f"version must be a string; got {type(version_raw).__name__} "
                f"({version_raw!r}). Quote it in YAML if numeric "
                f"(e.g. version: \"1.0\")",
            )
        # R4-WW-9 / R4-WW-10: reject empty and whitespace-only version
        # strings. The prior code path called SpecifierSet(version_raw)
        # directly, which silently produced a match-all specifier for ''
        # and behaved inconsistently across packaging releases for
        # whitespace-only input. Strip first so both collapse to the
        # same operator-friendly rejection.
        if not version_raw.strip():
            return (
                [],
                f"{target_stripped}.version",
                "empty version string not allowed; specify a PEP 440 "
                "constraint such as '>=1.0' (or omit the dependency "
                "entry if no version constraint is needed)",
            )
        try:
            version_spec = SpecifierSet(version_raw.strip())
        except InvalidSpecifier as e:
            return (
                [],
                f"{target_stripped}.version",
                f"invalid PEP 440 specifier: {e}",
            )

        optional_raw = entry.get("optional", False)
        if not isinstance(optional_raw, bool):
            return (
                [],
                f"{target_stripped}.optional",
                f"expected bool, got {type(optional_raw).__name__}",
            )

        specs.append(
            DependencySpec(
                name=target_stripped,
                version=version_spec,
                optional=optional_raw,
            )
        )
    return specs, None, None


def _check_version(target_version_str: str, spec: SpecifierSet) -> bool:
    """True if target_version satisfies spec.

    Empty SpecifierSet matches all (any-match short-circuits before
    Version() parse). Invalid PEP 440 target version against a non-empty
    spec returns False (cannot satisfy). prereleases=True so pre-release
    framework/plugin bumps (e.g. 0.42.0a1) still satisfy >=0.41,<1.0.

    W4-N1: widen the except to also catch ``TypeError``. If a YAML
    ``version: 1.0`` (unquoted float) reaches us, ``Version(1.0)`` raises
    ``TypeError`` (not ``InvalidVersion``); without this guard the
    exception escapes ``resolve()`` and crashes startup.
    """
    if not str(spec):
        return True
    try:
        version = Version(target_version_str)
    except (InvalidVersion, TypeError):
        return False
    return spec.contains(version, prereleases=True)


def _required_adjacency(
    plugin_deps: Dict[str, List[DependencySpec]],
) -> Dict[str, Set[str]]:
    """Adjacency keyed by plugin name -> set of required-dep target names.

    Optional deps and the plexus self-sentinel contribute NO edges.
    Targets outside plugin_deps keys are still included; cycle detection
    ignores those edges, topo sort handles missing-target via filtering.
    """
    adj: Dict[str, Set[str]] = {}
    for name, spec_list in plugin_deps.items():
        targets: Set[str] = set()
        for s in spec_list:
            if s.optional or s.name == PLEXUS_SELF_NAME:
                continue
            targets.add(s.name)
        adj[name] = targets
    return adj


def _detect_cycles(
    plugin_deps: Dict[str, List[DependencySpec]],
) -> List[List[str]]:
    """Find every SCC of size > 1 or singleton with self-loop.

    Uses Tarjan's SCC algorithm (iterative). Considers only required edges
    to existing plugins (no plexus self-sentinel, no missing targets).
    Returns list of cycles; each cycle is a list of plugin names.
    """
    full_adj = _required_adjacency(plugin_deps)
    adj: Dict[str, Set[str]] = {
        name: {t for t in targets if t in plugin_deps}
        for name, targets in full_adj.items()
    }

    index_counter = [0]
    stack: List[str] = []
    on_stack: Set[str] = set()
    indices: Dict[str, int] = {}
    lowlinks: Dict[str, int] = {}
    sccs: List[List[str]] = []

    def strongconnect(root: str) -> None:
        work: List[List] = [[root, iter(sorted(adj.get(root, set())))]]
        indices[root] = index_counter[0]
        lowlinks[root] = index_counter[0]
        index_counter[0] += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            node, it = work[-1]
            recursed = False
            for child in it:
                if child not in indices:
                    indices[child] = index_counter[0]
                    lowlinks[child] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append([child, iter(sorted(adj.get(child, set())))])
                    recursed = True
                    break
                if child in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[child])
            if recursed:
                continue
            if lowlinks[node] == indices[node]:
                scc: List[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.append(w)
                    if w == node:
                        break
                sccs.append(scc)
            work.pop()
            if work:
                parent = work[-1][0]
                lowlinks[parent] = min(lowlinks[parent], lowlinks[node])

    for n in sorted(adj):
        if n not in indices:
            strongconnect(n)

    cycles: List[List[str]] = []
    for scc in sccs:
        if len(scc) > 1:
            cycles.append(scc)
        elif len(scc) == 1 and scc[0] in adj.get(scc[0], set()):
            cycles.append(scc)
    return cycles


def _cycle_path(
    scc: List[str],
    plugin_deps: Dict[str, List[DependencySpec]],
) -> List[str]:
    """Reconstruct a dep-direction edge path through an SCC.

    Tarjan returns SCC members in reverse-DFS-finish order, not edge
    order. For an operator log line, we want arrows that point in the
    actual dependency direction (`A -> B` means A required-deps B).

    Walks required edges from a deterministic start node (alphabetical),
    following the first available unvisited SCC member at each step.
    Stops when no unvisited member is reachable. The caller formats the
    result as `' -> '.join(path + [path[0]])` to close the cycle.

    For non-trivial SCCs (more than one cycle interleaved) the walker
    finds ONE valid cycle path. Other paths exist but operators only need
    to see one to understand the problem.
    """
    if not scc:
        return []
    members = set(scc)
    adj = _required_adjacency(plugin_deps)
    start = sorted(scc)[0]
    path = [start]
    visited = {start}
    current = start
    while True:
        nxt = None
        for cand in sorted(adj.get(current, set())):
            if cand in members and cand not in visited:
                nxt = cand
                break
        if nxt is None:
            break
        path.append(nxt)
        visited.add(nxt)
        current = nxt
    # R2-II-9: the W4-P3 fallback (greedy walker stalled at start for a
    # multi-member SCC) is structurally unreachable when ``_cycle_path``
    # is invoked on an SCC produced by ``_detect_cycles``. Strong
    # connectivity guarantees ``start = sorted(scc)[0]`` has at least one
    # direct outgoing edge to another SCC member, so the first loop
    # iteration always advances ``path`` past length 1. Asserting here
    # makes any future caller that hands ``_cycle_path`` a non-SCC list
    # (the only way to hit this branch) loud instead of silent.
    assert not (len(path) == 1 and len(scc) > 1), (
        "_cycle_path fallback hit — caller passed a non-SCC list "
        "(start node has no direct outgoing edge to another listed member). "
        "Only _detect_cycles is supposed to feed this function."
    )
    return path


def _topo_sort(
    plugin_deps: Dict[str, List[DependencySpec]],
    excluded: Set[str],
) -> List[str]:
    """Kahn's algorithm — returns topo order of surviving plugins.

    Excluded plugins are dropped from the graph entirely. Required edges
    only (optional + plexus excluded by _required_adjacency). Edges to
    non-existent or excluded targets are ignored. Output is deterministic
    via alphabetical tiebreak among ready nodes (Python sets are
    unordered, so without a tiebreak the result would be non-reproducible
    boot-to-boot).
    """
    adj_full = _required_adjacency(plugin_deps)
    nodes = [n for n in plugin_deps if n not in excluded]
    incoming: Dict[str, int] = {n: 0 for n in nodes}
    outgoing: Dict[str, Set[str]] = {n: set() for n in nodes}

    for n in nodes:
        for target in adj_full.get(n, set()):
            if target in excluded or target not in incoming:
                continue
            outgoing[target].add(n)
            incoming[n] += 1

    import heapq
    # W4-P6: heap-based dequeue is O(log N) per step; the prior list-based
    # ready queue with a sort-then-front-pop was O(N) per step.
    # heapq orders by Python comparison — plugin names are strings so the
    # heap-min gives the same alphabetical tiebreak the explicit sort path
    # produced.
    ready = [n for n in nodes if incoming[n] == 0]
    heapq.heapify(ready)
    result: List[str] = []
    while ready:
        n = heapq.heappop(ready)
        result.append(n)
        for dependent in sorted(outgoing.get(n, set())):
            incoming[dependent] -= 1
            if incoming[dependent] == 0:
                heapq.heappush(ready, dependent)
    return result


def resolve(
    plugin_deps: Dict[str, List[DependencySpec]],
    plugin_versions: Dict[str, str],
    plexus_version: str,
    *,
    disabled_in_config: Optional[Set[str]] = None,
    failed_load_names: Optional[Set[str]] = None,
) -> DepResolutionResult:
    """End-to-end dependency resolution.

    Inputs:
      plugin_deps      — ALL loaded plugins, dep-less ones have value=[]
      plugin_versions  — plugin_name -> version str (may be non-PEP-440)
      plexus_version   — framework self-version str
      disabled_in_config — plugin names with enabled=False in config.yml
                           (None normalizes to empty set)
      failed_load_names  — plugin names already in state=FAILED_LOAD from
                           prior framework load failures (None -> empty)

    Reporting:
      First-failure-wins per plugin: when a plugin has multiple failing
      required deps, only the FIRST failing spec's reason is recorded in
      result.failed[name]. Operator fixes that dep, restarts, then sees
      next failure. Single string per plugin is simpler than comma-joined.

    Steps:
      1. Detect cycles (required-only edges). Mark every cycle member
         failed with reason "in dependency cycle: A -> B -> ... -> A".
         Cycle reason wins precedence over any later classification.
      2. For each non-cycle plugin: walk dep list, fail on first
         non-optional miss, append optional_warnings on optional miss.
         Missing-target classification order in code: disabled-in-config
         first, then failed-to-load, then default "missing (not loaded)".
         Version-mismatch is checked AFTER target-existence. plexus
         self-dep checked against plexus_version.
      3. Cascade: any non-cycle plugin with a required dep on an
         already-failed plugin is itself failed with SHORT-FORM reason
         "required dep '<X>' failed" (operators chain-lookup
         result.failed['<X>'] for the upstream reason; avoids O(depth)
         growth). Loop terminates in at most N iterations.
      4. Topo-sort surviving (non-failed) plugins.

    result.failed iteration order matches insertion: cycle members first,
    then per-plugin spec failures in plugin_deps order, then cascades in
    discovery order.
    """
    disabled_in_config = disabled_in_config or set()
    failed_load_names = failed_load_names or set()

    result = DepResolutionResult()

    # Step 1: cycles
    #
    # R4-WW-13: previously every SCC member received an identical
    # "in dependency cycle: A -> B -> C -> A" failure string, so a caller
    # iterating result.failed.items() and logging each entry emitted N
    # identical log lines for an N-member SCC. We now dedupe at the
    # reason-construction layer: each unique cycle (canonicalised by its
    # sorted membership tuple) is recorded in ``logged_cycles`` and only
    # the FIRST member of that cycle receives the full cycle-path text.
    # Subsequent members receive a short pointer reason naming the
    # representative member, so an operator log line is "member of
    # cycle (see <repr>)" rather than the full path repeated. The
    # representative's full message is logged exactly once.
    cycles = _detect_cycles(plugin_deps)
    logged_cycles = set()  # type: Set[Tuple[str, ...]]  # cycle dedup
    for cycle in cycles:
        # Canonical key: sorted membership. Distinct SCCs always have
        # different keys; the same SCC produced twice (defensive) would
        # be deduped via this set.
        cycle_key: Tuple[str, ...] = tuple(sorted(cycle))
        if cycle_key in logged_cycles:
            continue
        logged_cycles.add(cycle_key)

        # Use the edge-direction path walker so arrows in the operator
        # log read like the actual dependency chain (A requires B
        # requires C requires A), not Tarjan's reverse-finish order.
        path = _cycle_path(cycle, plugin_deps) or sorted(cycle)
        cycle_str = " -> ".join(path + [path[0]])
        # If the walker didn't visit every SCC member (can happen for
        # SCCs with multiple interleaved cycles — the walker greedily
        # follows one simple cycle), append the unvisited members so
        # the operator sees the full SCC and not just one path through
        # it. Sorted for deterministic message ordering.
        unvisited = sorted(set(cycle) - set(path))
        suffix = (
            f" (cycle SCC also includes: {', '.join(unvisited)})"
            if unvisited else ""
        )
        representative = path[0]
        for member in cycle:
            if member in result.failed:
                continue
            if member == representative:
                result.failed[member] = (
                    f"in dependency cycle: {cycle_str}{suffix}"
                )
            else:
                result.failed[member] = (
                    f"member of dependency cycle (see "
                    f"'{representative}')"
                )

    # Step 2: per-plugin spec checks
    #
    # R4-WW-14: previously this loop broke on the first failing required
    # dep ("first-failure-wins per plugin"), so an operator fixing the
    # reported failure restarted only to discover a second failing dep on
    # the next pass — N restarts for N failing deps on one plugin. We
    # now collect ALL failing required deps per plugin into
    # ``failed_for_plugin`` and join them into a single failure reason so
    # all failures surface in one pass. Optional misses still go to
    # optional_warnings unchanged.
    for name, spec_list in plugin_deps.items():
        if name in result.failed:
            continue
        failed_for_plugin: List[str] = []  # all failing required-dep reasons
        for spec in spec_list:
            if spec.name == PLEXUS_SELF_NAME:
                if not _check_version(plexus_version, spec.version):
                    reason = (
                        f"plexus self-version '{plexus_version}' does not "
                        f"satisfy required '{spec.version}'"
                    )
                    if spec.optional:
                        result.optional_warnings.append(
                            f"Plugin '{name}' optional dep 'plexus': {reason}"
                        )
                    else:
                        failed_for_plugin.append(reason)
                continue

            if spec.name not in plugin_versions:
                if spec.name in disabled_in_config:
                    reason = (
                        f"required dep '{spec.name}' disabled in config.yml"
                    )
                elif spec.name in failed_load_names:
                    reason = f"required dep '{spec.name}' failed to load"
                else:
                    reason = (
                        f"required dep '{spec.name}' missing "
                        f"(not loaded)"
                    )
                if spec.optional:
                    result.optional_warnings.append(
                        f"Plugin '{name}' optional dep '{spec.name}': {reason}"
                    )
                else:
                    failed_for_plugin.append(reason)
                continue

            target_version = plugin_versions[spec.name]
            if not _check_version(target_version, spec.version):
                reason = (
                    f"required dep '{spec.name}' version '{target_version}' "
                    f"does not satisfy '{spec.version}'"
                )
                if spec.optional:
                    result.optional_warnings.append(
                        f"Plugin '{name}' optional dep '{spec.name}': {reason}"
                    )
                    continue
                else:
                    failed_for_plugin.append(reason)

        if failed_for_plugin:
            # Single-failure path keeps the original wording verbatim so
            # callers parsing the reason string (logs, observers) see no
            # regression. Multi-failure path joins all reasons and
            # prefixes a count so operators see at a glance how many deps
            # need fixing.
            if len(failed_for_plugin) == 1:
                result.failed[name] = failed_for_plugin[0]
            else:
                result.failed[name] = (
                    f"{len(failed_for_plugin)} failing deps: "
                    + "; ".join(failed_for_plugin)
                )

    # Step 3: cascade — BFS propagation of failure.
    #
    # R4-WW-12: replaced the prior `changed = True; while changed:` full-
    # rescan loop, which was O(N^2) on linear dependency chains (length-N
    # chain required N-1 outer passes, each scanning all N plugins).
    # Now we build a reverse-dep adjacency once and walk it as a BFS from
    # the initially-failed set: each newly-failed plugin enqueues only
    # ITS reverse-dependents. Total work is O(V + E). The legacy
    # `changed`/`while changed` keywords no longer drive the algorithm.
    reverse_required_deps: Dict[str, List[str]] = {n: [] for n in plugin_deps}
    for name, spec_list in plugin_deps.items():
        for spec in spec_list:
            if spec.optional or spec.name == PLEXUS_SELF_NAME:
                continue
            # spec.name may be absent from plugin_deps (missing target) —
            # those failures were already classified in Step 2, so we
            # only need reverse edges between loaded plugins.
            if spec.name in reverse_required_deps:
                reverse_required_deps[spec.name].append(name)

    queue: "deque[str]" = deque(result.failed.keys())
    while queue:
        failed_node = queue.popleft()
        for dependent in reverse_required_deps.get(failed_node, []):
            if dependent in result.failed:
                continue
            # Mark this dependent as cascade-failed and enqueue it so
            # ITS reverse-dependents propagate next. The reason is the
            # short-form upstream-pointer ("required dep '<X>' failed");
            # operators chain-lookup result.failed['<X>'] for the
            # ultimate root cause — same wording as the legacy loop.
            result.failed[dependent] = (
                f"required dep '{failed_node}' failed"
            )
            queue.append(dependent)

    # Step 4: topo sort
    result.topo_order = _topo_sort(plugin_deps, set(result.failed.keys()))
    return result
