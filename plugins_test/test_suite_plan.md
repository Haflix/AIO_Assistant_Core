# AIO Assistant Core — Test Suite Plan

**Status:** approved after three review cycles. This plan is the canonical reference for the structured test framework that lives as a set of plugins inside the AIO Assistant Core.

**Scope:** Replace the legacy `plugins_test/InteropCaller`, `InteropTarget`, `NotifierPublisher`, `NotifierSubscriber` with a structured framework that exercises framework runtime behavior end-to-end and provides reproductions for every entry in `bugtracker.md`.

---

## 1. Goals

- **Structured, machine-readable output** per case, suite, runner.
- **Bugtracker reproduction.** Every entry in `bugtracker.md` (currently 39, growing) eventually maps to one or more test cases tagged with the bug ID.
- **Single command-line entry.** `core.execute("TestRunner", "run_all")` from REPL/CLI gives one consolidated report. Optional `dump_path` writes JSON to disk for CI.
- **Independence.** Each suite can be run in isolation: `core.execute("TestExecuteSuite", "run")`. Suites do not share state.
- **Filterable.** `run_all(case_ids=["B-015.list"])` runs a single case for repro work.
- **Self-protecting.** Tests with hang/leak failure modes wrap the SUT in outer `wait_for` so the runner cannot itself hang.

## 2. Non-goals

- No pytest / external harness.
- No mock framework. Targets are real plugins with predictable behaviors.
- No bug fixes. Tests reproduce, do not fix.

---

## 3. Architecture

### 3.1 Plugin layout

```
plugins_test/
  test_suite_plan.md             # this file
  _test_helpers.py               # SHARED helper (§5)
  _remote_node/                  # Phase 5 subprocess scaffold
    run_node.py                  # ~50 LOC; sketch in §6 Phase 5
    config.subnode.yml           # minimal subnode config
  TestRunner/                    # orchestrator (Tier-3)
  TestExecuteSuite/              # Phase 1 suite
  TestExecuteTarget/             # Phase 1 fixture (also instantiated as TestExecuteTarget2)
  TestStreamSuite/               # Phase 2
  TestStreamTarget/              # Phase 2 fixture
  TestNotifierSuite/             # Phase 3
  TestNotifierTarget/            # Phase 3 fixture
  TestNotifierBadActor/          # Phase 3 fixture (raises, hangs, cancels)
  TestLifecycleSuite/            # Phase 4
  TestLifecycleVictim/           # Phase 4 fixture (also instantiated as TestLifecycleVictim2)
  TestLifecycleBrokenVersion/    # Phase 4 static fixture for B-007 (plugin_config.yml without version)
  TestLifecycleSentinel/         # Phase 4 static fixture for B-007 (asserted via absence)
  TestRemoteSuite/               # Phase 5
  TestRemoteTarget/              # Phase 5 fixture; remote: true
  TestRemoteVictim/              # Phase 5 fixture; remote: false (B-001 target)
  TestRemoteSpoofer/             # Phase 5 peer-side raw notify_remote helper for B-018
```

### 3.2 Tier discipline

- Targets / Victims / fixtures are Tier-1.
- Suites + Runner are Tier-3.

### 3.3 Discovery

`TestRunner.arguments.suites` lists suites explicitly. Default after all phases land:
```yaml
arguments:
  suites:
    - TestExecuteSuite
    - TestStreamSuite
    - TestNotifierSuite
    - TestLifecycleSuite
    - TestRemoteSuite
```

`TestRunner` is `enabled: true` by default in `config.example.yml`. Suite/target plugins default to `enabled: false`. Empty enabled-suites list returns an empty report with a clear message.

### 3.4 Private-API carve-out

Suites are explicitly allowed to call private-ish PluginCore methods where reproducing a bug requires it:
- `core._reload_plugin(name)` — Lifecycle (B-010, B-016) AND Notifier (B-034, B-040)
- `core._disable_plugin(name)` / `core._enable_plugin(name)` — Lifecycle, Notifier (BadActor on-demand load, B-003 disable angle, B-004)
- `core.load_plugin_with_conf(entry_dict)` — Notifier (BadActor on-demand), Lifecycle
- Mutating `core.requests`, `core._running_loop_task` — Lifecycle B-006 only
- Mutating `core.yaml_config['plugins']` in-memory — Notifier (BadActor on-demand)

Document the dependency in each suite's README. If PluginCore refactors any of these names, suites break loudly — that's intended.

---

## 4. Result schema

### 4.1 Per-case

```python
{
  "id": str,                              # e.g. "exec.B-015.list"; expanded to "<id>.<host>" per host matrix
  "status": "pass" | "fail" | "error" | "skip" | "unexpected_pass",
  "category": "basic" | "edge",           # basic = documented contract / known bug; edge = boundary/stress
  "hosts": List[str],                     # default ["local"]; values: "local", "remote", or specific hostname
  "expected_status": "pass" | "fail",
  "tags": List[str],                      # categorical (e.g. "execute", "args_contract")
  "bug_ids": List[str],                   # explicit ["B-015"]
  "expected_signature": Optional[Dict],   # required when expected_status == "fail"
  "skip_reason": Optional[str],           # set when status == "skip"
  "detail": str,                          # human-readable; "" on pass
  "duration_ms": float,
  "expected": Any | None,                 # for value-comparison cases
  "actual":   Any | None,
  "exception": Optional[str],             # type and message
  "traceback": Optional[str],             # full; consumers can elide
  "marker": Optional[str],                # set by c.set_marker(...)
}
```

### 4.2 Status semantics (5 states)

| Body outcome | `expected_status` | `expected_signature` | Resulting `status` |
|---|---|---|---|
| no exception, all `expect()` held | `pass` | n/a | `pass` |
| no exception, all `expect()` held | `fail` | any | `unexpected_pass` |
| `c.skip(reason)` called | any | n/a | `skip` |
| `expect_exception(T)` registered, body raises T (msg matches) | `pass` | n/a | `pass` |
| `expect_exception(T)` registered, body raises T' ≠ T | `pass` | n/a | `fail` (mismatch counts as failure, not error) |
| AssertionError from `c.expect(...)` | `pass` | n/a | `fail` |
| AssertionError or matched exception | `fail` | matches signature | `pass` |
| AssertionError or exception | `fail` | does NOT match signature | `fail` |
| Other uncaught exception (not from `expect_*`) | `pass` | n/a | `error` |
| Uncaught exception | `fail` | matches signature | `pass` |
| Hard timeout fires | any | n/a | `error` (with detail "case exceeded hard timeout") |

**Signature match** (`expected_signature`):
- If `marker` field set on signature, `c.marker` must equal it.
- If `exception_type` set, raised exception's type must be that name.
- If `message_regex` set, raised exception's message must match.
- Multiple fields → all must match.

The `unexpected_pass` state distinguishes "bug was silently fixed" from regular pass. Reviewers see `runner.review_required` and flip the marker.

### 4.3 Per-suite & runner

```python
# Suite
{
  "suite": str, "version": str,
  "passed": int, "failed": int, "errored": int, "skipped": int,
  "unexpected_passes": int,
  "total": int, "duration_ms": float,
  "cases": List[Case],
}
# Runner
{
  "framework_version": str,
  "started_at": str, "finished_at": str, "duration_ms": float,
  "summary": {
    "passed": int, "failed": int, "errored": int, "skipped": int,
    "unexpected_passes": int,
    "suites_passed": int, "suites_failed": int,
  },
  "suites": List[SuiteResult],
  "review_required": List[str],          # case IDs with status=unexpected_pass
}
```

### 4.4 Runner signature

```python
async def run_all(
    self,
    suites: Optional[List[str]] = None,         # default: configured list
    category: Optional[str] = None,             # "basic" | "edge" | None (both)
    host: Optional[str] = None,                 # filter to a single host variant ("local"|"remote"|None=all)
    case_ids: Optional[List[str]] = None,       # filter cases within selected suites (matches base id OR expanded "<id>.<host>")
    bug_ids: Optional[List[str]] = None,        # filter cases by bug tag
    dump_path: Optional[str] = None,
    dump_compact: bool = False,
    fail_fast: bool = False,
    skip_slow: bool = False,                    # skip cases tagged "slow"
    allow_destructive: bool = True,             # see §4.7
) -> dict
```

### 4.5 Basic vs Edge split

Each case is tagged `category="basic"` or `category="edge"`:

- **`basic`** — exercises a documented contract (in README or CLAUDE.md) OR reproduces a known bug from `bugtracker.md`. Failure here means a documented promise has been broken, or a known regression has surfaced. CI runs basic by default. **Run with `category="basic"`.**
- **`edge`** — exercises boundary conditions, stress patterns, undocumented behavior we want to lock, or unusual input shapes that aren't part of the documented contract but matter for robustness. Failure here is a quality concern, not a contract break. **Run with `category="edge"`.**

`run_all()` with no `category` runs both. CI may run only `category="basic"` for fast feedback and `category="edge"` on a slower cadence.

Within each suite, basic cases are listed first, edge cases follow in a separate sub-table. Case IDs use `<suite>.<group>.<name>` where `<group>` reflects the concern, not basic/edge — the field on the case itself is authoritative.

### 4.6 Host matrix

Each case declares a `hosts: List[str]` (default `["local"]`). The recorder expands a case at execution time into N sub-cases, one per host:

- `hosts=["local"]` (default) — case body invoked once with `host="local"`. Sub-case ID = base ID (no suffix).
- `hosts=["local", "remote"]` — case body invoked twice. Sub-cases get suffixes: `<id>.local` and `<id>.remote`.
- `hosts=["remote"]` — case body invoked once with `host="remote"`. Sub-case ID = `<id>.remote`.
- Specific hostname (e.g. `"test-subnode"`) is also valid.

**Case body API:** the recorder passes the chosen `host` value into the case context (`c.host`). Test code uses `c.host` when constructing calls:
```python
with rec.case("notif.notify.exact_one_sub", hosts=["local", "remote"]) as c:
    count = await self.notify("test/greet", "World", host=c.host)
    c.expect(count, 1)
```

**Auto-skip:** if `c.host == "remote"` (or any non-local hostname) and the Phase 5 subprocess isn't up (suite-level flag), the sub-case skips with `skip_reason="Phase 5 subprocess not up"`. No need for per-case skip code.

**When a case can't matrix-expand cleanly:**
- Different fixture setup per host (e.g. local-side vs subnode-side sub registration) → keep as separate single-host case
- Assertion depends on host (e.g. "calling host='remote' with no peer raises X") → keep as `hosts=["remote"]` only
- Mechanism is intrinsically local (B-002 producer task identity, B-006 running_loop, multi-instance uuid handling, sync chain, lifecycle private-API) → `hosts=["local"]`
- Mechanism is intrinsically remote/wire (B-024 chunk corruption, B-025 partial yield, B-018 spoofing) → `hosts=["remote"]`

**Default host annotations** (applied across all phases, listed by category for compactness rather than per-case):

`hosts=["local", "remote"]` — matrix-expanded:
- Phase 1: `exec.value.*`, `exec.error.no_endpoint`, `exec.error.no_plugin`, `exec.error.endpoint_raises`, `exec.error.endpoint_raises_request_exc`, `exec.error.returns_none`, `exec.error.returns_future`, `exec.timeout.hang_with_timeout`, `exec.B-017.private_from_other`, `exec.large_payload.return_value`, `exec.contract.deep_chain_request_exception_preserves_message`, `discovery.tag.*`
- Phase 2: `stream.async.basic`, `stream.async.empty`, `stream.async.one`, `stream.async.raises_after_2`, `stream.timeout.hanging_gen`, `stream.error.endpoint_not_generator`
- Phase 3: `notif.notify.no_subs`, `notif.notify.exact_one_sub`, `notif.notify.wildcard_match`, `notif.notify.multiple_subs`, `notif.notify.exact_and_wildcard`, `notif.request_topic.basic`, `notif.request_topic.no_sub`, `notif.request_topic_stream.basic`, `notif.request_topic_stream.sync_gen`, `notif.B-023.notify_returns_int_not_raises`, `notif.B-017.priv_via_topic_from_other`

`hosts=["remote"]` only — wire-bug specific:
- Phase 5 dedicated cases (B-001, B-018, B-021, B-024, B-025, B-027, B-028, B-029, B-030, B-032, B-033, B-042) and access-boundary cases (`remote.execute.remote_false_blocked`, `remote.execute.access_false_blocked`, `remote.notify.remote_false_blocked_for_config`)

`hosts=["local"]` (default, no annotation needed) — everything else.

**Why these contract cases stay local-only** (clarification of the bullet-list contract):
- UUID-handling cases (`exec.contract.find_endpoint_uuid_target_plugin_conflict`, `exec.contract.uuid_after_pop_returns_none`, `exec.contract.uuid_invalidated_after_reload`) — UUIDs are local-process identifiers; they don't roundtrip the wire.
- Code-driven sub mechanics (`notif.notify.code_driven`, `notif.unsubscribe.code_driven`) — `Plugin.subscribe` is a local API; remote variants would test B-001's bypass instead, which is its own case.
- Lifecycle / reload / disable / pop / multi-instance / runner meta — intrinsically local per process state.
- Sync API cases — sync calls run in the local threadpool by design; remote sync is undefined.
- `notif.contract.notify_disabled_networking_no_remote_attempt` — assertion is "no remote attempt", so a remote variant is meaningless.
- B-XXX bug-repro cases that test local mechanisms (B-002, B-006, B-008, B-039, B-040, B-041) — bug surface is local.

**Phase 5 dedup:** the basic remote-execution / remote-notify / remote-request_topic cases that v3 had (`remote.execute.basic`, `remote.notify.basic`, `remote.request_topic.basic`, `remote.request_topic_stream.basic`) are removed from Phase 5 — they're now Phase 1/3 cases with `hosts=["local","remote"]`. Phase 5's table only contains wire-only mechanisms.

### 4.7 Destructive-case handling

A case may declare `destructive=True`. Destructive cases:
- have side-effects that survive the case's `finally` block (e.g. running_loop killed, plugin permanently popped from a fixture)
- are by default placed last in their suite's run order
- skip with `skip_reason="destructive case skipped via filter (would leave SUT broken for subsequent cases)"` if `case_ids` filter pulls them out without including all later cases that might depend on the same SUT state

When `allow_destructive=False`, all destructive cases skip cleanly. CI runs without destructive cases by default; manual runs use the default `True`.

The Phase 4 `lifecycle.B-006.running_loop_guard` is the only currently-planned destructive case; its body restarts running_loop in `finally` via `core._running_loop_task = asyncio.create_task(core.running_loop())`, but the `destructive=True` flag is the contract guarantee.

---

## 5. Helper utility

`plugins_test/_test_helpers.py` (shared, sys.path shim).

### 5.1 Import shim

Every suite plugin's `plugin.py` starts:
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _test_helpers import CaseRecorder, RecorderError, hang_guard  # noqa: E402
```

`spec_from_file_location` (PluginCore loader) doesn't interfere; `parent.parent` resolves to `plugins_test/`; `_test_helpers.py` is imported by name via sys.path.

### 5.2 `_CaseContext` API

```python
class _CaseContext:
    actual_value: Any
    expected_value: Any
    marker: Optional[str]
    skip_reason: Optional[str]
    expected_drift: Optional[Dict]   # {"added": [...], "removed": [...]}
    _exception_expectation: Optional[Tuple[type, Optional[str]]]

    # Test-side API:
    def expect(self, actual, expected): ...
    def expect_exception(self, exc_type, *, match=None): ...
    async def assert_hang(self, awaitable, *, timeout_s, marker): ...
    def set_marker(self, name: str): ...
    def skip(self, reason: str): ...                 # raises _SkipSignal
    def set_expected_drift(self, *,                  # for cases that legitimately
                           added: List[str] = (),    # mutate core.plugins
                           removed: List[str] = ()):
        ...
```

Suite recorder snapshots `core.plugins.keys()` at start of suite. After each case, recorder compares against snapshot:
- Drift not matching `expected_drift` → status `error`, detail `"plugin set drifted unexpectedly: added=... removed=..."`
- **Net-zero drift** (e.g. case loads BadActor in setup and unloads in finally; `set_expected_drift(added=[], removed=[])` or simply not declared) → after-case snapshot equals before-case snapshot; baseline unchanged.
- **Non-zero drift declared** (e.g. case intentionally leaves a plugin loaded for subsequent cases; `set_expected_drift(added=["X"])`) → recorder updates baseline to include `X`. Subsequent cases compare against the updated baseline.

### 5.3 Hard-timeout enforcement

Every case is wrapped in `asyncio.wait_for(case_body, timeout=hard_timeout_s)`. Default 30s. Per-case override via `recorder.case(..., hard_timeout_s=...)`.

### 5.4 BadActor on-demand load idiom

`load_plugin_with_conf` short-circuits if entry's `enabled` flag is `False` (PluginCore.py:499-504). The suite must build a fresh entry dict to bypass this:

```python
async def _load_badactor(self):
    entry = {
        "name": "TestNotifierBadActor",
        "enabled": True,
        "path": "./plugins_test/TestNotifierBadActor",
    }
    await self._plugin_core.load_plugin_with_conf(entry)
    await self._plugin_core._enable_plugin("TestNotifierBadActor")

async def _unload_badactor(self):
    await self._plugin_core.pop_plugin("TestNotifierBadActor")

async def _unload_badactor_safe(self):
    """Defensive variant for suite-level finally — never propagates."""
    try:
        await self._plugin_core.pop_plugin("TestNotifierBadActor")
    except Exception as e:
        self._logger.warning(
            f"BadActor defensive unload failed (already gone or half-state): {e}"
        )
```

The case using BadActor calls `_load_badactor()` in `setup`, `_unload_badactor()` in `finally`, and declares `set_expected_drift(added=[], removed=[])`. If the case fails between load and unload, the suite-level `finally` calls `_unload_badactor_safe()` defensively — wrapped in try/except so a defensive-cleanup error doesn't mask the original case failure.

---

## 6. Phases

Each phase is one iteration. Each phase delivers: suite plugin(s), target plugin(s), config update, working `run` endpoint, sample output verified manually.

### Phase 1 — TestExecuteSuite

**Plugins:** `TestRunner`, `TestExecuteSuite`, `TestExecuteTarget` (loaded twice as `TestExecuteTarget` and `TestExecuteTarget2`).

**TestExecuteTarget endpoints:**
- `ea_add(a=0, b=1)` — async
- `es_add(a=0, b=1)` — sync
- `ea_no_args()` — async
- `es_no_args()` — sync
- `ea_kwargs_only(*, name, value)` — async, kwargs only
- `ea_positional_only(a, b, /)` — async, positional only
- `ea_raises()` — async, raises `ValueError("intentional")`
- `es_raises()` — sync, raises `ValueError("intentional")`
- `ea_raises_request_exc()` — async, raises `RequestException("specific")`
- `ea_returns_none()` — async, returns None
- `ea_returns_future()` — async, returns a successfully-resolved Future
- `ea_returns_failing_future()` — async, returns a Future that raises (B-013):
  ```python
  async def ea_returns_failing_future(self):
      loop = asyncio.get_running_loop()
      fut = loop.create_future()
      fut.set_exception(ValueError("intentional"))
      return fut
  ```
- `ea_hang(seconds=10)` — async, awaits sleep
- `ea_private(value=42)` — async, `accessible_by_other_plugins=False`
- `ea_self_call(target_method, plugin_uuid=None)` — async; calls `self.execute("TestExecuteTarget", target_method, plugin_uuid=plugin_uuid)`. Default `plugin_uuid=self.plugin_uuid` so multi-instance loading does not bounce the call to the wrong instance.
- `ea_large_return(size_bytes)` — async, returns bytes; for in-process payload sanity (NOT B-024)

**Test cases (~28):**

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `exec.smoke.multi_instance_distinct` | smoke gate (§11.2); fail short-circuits later multi_instance cases | — | smoke |
| `exec.value.aa.tuple` | async→async tuple `(2,3)` returns 5 | — | execute |
| `exec.value.aa.dict` | async→async dict `{a:7,b:8}` returns 15 | — | execute |
| `exec.value.aa.none_for_zero_args` | `args=None` for zero-arg fn | — | execute |
| `exec.value.as.tuple` | async→sync tuple | — | execute |
| `exec.value.as.dict` | async→sync dict | — | execute |
| `exec.value.aa.kwargs_only_with_dict` | dict args to kwargs-only fn | — | execute |
| `exec.value.aa.kwargs_only_with_tuple_mismatch` | tuple args to kwargs-only fn → RequestException (TypeError wrapped) | — | execute, mismatch |
| `exec.value.aa.positional_only_with_tuple` | tuple args to positional-only fn | — | execute |
| `exec.value.aa.positional_only_with_dict_mismatch` | dict args to positional-only fn → RequestException | — | execute, mismatch |
| `exec.B-015.single_int` | `args=42` silently passed as positional | B-015 | args_contract |
| `exec.B-015.list` | `args=[1,2]` | B-015 | args_contract |
| `exec.B-015.string` | `args="hello"` | B-015 | args_contract |
| `exec.error.no_endpoint` | unknown endpoint → RequestException | — | error |
| `exec.error.no_plugin` | unknown plugin → RequestException | — | error |
| `exec.error.endpoint_raises` | endpoint raises Exception → RequestException with msg | — | error |
| `exec.error.endpoint_raises_request_exc` | endpoint raises RequestException → propagates | — | error |
| `exec.error.returns_none` | endpoint returns None → caller gets None | — | nullable |
| `exec.error.returns_future` | resolved Future → caller gets value | — | future_unwrap |
| `exec.B-013.returns_failing_future` | failing Future → caller hangs (expected_status=fail, marker="outer_wait_for_fired") | B-013 | bug_repro |
| `exec.timeout.hang_with_timeout` | endpoint hangs, caller passes `timeout=2`; assert `2.0 ≤ elapsed ≤ 5.0` (upper bound covers slow Windows scheduler) | — | timeout |
| `exec.B-017.private_from_other` | `ea_private` from other plugin → "Endpoint not found" | B-017 | accessibility |
| `exec.access.private_from_self_pinned_uuid` | `ea_self_call` with `plugin_uuid=self.plugin_uuid` → succeeds | — | accessibility |
| `exec.sync.from_sync` | `execute_sync` from sync context returns | — | sync |
| `exec.multi_instance.distinct_uuids` | both instances loaded; uuids distinct; calls WITH `plugin_uuid` resolve correctly | — | multi_instance |
| `exec.multi_instance.target_by_uuid` | `plugin_uuid=` filter selects specific instance | — | multi_instance |
| `exec.cancellation.entry_reaped` | mid-await cancel; assert request id no longer in `core.requests` within 25s (≥2 cleanup ticks). `hard_timeout_s=40`. | — | cancellation |
| `exec.large_payload.return_value` | endpoint returns 100 KB; received intact (sanity, NOT B-024) | — | payload |

**Cut from earlier drafts:**
- `exec.sync.from_async_raises`, `stream.sync.from_async_raises` — DROPPED. `execute_sync` from async deadlocks (loop frozen on `future.result()`); no clear error path. Filed as deferred test (§11.3).
- `exec.error.disabled_plugin` → MOVED to Phase 4.
- `stream.async.huge_item_local` (101 MB local) → DROPPED. B-024 is wire-only; local has no pickle path.

(All cases above are `category="basic"`.)

**Additional basic cases (added 2026-04-28 after coverage audit):**

These exercise documented contracts whose breakage would be a real regression. All `category="basic"`.

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `exec.contract.deep_chain_request_exception_preserves_message` | A → B → C; C raises `RequestException("specific")`; A's caller receives that exact message and type | — | error, propagation |
| `exec.contract.find_endpoint_uuid_target_plugin_conflict` | `plugin_uuid=X` AND `target_plugin="Y"` where X belongs to a plugin named "Z"; assert "Endpoint not found" (regression-lock current AND-filter behavior) | — | discovery |
| `exec.contract.uuid_after_pop_returns_none` | hold a `plugin_uuid`, pop the plugin, call with that uuid → "Endpoint not found" | — | discovery |
| `exec.contract.uuid_invalidated_after_reload` | hold pre-reload uuid; reload (uuid changes); call with old uuid → "Endpoint not found" | — | discovery, reload |
| `exec.contract.request_context_async_basic` | `core.request_context_async(request)` returns result on success, raises on error | — | api |
| `exec.contract.request_context_sync_basic` | `core.request_context_sync(request)` returns result on success, raises on error | — | api |
| `decorators.async_log_errors_reraises` | function decorated with `@async_log_errors` raises Exception → caller receives Exception (re-raised, logged once) | — | decorators |
| `decorators.async_handle_errors_swallows_generic` | function decorated with `@async_handle_errors(default_return=42)` raises ValueError → caller receives 42 | — | decorators |
| `decorators.async_handle_errors_propagates_request_exception` | function decorated with `@async_handle_errors(default_return=None)` raises RequestException → caller receives RequestException | — | decorators |
| `decorators.async_gen_log_errors_reraises_inside_iter` | async-gen decorator surfaces exception during iteration | — | decorators |
| `decorators.gen_log_errors_smoke` | sync-gen decorator basic | — | decorators |
| `discovery.tag.empty_list_invisible` | endpoint with `tags: []` invisible to `find_endpoints_by_tag("anything")` | — | discovery, tag |
| `discovery.tag.local_only_when_networking_disabled` | networking off → `find_endpoints_by_tag` only returns local matches | — | discovery, tag |
| `discovery.tag.config_shapes` | three plugins: `tags: null`, `tags:` (missing), `tags: []` — all behave identically (invisible) | — | discovery, tag |
| `discovery.tag.multi_tag_endpoint_visible_under_each` | endpoint with `tags: ["a", "b"]` returned by both `find_endpoints_by_tag("a")` and `find_endpoints_by_tag("b")` | — | discovery, tag |
| `runner.meta.framework_version_set` | `runner.framework_version` is non-empty in `run_all` output | — | runner, contract |

**Edge cases:**

These are boundary-condition / unusual-input variants. All `category="edge"`. Failure is a quality concern, not a contract break.

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `exec.edge.B-015.bytes` | `args=b"\x00\x01"` silently passed positional | B-015 | args_contract, edge |
| `exec.edge.B-015.set` | `args={1, 2, 3}` | B-015 | args_contract, edge |
| `exec.edge.B-015.frozenset` | `args=frozenset([1,2])` | B-015 | args_contract, edge |
| `exec.edge.B-015.dataclass` | `args=DataClassInstance()` | B-015 | args_contract, edge |
| `exec.edge.B-015.async_generator_arg` | `args=async_gen_object` | B-015 | args_contract, edge |
| `exec.edge.B-015.ordered_dict_unpacked` | `args=OrderedDict(...)` — `isinstance(dict)` True → unpacked as kwargs (lock current behavior) | B-015 | args_contract, edge |
| `exec.edge.cancellation.sync_handler_in_threadpool` | cancel caller while sync handler running in `_plugin_executor`; assert caller raises CancelledError; worker thread runs to completion in background; no leak | — | cancellation, edge |

### Phase 2 — TestStreamSuite

**Plugins added:** `TestStreamSuite`, `TestStreamTarget`.

**TestStreamTarget endpoints:**
- `ea_gen(n=5, prefix="x", delay_ms=0)` — async generator
- `es_gen(n=5, prefix="x", delay_ms=0)` — sync generator
- `ea_gen_raises_after(n_yielded=2)`
- `ea_gen_infinite()` — for B-002 abandonment
- `ea_gen_one_item()`
- `ea_gen_empty()`
- `es_gen_with_delay(n=3, delay_ms=20)`
- `ea_gen_returns_one_large_item(size_bytes=100_000)` — local-only sanity

**Test cases (~12):**

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `stream.async.basic` | yields 5 items in order | — | basic |
| `stream.sync.basic` | sync gen yields 5 in order | — | basic |
| `stream.async.empty` | empty gen yields zero items | — | edge |
| `stream.async.one` | one-item gen | — | edge |
| `stream.async.raises_after_2` | gen raises after 2 → consumer gets 2 then RequestException | — | error |
| `stream.B-002.consumer_break` | break after 3 items, sleep 15s; assert `request.queue.qsize()` is still growing (producer still running); expected_status=fail, signature marker="producer_still_running". Detection: snapshot qsize at t=15s and t=20s; if qsize_t20 > qsize_t15, marker set. (Avoids private-frame inspection of `core.task_list`.) | B-002 | bug_repro |
| `stream.B-002.consumer_cancel` | same detection mechanism; consumer is `asyncio.create_task(...)` then `task.cancel()` mid-stream | B-002 | bug_repro |
| `stream.B-002.consumer_break_sync` | same as `consumer_break` but driven via `execute_stream_sync`; producer leak surface is shared | B-002 | bug_repro |
| `stream.local.large_item` | yields 100 KB item locally; received intact (NOT B-024) | — | payload |
| `stream.sync.from_sync_context` | `execute_stream_sync` from sync iter | — | sync |
| `stream.timeout.hanging_gen` | gen never yields; timeout fires | — | timeout |
| `stream.contract.collected_set` | normal completion → request entry reaped within 30s. Detection: snapshot `set(core.requests.keys())` immediately before `execute_stream` and immediately after to capture the new request_id; assert it leaves the dict within 30s. | — | lifecycle |
| `stream.error.endpoint_not_generator` | calling execute_stream on a non-generator endpoint → clear error | — | error |

(All cases above are `category="basic"`.)

**Additional basic cases:**

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `stream.B-041.sync_chain_through_stream` | sync handler A in threadpool calls `execute_stream_sync("B", "gen")`; B's sync gen calls `execute_sync("A", "method")` → cycle. Today: chain wiped at stream boundary → undetected → either threadpool deadlock OR `RecursionError`. expected_status=fail, signature `marker="cycle_undetected"` set by the case body when (a) outer `wait_for(timeout=5)` fires, or (b) `RecursionError` is caught. When B-041 is fixed, expect `RequestException("Circular sync ...")` → no marker → `unexpected_pass` (review trigger). | B-041 | bug_repro |

**Edge cases:**

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `stream.edge.cancellation.between_yields` | cancel consumer while producer is between yields (sleep); request entry reaped, no producer leak | — | cancellation, edge |

### Phase 3 — TestNotifierSuite

**Plugins added:** `TestNotifierSuite`, `TestNotifierTarget`, `TestNotifierBadActor`.

**TestNotifierTarget config-driven topics:**
- `n_handle_greet` ← `test/greet`
- `n_handle_math` ← `test/math/add`
- `n_handle_wild_a` ← `test/wild/*`
- `n_handle_wild_b` ← `test/*/end`
- `n_handle_priv` ← `test/priv` with `accessible_by_other_plugins: false` (B-017)
- `n_handle_count` — code-driven sub registered in `on_enable` on `test/count`
- `n_handle_async_gen` ← `test/stream` (async generator)
- `n_handle_sync_gen` ← `test/sync_stream` (sync generator)

**TestNotifierTarget non-config endpoints (suite-driven):**
- `trigger_topic_hop()` (sync) — calls `self.request_topic_sync("topic/hop", ...)` for B-039 chain observation
- `topic_hop_observer()` (sync, subscribed to `topic/hop`) — reads `_sync_call_chain.chain` on entry, stores observed value on `self.observed_chain`
- `self_publish_handler()` ← `test/self` — increments `self.self_publish_count`
- `trigger_self_publish()` (async) — calls `self.notify("test/self")` from inside the same plugin; suite reads counter

**TestNotifierBadActor** is `enabled: false` by default. Suites load it on demand using the §5.4 idiom. Behaviors:
- `bad_handler_raises()`
- `bad_handler_cancels()` — raises `asyncio.CancelledError()` (B-036)
- `bad_handler_hangs(secs=3600)` (B-035)
- `bad_handler_returns(value)`
- Helper to register/unregister code-driven subs on demand

**Test cases (~36):**

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `notif.notify.no_subs` | returns 0 | — | basic |
| `notif.notify.exact_one_sub` | 1 exact match → fires, returns 1 | — | basic |
| `notif.notify.wildcard_match` | `test/wild/foo` → fires `test/wild/*` | — | wildcards |
| `notif.notify.multiple_subs` | 2 subs same topic → both fire, returns 2 | — | fan_out |
| `notif.notify.exact_and_wildcard` | both match → both fire | — | wildcards |
| `notif.notify.wildcard.empty_segment` | empty topic — pin current behavior | — | wildcards, contract |
| `notif.notify.wildcard.leading_slash` | `/test/x` vs `/test/*` — pin current | — | wildcards, contract |
| `notif.notify.wildcard.double_slash` | `a//b` — pin current | — | wildcards, contract |
| `notif.notify.wildcard.mid_segment` | `a*b` literal star — pin current | — | wildcards, contract |
| `notif.request_topic.basic` | returns first sub's value | — | basic |
| `notif.request_topic.no_sub` | RequestException | — | error |
| `notif.request_topic.priority_config_first` | config-driven before code-driven | — | priority |
| `notif.B-040.wildcard_tie_after_reload` | two wildcards both match topic; record initial winner; reload; expected_status=fail, marker="winner_flipped" | B-040 | bug_repro |
| `notif.request_topic_stream.basic` | streams 3 items | — | stream |
| `notif.request_topic_stream.sync_gen` | sync gen handler | — | stream, sync_target |
| `notif.notify.code_driven` | code-driven sub fires | — | code_driven |
| `notif.unsubscribe.code_driven` | unsub → no longer fires | — | unsubscribe |
| `notif.unsubscribe.idempotent` | unsubscribe twice → second returns False | — | unsubscribe |
| `notif.subscribe.duplicate_topic_same_plugin` | two subs same topic same plugin — both fire | — | edge |
| `notif.B-003.disable_clears_code_subs` | `_disable_plugin` then notify; expected_status=fail, marker="disabled_handler_fired" | B-003 | bug_repro |
| `notif.B-003.disable_clears_config_subs` | same for config-driven | B-003 | bug_repro |
| `notif.B-034.reload_window_drops_notify_code_driven` | start reload + notify in window; code-driven sub gone until new on_enable runs; Window B (large gap); expected_status=fail, marker="notify_dropped" | B-034 | bug_repro |
| `notif.B-034.reload_window_drops_notify_config_driven` | same but config-driven topic sub; Window A (small gap between unsubscribe_plugin and re-subscribe inside load_plugin_with_conf); expected_status=fail, marker="notify_dropped" | B-034 | bug_repro |
| `notif.B-022.subscribe_neither_handler_nor_access` | silent no-op | B-022 | bug_repro |
| `notif.contract.both_handler_and_access_handler_wins` | regression-lock: handler wins (current documented behavior); expected_status=pass | — | contract |
| `notif.error.sub_raises` | one sub raises → others still fire | — | error |
| `notif.B-036.cancellederror_propagates` | sub raises CancelledError; expected_status=fail, exception_type="CancelledError" | B-036 | bug_repro |
| `notif.B-035.notify_blocks_on_slow_sub` | sub hangs; wrap notify in wait_for(2s); expected_status=fail, marker="outer_wait_for_fired" | B-035 | bug_repro |
| `notif.B-023.notify_returns_int_not_raises` | notify returns int; expected_status=fail with marker if it raises | B-023 | bug_repro |
| `notif.B-017.priv_via_topic_from_other` | other plugin notifies private topic; expected_status=fail, marker="silently_dropped" | B-017 | bug_repro |
| `notif.access.priv_via_topic_from_self_pinned` | same plugin via Plugin.notify with own author_id → reaches handler | — | access, contract |
| `notif.B-017.priv_via_raw_core_notify` | raw `core.notify(topic, args)` (no author override; author='system' rewritten to hostname → `requester_id != plugin_uuid` AND `accessible_by_other_plugins=False` → handler not invoked); expected_status=fail, marker="silently_dropped" | B-017 | bug_repro |
| `notif.basic.local_count_correct` | count returned == actual local subs that fired (sanity for non-remote path; NOT B-019) | — | basic |
| `notif.sync.notify_sync` | from sync context | — | sync |
| `notif.sync.request_topic_sync` | from sync context | — | sync |
| `notif.sync.request_topic_stream_sync_no_remote` | host="any" no local sub → currently raises | B-014 | bug_repro |
| `notif.sync.request_topic_stream_sync_host_remote_routes_local` | host="remote" silently routes local; SKIP if no peer (Phase 5) | B-033 | bug_repro, requires_remote |
| `notif.B-039.sync_chain_via_topic_hop` | suite calls `core.execute_sync("TestNotifierTarget", "trigger_topic_hop")`; trigger calls `self.request_topic_sync("topic/hop", ...)` → topic_hop_observer fires → reads `_sync_call_chain.chain` on entry → stores in `target.observed_chain`; suite reads back via `execute("TestNotifierTarget", "get_observed_chain")` and asserts the chain is empty → `c.set_marker("chain_was_empty")`; expected_status=fail, signature marker="chain_was_empty" | B-039 | bug_repro |

(All cases above are `category="basic"`.)

**Additional basic cases:**

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `notif.contract.wildcard_does_not_cross_segments` | `"sensor/*"` MUST NOT match `"sensor/bath/temp"` (different segment count) | — | wildcards, contract |
| `notif.contract.find_all_exact_before_wildcard` | exact subs returned before wildcard subs in `find_all` order (lock current behavior) | — | priority, contract |
| `notif.contract.find_first_code_driven_registration_order` | two code-driven subs same topic; first registered wins | — | priority, contract |
| `notif.contract.self_publish_self_delivers` | plugin notifies a topic it has subscribed to; handler fires (no `plugin_uuid != publisher` filter today; lock that) | — | self_publish, contract |
| `notif.contract.notify_disabled_networking_no_remote_attempt` | `networking_enabled=False` → notify host="any" doesn't iterate `network.nodes` | — | networking, contract |
| `notif.plugin_api.subscribe_unsubscribe_via_plugin_helpers` | `Plugin.subscribe(...)` returns sub_id; `Plugin.unsubscribe(sub_id)` removes it; verify via notify | — | api, contract |
| `notif.plugin_api.notify_returns_int_through_plugin_wrapper` | `Plugin.notify(...)` returns the int from PluginCore.notify (decorator doesn't swallow) | — | api, contract |

**Edge cases:**

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `notif.edge.wildcard.unicode` | topic `"测试/x"` and `"café/*"` — match correctly under segment-level rules | — | wildcards, edge |
| `notif.edge.wildcard.literal_star_topic` | subscribe `"*"`; notify `"*"` → handler fires (single-segment wildcard matches `"*"`) | — | wildcards, edge |
| `notif.edge.topic.just_slash` | topic `"/"` — pin behavior | — | wildcards, edge |
| `notif.edge.subscribe.topic_with_control_chars` | subscribe topic with `\n` / `\t` — accepts (lock current) | — | edge |
| `notif.edge.subscribe.long_topic_pattern` | 1 KB pattern works | — | edge |
| `notif.edge.cancellation.notify_caller_cancel` | cancel `notify()` caller while `gather` runs; verify subs that already started complete; cancelled caller raises CancelledError | — | cancellation, edge |

### Phase 4 — TestLifecycleSuite

**Plugins added:** `TestLifecycleSuite`, `TestLifecycleVictim` (loaded twice as `TestLifecycleVictim` and `TestLifecycleVictim2`), `TestLifecycleBrokenVersion` (static fixture), `TestLifecycleSentinel` (static fixture).

**TestLifecycleVictim** behaviors via `configure({...})`:
- `on_load_raises` (bool)
- `on_enable_raises_after_setup` (bool) — opens "DB" (sets `self.db_open=True`) then raises (B-004)
- `on_enable_delay_secs` (int) — for concurrent enable race (B-008)
- `on_disable_raises` (bool) (B-010)
- `on_disable_hangs_secs` (int) (B-009) — must use `await asyncio.sleep(secs)` (NOT `time.sleep` — sync `time.sleep` inside an async coroutine freezes the event loop)
- Endpoints: `is_db_open()`, `enable_count()`, `disable_count()`, `victim_hang_endpoint(secs)` (for B-005), `inject_bad_request()` (writes a malformed object into `self._plugin_core.requests` for B-006)

**TestLifecycleBrokenVersion** static fixture: `plugin_config.yml` *without* a `version` field. Listed in `config.example.yml` BEFORE `TestLifecycleSentinel` so the B-007 KeyError-aborts-load-loop bug can be observed via Sentinel's absence in `core.plugins`.

**TestLifecycleSentinel** static fixture: trivial plugin that records `self.loaded=True`. Its presence/absence after startup is the B-007 assertion.

**Suite cleanup contract:** every case ends with a `finally` that re-installs the victim into a clean state (re-enables, resets configure flags, closes any opened "DB"). Suite-level `core.plugins` snapshot diff at end of each case (per §5.2).

**Test cases (~17):**

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `lifecycle.load.valid_config` | normal load → in `core.plugins` | — | basic |
| `lifecycle.B-007.missing_version_aborts_load_loop` | TestLifecycleBrokenVersion in config BEFORE Sentinel; `c.skip(...)` if order wrong; expected_status=fail, marker="sentinel_loaded" | B-007 | bug_repro |
| `lifecycle.load.malformed_endpoint` | endpoint missing access_name → error_config + plugin not loaded | — | basic |
| `lifecycle.enable.success` | enabled=True after on_enable | — | basic |
| `lifecycle.B-004.on_enable_raises_no_undo` | configure on_enable_raises_after_setup; assert `victim.is_db_open()==True` after `_enable_plugin` returns; expected_status=fail, marker="db_was_closed" | B-004 | bug_repro |
| `lifecycle.disable.success` | enabled=False after on_disable | — | basic |
| `lifecycle.disable.error.disabled_plugin_not_callable` | disable victim then call its endpoint → "Endpoint not found" (relocated from Phase 1) | — | error |
| `lifecycle.B-010.on_disable_raises` | enabled flag state, `core.plugins` state after on_disable raises during reload; expected_status=fail | B-010 | bug_repro |
| `lifecycle.B-009.disable_no_timeout` | configure on_disable_hangs_secs=120; outer wait_for(_reload_plugin, timeout=10); expected_status=fail, marker="outer_wait_for_fired". Recovery in `finally`: forcibly mark victim `enabled=False` then `pop_plugin` (skips disable branch). `hard_timeout_s=25`. Document plugin_lock starvation risk in suite README. | B-009 | bug_repro |
| `lifecycle.reload.preserves_enabled` | reload while enabled → still enabled after | — | reload |
| `lifecycle.B-016.reload_disabled_in_new_config` | edit yaml_config in-memory to set enabled=false on victim; reload; expected_status=fail, marker="silent_keyerror_swallowed" | B-016 | bug_repro |
| `lifecycle.pop_plugin.fails_pending` | task waiting on `victim_hang_endpoint`; pop_plugin; assert task raises RequestException with "unloaded while pending" within 5s | — | basic |
| `lifecycle.B-005.purge_skips_pending` | task waiting on `victim_hang_endpoint`; purge_plugins; expected_status=fail, marker="task_did_not_get_unloaded_error" | B-005 | bug_repro |
| `lifecycle.B-008.concurrent_enable_race` | TWO Victim plugins; runtime config-order assertion (Victim before Victim2 — `c.skip(...)` if mis-ordered); Victim2 has `on_enable_delay_secs=1`; Victim's on_enable calls `execute("TestLifecycleVictim2", "is_db_open")`. Today: bug present → "Endpoint not found" → expected; suite catches RequestException("Endpoint not found"). expected_status=fail, signature `exception_type="RequestException", message_regex="Endpoint .* not found"`. (When bug fixed: call succeeds, no exception → `unexpected_pass`.) | B-008 | bug_repro |
| `lifecycle.B-037.notify_during_pop` | notify on victim's topic; concurrently pop_plugin; expected_status=fail, marker="handler_ran_after_disable" | B-037 | bug_repro |
| `lifecycle.B-006.running_loop_guard` | **destructive=True**. Body: snapshot `_running_loop_task`; inject `core.requests["bad-test-id"] = object()`; sleep 13s (>1 cleanup tick + slack); assert `_running_loop_task.done()` and `_running_loop_task.exception() is not None` → `c.set_marker("running_loop_died")`. `finally`: pop the bad request; restart `core._running_loop_task = asyncio.create_task(core.running_loop())`. `hard_timeout_s=30`. expected_signature `marker="running_loop_died"`. | B-006 | bug_repro, terminal |

(All cases above are `category="basic"`.)

**Additional basic cases:**

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `lifecycle.B-005.purge_except_skips_pending` | task waiting on victim_hang_endpoint; `purge_plugins_except([keepers])`; same gap as B-005 in purge_plugins; expected_status=fail, marker="task_did_not_get_unloaded_error" | B-005 | bug_repro |
| `lifecycle.B-043.pop_plugin_failed_requests_eventually_reaped` | pop a plugin while a long-running task awaits one of its endpoints; cancel the caller before it observes the result; assert request entry reaped within 30s via `created_at` fallback | B-043 | bug_repro |
| `lifecycle.contract.disable_reverse_order_via_disable_plugin` | drive `_disable_plugin` against Victim then Victim2 in the order `close()` would use (reverse config order: Victim2 first, then Victim); track `disable_count` and timestamps on each; assert ordering matches reverse config order. Note: this tests `_disable_plugin` ordering logic, not the full `close()` path — full-shutdown reverse order is verified in Phase 5 via subprocess (subnode shut down with multiple plugins, log inspection). | — | shutdown, contract |
| `lifecycle.args.deep_merge_preserves_siblings` | base `{a: {x:1, y:2}}`, override `{a: {x:10}}` → merged `{a: {x:10, y:2}}` | — | args_override, contract |
| `lifecycle.args.replace_marker_clears` | base `{a: {x:1, y:2}}`, override `{a: {__replace__: True}}` → merged `{a: {}}` (sibling y dropped) | — | args_override, contract |
| `lifecycle.args.replace_marker_with_keys` | base `{a: {x:1, y:2}}`, override `{a: {__replace__: True, z:3}}` → merged `{a: {z:3}}` | — | args_override, contract |
| `lifecycle.args.list_fully_replaces` | base `{a: [1,2,3]}`, override `{a: [9]}` → merged `{a: [9]}` (no list extend/merge) | — | args_override, contract |
| `lifecycle.args.type_mismatch_warns_applies` | base `{a: 1}`, override `{a: "string"}` → warning logged AND override applied | — | args_override, contract |
| `lifecycle.args.base_none_not_mismatch` | base `{a: null}` (None), override `{a: {b: 1}}` → merged `{a: {b: 1}}`, no warning | — | args_override, contract |
| `lifecycle.args.main_invalid_override_warns_ignored` | main config `arguments: "not a dict"` → warning logged, override ignored, plugin loads with base args | — | args_override, contract |
| `lifecycle.args.plugin_invalid_hard_fails` | plugin_config.yml `arguments: "not a dict or null"` → plugin fails to load (error_config) | — | args_override, contract |
| `lifecycle.args.replace_marker_at_root` | root-level override `{__replace__: True, x: 1}` → wholesale replace; resulting args = `{x: 1}` (no `__replace__` key) | — | args_override, contract |
| `lifecycle.logger.set_level_persists_during_runtime` | `Plugin.set_logger_level("foo", console="WARNING")`; verify a "foo.bar" record at INFO is dropped | — | logger, contract |
| `lifecycle.logger.set_level_clears_on_disable` | set level then disable plugin; assert level cleared (record at INFO passes again) | — | logger, contract |
| `lifecycle.logger.set_level_clears_on_pop` | set level then pop_plugin; assert level cleared | — | logger, contract |
| `lifecycle.logger.longest_prefix_dot_boundary` | levels for `a` and `a.b`; logger `a.b.c` matches `a.b` (longest dot-boundary prefix) | — | logger, contract |
| `lifecycle.logger.mute_level` | set logger to MUTE → no records pass | — | logger, contract |
| `lifecycle.logger.set_level_survives_config_reload` | set runtime level; `async_load_config_yaml`; assert runtime level still active (overrides re-read config) | — | logger, contract |
| `lifecycle.logger.set_level_clears_on_purge` | set level then `purge_plugins`; assert level cleared | — | logger, contract |
| `lifecycle.config.async_reload_preserves_plugins` | call `async_load_config_yaml(path)` mid-execution; assert running plugins unaffected | — | config, contract |

**Edge cases:**

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `lifecycle.edge.race.enable_during_reload` | start `_reload_plugin(name)`; before it finishes, fire `_enable_plugin(name)`; assert no KeyError, no double-enable | — | race, edge |
| `lifecycle.edge.reload.fails_pending_request` | reload while task is awaiting an endpoint of the plugin being reloaded; assert task gets "unloaded while pending" or equivalent | — | reload, edge |
| `lifecycle.edge.churn.rapid_load_pop_load_same_name` | load → pop → load same name 5×; assert UUIDs all distinct, topic-registry cleanup correct each time | — | churn, edge |

### Phase 5 — TestRemoteSuite (subprocess model)

**Plugins added:** `TestRemoteSuite`, `TestRemoteTarget` (remote=True), `TestRemoteVictim` (remote=False), `TestRemoteSpoofer`.

**Why subprocess:** two PluginCore instances in one process collide on `LogUtil.create()` global state, share the asyncio loop, and bind ports in the same process. Subprocess matches how networking is meant to be used.

#### 6.5.1 `plugins_test/_remote_node/run_node.py` (sketch)

```python
import argparse, asyncio, json, os, signal, sys
from pathlib import Path

# Add repo root so `from PluginCore import ...` works
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from PluginCore import PluginCore  # noqa: E402

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--ready-file", required=True)
    args = ap.parse_args()

    pc = PluginCore(args.config)
    # Override port BEFORE wait_until_ready (NetworkManager is constructed there
    # and reads pc.networking_port — the INSTANCE ATTRIBUTE, not yaml).
    # Mutating yaml alone is silently no-op'd. Set both for safety.
    pc.networking_port = args.port
    pc.yaml_config.setdefault("networking", {})["port"] = args.port

    await pc.wait_until_ready()

    Path(args.ready_file).write_text(json.dumps({
        "hostname": pc.hostname,
        "port": args.port,
        "ip": "127.0.0.1",                 # localhost for in-machine testing
        "pid": os.getpid(),
    }))

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            # Windows fallback (Selector loop)
            signal.signal(sig, lambda s, f: shutdown.set())
    await shutdown.wait()
    await pc.graceful_shutdown()

if __name__ == "__main__":
    asyncio.run(main())
```

#### 6.5.2 `config.subnode.yml` (sketch)

```yaml
plugins:
  - name: TestRemoteTarget
    enabled: true
    path: ./plugins_test/TestRemoteTarget

  - name: TestRemoteVictim
    enabled: true
    path: ./plugins_test/TestRemoteVictim

  - name: TestRemoteSpoofer
    enabled: true
    path: ./plugins_test/TestRemoteSpoofer

general:
  hostname: "test-subnode"               # MUST differ from parent hostname
  plugin_package: plugins_test
  console_log_level: "WARNING"           # quiet sub-process

networking:
  enabled: true
  node_ips: ["127.0.0.1"]                # parent is on localhost
  port: 0                                # overridden via --port
  discover_nodes: true
  direct_discoverable: true
  auto_discoverable: true
```

The parent's main `config.yml` for Phase 5 testing must include:
```yaml
networking:
  enabled: true
  node_ips: ["127.0.0.1"]
  port: 2510
```

Subnode port chosen by suite at runtime (e.g. `2511`) and passed via `--port`. Suite reads `ready_file` to confirm subnode is up, then issues remote calls via `host="test-subnode"`.

#### 6.5.3 Subprocess lifecycle

Suite's `on_enable`:
1. Resolve subnode interpreter via `sys.executable`
2. Generate temp ready-file path
3. Spawn:
   ```python
   self._subproc = subprocess.Popen(
       [sys.executable, str(REPO_ROOT / "plugins_test/_remote_node/run_node.py"),
        "--config", "plugins_test/_remote_node/config.subnode.yml",
        "--port", str(self.port),
        "--ready-file", self._ready_file],
       cwd=str(REPO_ROOT),
   )
   ```
4. Poll for ready-file existence with `wait_for(timeout=15)`. If timeout, all Phase 5 cases skip with `skip_reason="subnode failed to come up within 15s"`.

Suite's `on_disable`:
1. `self._subproc.terminate()`
2. Wait up to 5s
3. `self._subproc.kill()` if still alive
4. Delete ready-file

#### 6.5.4 Shared secret coordination

If networking uses a shared secret or TLS material, the subnode must read the same files. Approach: subnode's config references the SAME secret/cert paths as the parent; both processes read independently. If parent generates secrets at startup into temp files, suite must pass those paths to subnode via env vars (`AIO_NETWORKING_SECRET_FILE`, etc.) — subnode config picks them up. Document this as a known coupling at Phase 5 implementation time.

#### 6.5.5 TestRemoteTarget endpoints (`remote: true` plugin)

- `r_open(value)` — accessible=True
- `r_remote_only(value)` — accessible=False, remote=True
- Plugin-level: `remote: true`
- `r_async_gen(n)` — for stream tests
- `r_async_gen_huge_item(size_mb=101)` — for B-024
- `r_async_gen_raises_after(n=2)` — for B-025
- `r_hang()` — for B-028
- `r_topic_open` ← `test/r/open` (config-driven, remote=True)

**TestRemoteVictim endpoints (`remote: false` plugin):**
- `r_local_only(value)` — accessible=True, plugin-level remote=False
- Code-driven sub on `test/r/code` registered in `on_enable` (B-001 target). Handler increments `self.bypass_count`; suite reads via `get_bypass_count` endpoint.
- `r_topic_local_only` ← `test/r/local` (config-driven, remote=False)

**TestRemoteSpoofer endpoints (peer-side only):**
- `spoof_notify(topic, args, author, author_id)` — uses `self._plugin_core.network.notify_remote(IP, topic, args, author, author_id)` directly to inject arbitrary author/author_id values. Suite calls into it via legitimate `execute(..., host="test-subnode")` to TRIGGER the spoof, which then originates from the peer back to the local node. Hard-guard: if `not self._plugin_core.networking_enabled or self._plugin_core.network is None`, skip cleanly.

#### 6.5.6 Test cases (~22)

All Phase 5 cases declare `hosts=["remote"]` (or specific subnode hostname). Generic remote-execute / remote-notify / remote-request_topic cases that mirror local behavior are NOT listed here — they live in Phase 1/3 with `hosts=["local","remote"]` and matrix-expand at runtime (§4.6).

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `remote.execute.remote_false_blocked` | plugin-level `remote=False` endpoint NOT reachable from peer | — | access |
| `remote.execute.access_false_blocked` | plugin-level remote=True but `accessible_by_other_plugins=False` from peer NOT reachable | — | access |
| `remote.notify.remote_false_blocked_for_config` | config-driven sub on `remote=False` plugin NOT fired by remote notify (B-001 boundary check) | — | access |
| `remote.B-001.code_driven_bypass` | code-driven sub on `remote=False` plugin IS fired by remote notify; readback via `get_bypass_count`; expected_status=fail, marker="bypass_succeeded" | B-001 | bug_repro, security |
| `remote.B-042.code_driven_stream_bypass` | code-driven async-gen sub on `remote=False` plugin IS iterated by remote `request_topic_stream`; readback via counter; expected_status=fail, marker="bypass_succeeded". Companion to B-001 for the streaming path. | B-042 | bug_repro, security |
| `remote.B-018.spoof_system_string` | TestRemoteSpoofer issues notify_remote with author="system"; expected_status=fail, marker="bypass_succeeded" | B-018 | bug_repro, security |
| `remote.B-018.spoof_known_uuid` | same with `author_id=<known local uuid>`; suite passes uuid via args | B-018 | bug_repro, security |
| `remote.B-019.notify_count_per_node_not_per_sub` | peer subscribes 3 handlers to `test/r/multi`; from local, `count = await core.notify("test/r/multi", host="remote")`; assert `count == 3`; today gives 1 per remote node; expected_status=fail, marker="count_was_node_not_subs" | B-019 | bug_repro |
| `remote.B-021.first_sub_not_remote_eligible` | local has two subs: first remote=False, second remote=True; remote request → assert RequestException; expected_status=fail, exception_type=RequestException, message_regex="No handler" | B-021 | bug_repro |
| `remote.B-024.huge_item` | server yields 101 MB item; expected_status=fail, marker="stream_aborted" | B-024 | bug_repro, slow |
| `remote.B-025.partial_then_failover` | A yields 5 then raises, B yields 3; assert N>5 items received; expected_status=fail, marker="duplicate_items_silently_appended" | B-025 | bug_repro |
| `remote.B-011.stream_error_sentinel_via_item_end` | server emits `("__STREAM_ERROR__", msg)` then `MSG_STREAM_ITEM_END`; client treats sentinel as data (path 1421-1433) | B-011, B-012 | bug_repro |
| `remote.B-012.stream_error_sentinel_via_end_stream` | server emits `("__STREAM_ERROR__", msg)` then `MSG_END_STREAM` directly (path 1465-1477 — different unpickle branch); client treats sentinel as data | B-012 | bug_repro |
| `remote.B-028.no_client_timeout` | remote hangs; outer wait_for(2s); expected_status=fail, marker="outer_wait_for_fired" | B-028 | bug_repro |
| `remote.B-029.code_driven_timeout_ignored` | code-driven topic handler ignores caller's timeout; expected_status=fail, marker="elapsed_exceeded_timeout" | B-029 | bug_repro |
| `remote.B-030.unpicklable_args` | local fires, remote silently misses; expected_status=fail, marker="state_diverged" | B-030 | bug_repro |
| `remote.B-027.notify_return_count_misleading` | remote returns 0 from transport fail; assert PluginCore counted as +1; expected_status=fail, marker="counted_as_success" | B-027 | bug_repro |
| `remote.B-032.head_of_line_blocking` | slow sub blocks fast call on same connection; expected_status=fail, marker="fast_call_blocked" | B-032 | bug_repro, slow |
| `remote.B-033.request_topic_stream_sync_host_remote` | host="remote" silently routes local | B-033 | bug_repro |
| `remote.B-020.notify_sync_blocks_on_remote` | notify_sync from sync context with slow remote sub; assert calling thread blocked; expected_status=fail, marker="thread_blocked" | B-020 | bug_repro |
| `remote.find_endpoints_by_tag` | tag discovery cross-node returns peer endpoints | — | discovery, basic |

(All cases above are `category="basic"`.)

**Edge cases:**

| ID | Case | bug_ids | tags |
|---|---|---|---|
| `remote.edge.tag.no_matches_returns_empty_list` | tag absent everywhere → empty list, not None | — | discovery, edge |
| `remote.edge.tag.mixed_local_remote` | same tag on both nodes → both endpoints returned in single response | — | discovery, edge |
| `remote.edge.pool.exhaustion_under_concurrent_hangs` | spawn N concurrent `request_topic("hang")`; assert pool slot recycled after caller cancellation | — | networking, edge, slow |
| `remote.edge.discovery.simultaneous_startup_race` | both nodes start within 100ms of each other; both eventually discover each other | — | networking, edge |

---

## 7. Config integration

### 7.1 `config.example.yml` after Phase 5

```yaml
plugins:
  # ... existing plugins (PostgreSQL, etc.) ...

  - name: TestRunner
    enabled: true                         # default ON so REPL/CLI works
    path: ./plugins_test/TestRunner

  - name: TestExecuteSuite
    enabled: false
  - name: TestExecuteTarget
    enabled: false
  - name: TestExecuteTarget2              # multi-instance
    enabled: false
    path: ./plugins_test/TestExecuteTarget

  - name: TestStreamSuite
    enabled: false
  - name: TestStreamTarget
    enabled: false

  - name: TestNotifierSuite
    enabled: false
  - name: TestNotifierTarget
    enabled: false
  - name: TestNotifierBadActor
    enabled: false                        # suite enables programmatically when needed

  - name: TestLifecycleSuite
    enabled: false
  - name: TestLifecycleVictim
    enabled: false
  - name: TestLifecycleVictim2            # for B-008 concurrent enable race
    enabled: false
    path: ./plugins_test/TestLifecycleVictim
  - name: TestLifecycleBrokenVersion      # ORDER MATTERS for B-007
    enabled: false
  - name: TestLifecycleSentinel           # MUST be after BrokenVersion
    enabled: false

  - name: TestRemoteSuite
    enabled: false
  - name: TestRemoteTarget
    enabled: false
  - name: TestRemoteVictim
    enabled: false
  - name: TestRemoteSpoofer
    enabled: false                        # loaded only on the peer subprocess
```

### 7.2 Removal (Phase 6)

After Phase 5 ships and verifies, delete:
- `plugins_test/InteropCaller/`
- `plugins_test/InteropTarget/`
- `plugins_test/NotifierPublisher/`
- `plugins_test/NotifierSubscriber/`

And remove their entries from `config.example.yml`.

---

## 8. Per-phase delivery checklist

1. New plugin folders + files created with valid `plugin_config.yml`.
2. Version bumped on all touched plugins.
3. New entries added to `config.example.yml` (`enabled: false` except TestRunner).
4. `bugtracker.md` updated — for each case tagged with a bug ID, append a "Repro test:" line under the bug entry pointing at the case ID.
5. Manually run the suite: enable suite + targets in `config.yml`, hit `core.execute("TestRunner", "run_all", args={"suites": ["...Suite"]})`. Inspect output dict and JSON dump.
6. Verify pass/fail counts match what the bugtracker predicts (every `expected_status=fail` case should report `status=pass` today; if a case reports `status=fail`, the signature didn't match — investigate).
7. Optional: snapshot output JSON to `_private/test_outputs/<phase_N>_baseline.json`.
8. Report any `status=unexpected_pass` — this means a bug may have been silently fixed and the case marker should be flipped.

---

## 9. Risks

- **Concurrency-test bounds.** Hard timeouts prevent runner death. Bound choices documented per case.
- **Multi-instance plugin loading** — relied on by Phase 1. The §11.2 smoke test gates this; if it fails, all `multi_instance` cases skip cleanly.
- **Subprocess on Windows.** §6.5.1 sketch falls back to `signal.signal` for Windows; `terminate()` then `kill()` after 5s.
- **B-007 fixture order.** Case re-reads `core.yaml_config` and `c.skip(...)` if BrokenVersion not before Sentinel.
- **`expected_status="fail"` flip review.** `runner.review_required` is loud; CI script can fail the build if non-empty.
- **B-009 plugin_lock starvation.** Recovery requires forcibly marking victim disabled then `pop_plugin`; documented in suite README.

---

## 10. What this plan does NOT cover

- **Performance benchmarks** — measure throughput, latency. Different concern.
- **Load testing** — sustained pressure, leak detection at scale.
- **Fuzz testing** — random input generation against parsers.
- **CLI Dashboard / TUI** — Textual-based plugin tab system. UI testing requires snapshot pixels and Textual's pilot framework; out of scope for the runtime test framework.
- **TLS certificate generation / rotation** — requires cert-authority machinery.
- **Shared-secret rotation across running nodes** — Phase 5 scaffold uses static secrets per process; rotation testing requires running infra.
- **`Plugin._to_dict`** — `raise NotImplementedError` per code; contract is explicitly broken.
- **Config file edit utilities** (`read_config_file` / `save_config_file` / `add_to_config`) — file IO surface, not framework runtime. Plugin authors test their own usages.
- **Pre-`start()` framework state** — sync notifier wrappers raise `TypeError` if loop not initialized (B-038); testing that state is outside the framework's "running core" contract.
- **External CI harness / pytest wrapper** — `dump_path` + a small CI script can read the JSON; bridge-to-pytest is separate work.
- **Migration guide for users** — documented in Phase 6 commit message; not part of the test framework itself.

For each of these, the documentation surface (README / CLAUDE.md / commit messages) remains authoritative; the test framework focuses exclusively on runtime behavior of a started PluginCore.

---

## 11. Phase 1 implementation order

1. Create `plugins_test/_test_helpers.py` with `CaseRecorder`, `_CaseContext`, `RecorderError`, `assert_hang`, signature-matching, hard-timeout enforcement.
2. Create `plugins_test/TestRunner/{plugin.py, plugin_config.yml}` with `run_all`, `run_suite` endpoints implementing aggregation, dump, filtering by `case_ids` / `bug_ids`.
3. Create `plugins_test/TestExecuteTarget/{plugin.py, plugin_config.yml}` with all 15 fixture endpoints listed in §6 Phase 1.
4. Create `plugins_test/TestExecuteSuite/{plugin.py, plugin_config.yml}` with 28 cases listed in §6 Phase 1.
5. Update `config.example.yml` with new entries (TestRunner=true, others=false).
6. Smoke test: enable Runner + ExecuteSuite + ExecuteTarget + ExecuteTarget2 in `config.yml`, run `core.execute("TestRunner", "run_all")`, inspect dict.
7. Snapshot baseline JSON to `_private/test_outputs/phase_1_baseline.json`.
8. PAUSE for user review.

### 11.1 Helper outline

```python
# plugins_test/_test_helpers.py
import asyncio, time, traceback, re
from typing import Any, Optional, Dict, List, Tuple

class RecorderError(Exception): pass
class _SkipSignal(BaseException): pass

class _CaseContext:
    def __init__(self, recorder, id, *, tags=(), bug_ids=(),
                 category="basic", hosts=("local",),
                 expected_status="pass", expected_signature=None,
                 hard_timeout_s=30.0, destructive=False,
                 host="local"):
        self.recorder = recorder
        self.id = id
        self.tags = list(tags)
        self.bug_ids = list(bug_ids)
        self.category = category
        self.hosts = list(hosts)
        self.host = host                              # set per-iteration when matrix-expanding
        self.expected_status = expected_status
        self.expected_signature = expected_signature
        self.hard_timeout_s = hard_timeout_s
        self.destructive = destructive
        self.expected_value = None
        self.actual_value = None
        self.exception_type = None
        self.exception_message = None
        self.marker = None
        self.skip_reason = None
        self.expected_drift = None
        self._exception_expectation: Optional[Tuple[type, Optional[str]]] = None
        self._t0 = None
        if expected_status == "fail" and expected_signature is None:
            raise RecorderError(
                f"case {id}: expected_status='fail' requires expected_signature"
            )

    def expect(self, actual, expected):
        self.actual_value = actual
        self.expected_value = expected
        if actual != expected:
            raise AssertionError(f"expected {expected!r}, got {actual!r}")

    def expect_exception(self, exc_type, *, match=None):
        self._exception_expectation = (exc_type, match)

    async def assert_hang(self, awaitable, *, timeout_s, marker):
        try:
            await asyncio.wait_for(awaitable, timeout=timeout_s)
        except asyncio.TimeoutError:
            self.marker = marker
            raise AssertionError(f"hang_guard fired: {marker}")

    def set_marker(self, name: str):
        self.marker = name

    def skip(self, reason: str):
        self.skip_reason = reason
        raise _SkipSignal()

    def set_expected_drift(self, *, added: List[str] = (), removed: List[str] = ()):
        self.expected_drift = {"added": list(added), "removed": list(removed)}

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # ... compute status per §4.2 / §4.4 rules, append to recorder
        # always swallow expected exceptions (don't re-raise), let runner continue
        return True

class CaseRecorder:
    def __init__(self, suite_name, version, plugin_core):
        self.suite_name = suite_name
        self.version = version
        self.plugin_core = plugin_core
        self.cases: List[dict] = []
        self.snapshot: set = set(plugin_core.plugins.keys())

    def case(self, id, **kwargs):
        return _CaseContext(self, id, **kwargs)

    def to_dict(self) -> dict:
        ...
```

### 11.2 Multi-instance smoke test

The first case in TestExecuteSuite is `exec.smoke.multi_instance_distinct`:
- `await core.execute("TestExecuteTarget", "ea_add", (1, 2), plugin_uuid=t1_uuid)` returns 3.
- `await core.execute("TestExecuteTarget2", "ea_add", (10, 20), plugin_uuid=t2_uuid)` returns 30.
- Assert `t1_uuid != t2_uuid`.

If it fails, all `multi_instance`-tagged cases skip with `skip_reason="multi-instance smoke failed; loader does not isolate instances"`. Phase 1 cannot be considered done until either the smoke passes OR multi-instance loading is intentionally dropped.

### 11.3 Bugtracker note about deferred test cases

These cases need design work outside the framework before they can be repro'd cleanly. Listed in `bugtracker.md` with a "Test deferred" marker:
- `exec.sync.from_async_*` — needs runtime guard inside `execute_sync` OR deadlock-with-outer-wait_for assertion that's ambiguous (loop frozen, no clear failure mode).
- **B-026** (rare race after partial chunk write voids `None` result): requires writer mocking the framework doesn't support.
- **B-038** (sync wrappers raise `TypeError` pre-`start()`): requires testing PluginCore before its own `start()` runs — outside the framework's "running core" contract.

---

## 12. Conventions for adding new cases later

- **One bug, one repro case.** When a new bug lands in `bugtracker.md`, add a case in the appropriate suite tagged with the bug ID, `expected_status="fail"`, with a precise `expected_signature`.
- **When a bug is fixed**, the case turns `unexpected_pass`. Reviewer flips `expected_status` from `fail` to `pass` (and removes signature) in the same commit that lands the fix.
- **Never silently delete** a case for a bug that's been fixed. Promote it to a regression-lock (`expected_status="pass"`) so the fix can't be undone unnoticed.
- **Tags vs bug_ids.** `tags` is categorical (`execute`, `args_contract`, `wildcards`); `bug_ids` is `["B-NNN", ...]`. CI dashboards filter on each separately.
- **Slow cases** must be tagged `slow` so `skip_slow=True` works in CI.
- **Destructive cases** must set `destructive=True` and document their post-condition recovery in a comment immediately above the case body.

---

End.
