import os
import re
import socket
import sys

os.environ.setdefault("PYTHONUTF8", "1")  # UTF-8 mode: all open() default to utf-8
if hasattr(sys.stdout, "reconfigure"):  # Reconfigure console streams to UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import contextlib
import functools
import importlib
import inspect
import asyncio
import time
import threading
import traceback
from collections import deque
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from typing import Any, Optional, Callable, Union, Dict, List, Set, Tuple
import yaml

# Tracks the sync call chain on each threadpool worker thread.
# Used by execute_sync / _call_endpoint to detect circular sync calls
# that would deadlock the ThreadPoolExecutor.
_sync_call_chain = threading.local()

from .exceptions import (
    NetworkRequestException,
    NoLocalSubException,
    RequestException,
)
from .networking_classes import Node, RemotePlugin
from .utils import LogUtil, Request, Plugin, ConfigUtil, GeneratorRequest, Event
from .decorators import (
    log_errors,
    handle_errors,
    async_log_errors,
    async_handle_errors,
    async_gen_log_errors,
    async_gen_handle_errors,
    gen_log_errors,
    gen_handle_errors,
)
from .networking import NetworkManager
from .notifier import TopicRegistry, Subscription, SyncDispatcher
from .plugin_state import State, Phase, ErrorRecord, PluginState

# Reserved identifier names — disallowed as plugin names AND endpoint
# access_names because they are framework-reserved keywords used in
# config/system contexts. Future-proof: extend as new framework-reserved
# names are introduced.
# Stage M (B-051): "any"/"remote" reserved by _normalize_hosts as host
# keywords; "local" reserved as the loopback hostname keyword. Reusing
# these as plugin names creates ambiguity in `authors:` and
# `blocked_authors:` subscription filter lists (which delegate validation
# to _normalize_hosts and would silently reject the literal name).
_RESERVED_IDENTIFIER_NAMES = frozenset({"system", "general", "any", "remote", "local"})

# Default plugin-readiness gate timeout (seconds). Used by
# _wait_for_plugin_ready before dispatching to a plugin endpoint;
# raises asyncio.TimeoutError on expiry. Per spec must not be reduced
# below 30 in normal operation. Configurable via
# general.plugin_ready_timeout in config.yml; tests may override
# self.plugin_ready_timeout directly.
DEFAULT_PLUGIN_READY_TIMEOUT: float = 60.0

# Default plugin-disable timeout (seconds). Wraps user on_disable in
# asyncio.wait_for in disable_plugin / _pop_plugin_under_lock so a
# misbehaving on_disable can't hang pop_plugin / _reload_plugin /
# purge_plugins indefinitely. close() already had its own 30s; this
# brings runtime hot-reload paths to parity. Configurable via
# general.plugin_disable_timeout in config.yml; tests may override
# self.plugin_disable_timeout directly.
DEFAULT_PLUGIN_DISABLE_TIMEOUT: float = 30.0


def _validate_identifier_name(name, *, context: str) -> None:
    """Validate that ``name`` is a Python-identifier-style string and not in
    the reserved blacklist. Used for plugin names from config.yml and for
    endpoint access_names (= dict keys in plugin_config.yml endpoints:).

    Raises ValueError with a message that begins with ``context`` (e.g.
    ``"plugin name"`` or ``"endpoint access_name"``) so the caller can tag
    the error site without reformatting.
    """
    if not isinstance(name, str):
        raise ValueError(
            f"{context} {name!r} invalid: must be a string, got {type(name).__name__}"
        )
    if not name.isidentifier():
        raise ValueError(
            f"{context} {name!r} invalid: must be a valid Python identifier "
            f"(letters, digits, underscores; cannot start with a digit)"
        )
    if name in _RESERVED_IDENTIFIER_NAMES:
        raise ValueError(
            f"{context} {name!r} invalid: reserved name "
            f"(reserved: {sorted(_RESERVED_IDENTIFIER_NAMES)})"
        )


# Sections of plugin_config.yml that get DEEP-MERGED by apply_overrides.
# Each section is a top-level mapping; per-key entries are merged via
# _deep_merge_args. PR3 Stage B adds "events" and "subscriptions".
_OVERRIDE_SECTIONS = ("arguments", "endpoints", "events", "subscriptions")

# Sections that enforce STRICT unknown-subkey handling: an override naming
# a subkey not present in the base plugin_config is a fail-load ERROR.
# Other sections fall back to lenient (additive) deep-merge per Q14.
# Per Q14 events/subscriptions are LENIENT — overrides may add new keys.
_STRICT_OVERRIDE_SECTIONS = frozenset({"endpoints"})

# Plugin-level scalar/list fields (top-level fields of plugin_config.yml)
# that an `overrides:` block may VALUE-REPLACE. PR3 Stage B adds "prefix"
# and "verbose_notifier".
_PLUGIN_LEVEL_OVERRIDE_FIELDS = (
    "description",
    "remote",
    "version",
    "prefix",
    "verbose_notifier",
)

# Reserved topic_vars / load-time-templating names.
_RESERVED_TEMPLATE_VARS = frozenset(
    {"prefix", "plugin_name", "hostname", "plugin_uuid"}
)

# {var}-style placeholder regex. Matches {name} where name is identifier-style.
_TEMPLATE_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Networking-config fields whose change forces a full NetworkManager
# rebuild (not in-place attribute update). Per Commit 2b cycle 3
# settled design + framework_changes_plan.md Session 1 [A1]. Field
# locations:
#   - networking.peers            (list of peer entry dicts)
#   - networking.enabled          (bool)
#   - networking.port             (int)
#   - networking.hostname         (str — read by NetworkManager.__init__:175)
#   - general.hostname            (str — read by apply_configvalues for plx.hostname)
#   - networking.keys_dir         (str — read by NetworkManager.__init__:176)
# (``hostname`` covers BOTH source paths — see _networking_config_changed.)
# All OTHER networking fields (heartbeat_interval, lookup_interval,
# liveness_timeout, pool_size, discover_nodes, direct_discoverable,
# auto_discoverable, secret, cert_file, key_file) trigger
# _update_networking_in_place — no rebuild needed.
_REBUILD_FIELDS = frozenset({"peers", "enabled", "port", "hostname", "keys_dir"})


# B-073: Internal event bus recursion guard. Module-level ContextVar
# (NOT instance attr) so the per-task counter is shared across emit
# calls in the same task while remaining isolated between tasks via
# Python's contextvars task-inheritance. Increment with
# ``token = _EMIT_DEPTH.set(...)``; restore with ``_EMIT_DEPTH.reset(token)``
# — naive ``set(get() - 1)`` corrupts inherited parent-task state under
# nested ``asyncio.create_task`` fan-out. ``_MAX_EMIT_DEPTH = 5`` aborts
# pathological recursive observer chains.
_EMIT_DEPTH: ContextVar[int] = ContextVar("_aio_emit_depth", default=0)
_MAX_EMIT_DEPTH: int = 5


def _resolve_load_time_template(
    template: str,
    *,
    prefix: str,
    plugin_name: str,
    hostname: str,
    plugin_uuid: str,
) -> str:
    """Resolve the four reserved placeholders in a topic template at config-load
    time per PR3 LOCKED J. UNKNOWN ``{var}`` placeholders are LEFT INTACT for
    runtime resolution via topic_vars (PR3 LOCKED L).

    NOT a generic ``str.format()`` — that would error on unresolved
    ``{var}`` placeholders that should stay templated.
    """
    if not isinstance(template, str) or "{" not in template:
        return template

    substitutions = {
        "prefix": prefix,
        "plugin_name": plugin_name,
        "hostname": hostname,
        "plugin_uuid": plugin_uuid,
    }

    def _repl(match: re.Match) -> str:
        name = match.group(1)
        if name in substitutions:
            return substitutions[name]
        return match.group(0)  # leave untouched for runtime templating

    return _TEMPLATE_VAR_RE.sub(_repl, template)


def _validate_topic_static(
    topic: str,
    *,
    context: str,
    allow_wildcards: bool,
) -> str:
    """Validate a topic string per PR3 LOCKED L + Q15/Q16/C20.

    Returns the normalized topic (leading/trailing slashes stripped).

    Validation rules applied here:
      * Must be non-empty after stripping (Q15).
      * No empty middle segments (e.g. "a//b") — C20 rejection.
      * If allow_wildcards is False, ``*`` characters anywhere are
        rejected (events.topic field — wildcards are subscriber-side
        only, LOCKED L #1).
      * If allow_wildcards is True, the only allowed ``*`` form is a
        FULL-segment wildcard (e.g. "ai/*"). Embedded ``*`` mid-segment
        like "sensor/abc*" is rejected (C20 / LOCKED L #2a).

    Raises ValueError with ``context`` prefix on violation.
    """
    if not isinstance(topic, str):
        raise ValueError(
            f"{context}: topic must be a string; got {type(topic).__name__}"
        )

    stripped = topic.strip("/")
    if not stripped or not stripped.strip():
        raise ValueError(f"{context}: topic must not be empty (Q15)")

    # B-073 Step 9: reject topics starting with the framework-internal
    # prefix ``_``. ``_core/...`` is reserved for the internal event bus
    # (Plexus._internal_emit, exempt from this validator). Plugin
    # authors must use a non-underscore-prefixed namespace.
    if stripped.startswith("_"):
        raise ValueError(
            f"{context}: topic {topic!r} starts with reserved framework "
            f"prefix '_' — '_core/' is framework-internal (B-073)"
        )

    segments = stripped.split("/")
    for seg in segments:
        if not seg:
            raise ValueError(
                f"{context}: empty middle segment in topic {topic!r} (C20)"
            )
        if "*" in seg:
            if not allow_wildcards:
                raise ValueError(
                    f"{context}: wildcards not allowed in topic {topic!r} "
                    f"(LOCKED L #1 — wildcards are subscriber-side only)"
                )
            if seg != "*":
                raise ValueError(
                    f"{context}: embedded '*' mid-segment in topic {topic!r} "
                    f"(C20 — '*' must be a complete segment)"
                )

    # NOTE: subscriptions.topic also forbids {var} runtime templating;
    # that check lives in _validate_subscription_topic (different fn).
    # events.topic *allows* {var} runtime placeholders. So this fn
    # only validates: non-empty + (events-only) no wildcards + no
    # embedded * mid-segment + no empty middle segments.

    return stripped


def _validate_subscription_topic(topic: str, *, context: str) -> str:
    """Subscription topic validator. Same shape as
    _validate_topic_static(allow_wildcards=True) PLUS subscription-only
    rule LOCKED L #2: ``{var}`` runtime templating syntax is REJECTED in
    subscriptions.topic (subscribers use ``*`` wildcards instead).
    """
    if not isinstance(topic, str):
        raise ValueError(
            f"{context}: topic must be a string; got {type(topic).__name__}"
        )
    if _TEMPLATE_VAR_RE.search(topic) is not None:
        raise ValueError(
            f"{context}: '{{var}}' templating syntax not allowed in "
            f"subscription topic {topic!r} (LOCKED L #2 — subscribers use "
            f"'*' wildcards, not {{var}} placeholders)"
        )
    return _validate_topic_static(topic, context=context, allow_wildcards=True)


def _deep_merge_args(
    base: dict,
    override: dict,
    plugin_name: str,
    logger,
    counters: dict,
    _path: str = "",
) -> dict:
    """Deep-merge override into a copy of base. Lists fully replaced.
    Logs each change at DEBUG (key path only — never values).
    `counters` is mutated with {'added','replaced','type_mismatched'}.

    Single-level shallow copy at each recursion. Keys present only in `base`
    keep their original reference. Plugins must not mutate `self.arguments`
    in place — consistent with the existing contract.

    A dict containing `__replace__: true` is treated as a wholesale-replace
    directive: the rest of that dict (with the marker stripped) becomes the
    value at this position, bypassing deep merge. Use it to clear a subtree
    (`{__replace__: true}` -> `{}`) or replace it (`{__replace__: true, k: v}` -> `{k: v}`).
    """
    if override.get("__replace__") is True:
        replacement = {k: v for k, v in override.items() if k != "__replace__"}
        counters["replaced"] += 1
        logger.debug(
            f"Plugin '{plugin_name}': arg subtree replaced '{_path or '<root>'}'"
        )
        return replacement

    out = dict(base)
    for key, ov in override.items():
        path = f"{_path}.{key}" if _path else key
        if key not in out:
            out[key] = ov
            counters["added"] += 1
            logger.debug(f"Plugin '{plugin_name}': arg added '{path}'")
        elif isinstance(out[key], dict) and isinstance(ov, dict):
            out[key] = _deep_merge_args(
                out[key], ov, plugin_name, logger, counters, path
            )
        elif type(out[key]) == type(ov) and out[key] == ov:
            # No-op: same type AND same value. Type guard prevents `True == 1`
            # (bool vs int) from being treated as no-op — that pair must reach
            # the type-mismatch branch below.
            pass
        else:
            # Base-was-None is NOT a mismatch — overriding a previously-null
            # key is normal. Everything else with a type change warns.
            if out[key] is not None and type(out[key]) != type(ov):
                counters["type_mismatched"] += 1
                logger.warning(
                    f"Plugin '{plugin_name}': arg '{path}' type mismatch "
                    f"({type(out[key]).__name__} -> "
                    f"{type(ov).__name__ if ov is not None else 'NoneType'}); override applied"
                )
            else:
                counters["replaced"] += 1
                logger.debug(f"Plugin '{plugin_name}': arg replaced '{path}'")
            out[key] = ov
    return out


def apply_overrides(
    plugin_config: dict,
    overrides_block: Optional[dict],
    plugin_name: str,
    logger,
) -> dict:
    """Apply a main-config `overrides:` block to a copy of plugin_config.

    Walks `overrides_block`'s top-level keys:

      * Known SECTION (``arguments``, ``endpoints``) — deep-merged against
        plugin_config[section] via ``_deep_merge_args``. For STRICT sections
        (currently ``endpoints``), unknown subkeys (entries not present in
        the base plugin_config[section]) are a fail-load ERROR per Q2.
        For other sections, unknown subkeys are added per existing
        ``_deep_merge_args`` behavior (lenient, Q14).
      * Known PLUGIN-LEVEL FIELD (``description``, ``remote``, ``version``)
        — value-replaces plugin_config[field] outright.
      * Anything else at the top level — WARN and ignore (Q22).

    Returns a new dict (does not mutate ``plugin_config``). On strict-section
    error, raises ValueError so the caller can fail-load the plugin.

    Existing behavior preserved across the generalization:
      - ``__replace__: true`` directive (handled inside _deep_merge_args)
      - type-mismatch warning + counter
      - same DEBUG/INFO log shape as the previous narrow `arguments`
        override path (logged here at the section level so the user still
        sees the per-plugin "applied N override(s)" summary)
    """
    if overrides_block is None:
        return dict(plugin_config)
    if not isinstance(overrides_block, dict):
        # Caller is expected to type-check `overrides:` itself and convert
        # to None on warning. Defensive guard for direct callers (tests).
        raise ValueError(
            f"apply_overrides: overrides must be a mapping, "
            f"got {type(overrides_block).__name__}"
        )

    merged = dict(plugin_config)
    if not overrides_block:
        # Empty `overrides: {}` block — no-op shallow copy.
        return merged

    counters = {"added": 0, "replaced": 0, "type_mismatched": 0}

    for key, ov in overrides_block.items():
        if key in _OVERRIDE_SECTIONS:
            base_section = merged.get(key)
            # Sections must be mappings (or None / absent). A list-valued
            # override targeting a section is a hard error — covered for
            # the `endpoints:` case explicitly to surface the migration
            # mistake, applies generally to all sections.
            if not isinstance(ov, dict):
                raise ValueError(
                    f"override section '{key}' must be a mapping; "
                    f"got {type(ov).__name__}"
                )
            base_dict = base_section if isinstance(base_section, dict) else {}

            if key in _STRICT_OVERRIDE_SECTIONS:
                # Strict: every override subkey must exist in the base.
                # Unknown subkey → fail-load (Q2).
                unknown = [sk for sk in ov.keys() if sk not in base_dict]
                if unknown:
                    raise ValueError(
                        f"override section '{key}' references unknown "
                        f"entries {unknown!r}; known entries are "
                        f"{sorted(base_dict.keys())!r}"
                    )

            merged[key] = _deep_merge_args(
                base_dict, ov, plugin_name, logger, counters, _path=key
            )
        elif key in _PLUGIN_LEVEL_OVERRIDE_FIELDS:
            base_val = merged.get(key)
            if base_val is not None and type(base_val) != type(ov):
                # Mirrors _deep_merge_args type-mismatch warning behavior
                # for plugin-level fields. Override still applied (lenient).
                counters["type_mismatched"] += 1
                logger.warning(
                    f"Plugin '{plugin_name}': override field '{key}' type "
                    f"mismatch ({type(base_val).__name__} -> "
                    f"{type(ov).__name__ if ov is not None else 'NoneType'}); "
                    f"override applied"
                )
            else:
                if key in merged:
                    counters["replaced"] += 1
                else:
                    counters["added"] += 1
            merged[key] = ov
        else:
            # Unknown top-level override key — Q22: warn + ignore.
            logger.warning(
                f"Plugin '{plugin_name}': unknown top-level override key "
                f"'{key}'; ignored "
                f"(known sections: {list(_OVERRIDE_SECTIONS)}, "
                f"known plugin-level fields: {list(_PLUGIN_LEVEL_OVERRIDE_FIELDS)})"
            )

    total = counters["added"] + counters["replaced"] + counters["type_mismatched"]
    if total:
        logger.info(
            f"Plugin '{plugin_name}': applied {total} override(s) "
            f"({counters['added']} added, "
            f"{counters['replaced']} replaced, "
            f"{counters['type_mismatched']} type-mismatched)"
        )

    return merged


def _normalize_hosts(
    value: Any,
    *,
    param_name: str = "hosts",
    default: Optional[Union[str, List[str]]],
) -> Optional[Union[str, List[str]]]:
    """Normalize a hosts/blocked_hosts value into canonical form.

    Returns canonical value (str, list, or None). Raises ValueError on
    structural errors. Caller is responsible for passing normalized values
    to find_endpoint and to _warn_redundant_host_combos.
    """
    if value is None:
        return default

    if isinstance(value, str):
        if not value.strip():
            raise ValueError(f"{param_name}: empty string not allowed")
        return value

    if isinstance(value, list):
        if not value:
            raise ValueError(f"{param_name}: empty list not allowed")
        for item in value:
            if not isinstance(item, str):
                raise ValueError(
                    f"{param_name}: list entries must be str, "
                    f"got {type(item).__name__}"
                )
            if not item.strip():
                raise ValueError(f"{param_name}: empty string in list not allowed")

        # Dedup first (preserves first-occurrence order).
        seen = set()
        deduped = []
        for item in value:
            if item not in seen:
                seen.add(item)
                deduped.append(item)

        # Single-element list collapses to bare string.
        if len(deduped) == 1:
            return deduped[0]

        # Keyword-in-list guard runs against the deduped list — duplicate
        # keywords like ["remote", "remote"] collapse first and never reach
        # this guard.
        for keyword in ("any", "remote"):
            if keyword in deduped:
                raise ValueError(
                    f"{param_name}: keyword '{keyword}' cannot appear in a "
                    f"list with other elements (it already covers them)"
                )
        return deduped

    raise ValueError(
        f"{param_name}: must be str, list[str], or None, " f"got {type(value).__name__}"
    )


def _warn_redundant_host_combos(hosts, blocked_hosts, logger) -> None:
    """Warn on hosts/blocked_hosts combinations that simplify to a single
    keyword. Run AFTER both values are normalized.
    """
    if hosts != "any":
        return
    block_set = (
        {blocked_hosts} if isinstance(blocked_hosts, str) else set(blocked_hosts or [])
    )
    if "local" in block_set:
        logger.warning(
            "hosts='any' + blocked_hosts contains 'local' — simpler form is "
            "hosts='remote'."
        )
    if "remote" in block_set:
        logger.warning(
            "hosts='any' + blocked_hosts contains 'remote' — simpler form is "
            "hosts='local'."
        )


class Plexus:
    """Manages all plugins and facilitates communication between them."""

    def __init__(self, config_path: str):
        self.config_path = config_path

        # Pre-read config for logger bootstrap so the filter system has its
        # thresholds in place from the very first record. quickget_config
        # handles integrity failures; the try/except handles file-missing /
        # YAML parse errors — neither emits a log line at this point because
        # no logger exists yet, but load_config_yaml below will surface a
        # useful error if the YAML is genuinely broken.
        try:
            bootstrap_cfg = (
                ConfigUtil.quickget_config(config_path, fallback_value={}) or {}
            )
        except Exception:
            bootstrap_cfg = {}
        if not isinstance(bootstrap_cfg, dict):
            bootstrap_cfg = {}
        bootstrap_general = bootstrap_cfg.get("general", {})
        if not isinstance(bootstrap_general, dict):
            bootstrap_general = {}
        self._logger = LogUtil.create(
            log_level=bootstrap_general.get("console_log_level", "DEBUG"),
            file_level=bootstrap_general.get("file_log_level", "DEBUG"),
            logger_levels=bootstrap_general.get("logger_levels", {}),
        )

        self.yaml_config = None
        self.load_config_yaml(self.config_path)
        # change_level / change_file_level / apply_logger_levels_config now
        # live inside load_config_yaml so hot-reload picks up changes too.

        self.requests = {}
        self.request_lock = asyncio.Lock()
        # B-047 fix (Stage Q): task_list is currently-in-flight tasks
        # only. Per-task done_callback in _spawn_tracked evicts on
        # completion (replaces the previous 10s polling sweep that
        # left the list growing in the gap between sweeps + never
        # reclaimed hung tasks). recent_completed is a bounded buffer
        # of (name, started, completed, error) records for TUI / log
        # introspection of recently-finished tasks; counters provide
        # exact aggregate dispatch numbers even when bursts overflow
        # the deque.
        self.task_list: set = set()
        self.recent_completed: deque = deque(maxlen=200)
        self.tasks_started_total: int = 0
        self.tasks_completed_total: int = 0

        self.main_event_loop = None
        self.plugins = {}
        self.plugins_by_uuid = {}
        # Session 3 (v0.26.0): plugin state machine. Read-only data
        # container; all mutations through plx._transition_plugin(name, state).
        # External readers MUST snapshot before iterating: dict(plx.plugin_states).
        self.plugin_states: Dict[str, PluginState] = {}
        self.plugin_lock = asyncio.Lock()
        # Stage O: per-plugin lifecycle locks (B-046 fix). Each plugin
        # gets its own asyncio.Lock for serializing on_enable / on_disable
        # / pop_plugin / _reload_plugin on THAT plugin. plugin_lock
        # (global) is now used only for fast dict reads/writes
        # (plugins, plugins_by_uuid). Locks are leaked across the
        # process lifetime — bounded by the number of distinct plugin
        # names ever loaded; entries are not removed on pop_plugin so
        # a concurrent waiter on a popped-and-reloaded plugin keeps
        # lock identity.
        self._lifecycle_locks: Dict[str, asyncio.Lock] = {}
        # Dedicated thread pool for sync plugin endpoints — isolated from
        # Python's default executor to prevent deadlock under load.
        # See README "Sync vs Async Plugins: Thread Pool Deadlock Risk".
        self._plugin_executor = ThreadPoolExecutor(
            max_workers=32,
            thread_name_prefix="plugin",
        )
        self._init_tasks = []
        self.network = None
        # Hot-reload networking rebuild lock (Commit 2b). Acquired by
        # ``wait_until_ready()`` during boot AND by
        # ``_rebuild_networking`` (Step 7) during hot reload —
        # non-reentrant, held across NetworkManager construction +
        # ``network.start()`` so the two flows can't race. Per cycle 3
        # HIGH-α + Option A (one canonical construction site lives in
        # ``wait_until_ready``; ``start()`` is a thin shim).
        self._network_rebuild_lock: asyncio.Lock = asyncio.Lock()
        # B-073: Internal event bus state. Sync observer dispatch on the
        # loop thread; observers must return < 1ms (heavy work goes to
        # caller-spawned tasks). Topic prefix ``_core/`` reserved from
        # plugin author code by Step 9 validator change. Auto-cleanup on
        # pop_plugin via ``_unobserve_plugin`` called from
        # ``_pop_plugin_under_lock`` alongside
        # ``topic_registry.unsubscribe_plugin``. ``_observer_owners`` maps
        # plugin_uuid -> set of (topic, callback) for bulk-unobserve on
        # pop. Tuples are hashable because callables hash by identity
        # (bound methods hash by ``(func, instance)`` identity); set
        # membership and removal work correctly.
        self._internal_observers: Dict[str, List[Callable]] = {}
        self._observer_owners: Dict[str, Set[Tuple[str, Callable]]] = {}
        self.topic_registry = TopicRegistry(self._logger.getChild("notifier"))
        self._config_write_lock = threading.Lock()

        # PR3 Stage A: dedicated executor for sync subscriber handlers
        # (Q17 + C3 + C8). Default 4 workers, configurable via
        # `general.sync_dispatcher_workers`. Reads from the live yaml_config
        # which load_config_yaml has already populated above. Falls back to
        # 4 for any malformed/absent value (no config-time hard failure —
        # the framework never blocked startup on a bad sync-dispatcher
        # value before, so we keep that posture).
        general_cfg = self.yaml_config.get("general", {}) or {}
        raw_workers = general_cfg.get("sync_dispatcher_workers", 4)
        try:
            sync_workers = int(raw_workers)
            if sync_workers < 1:
                raise ValueError("must be >= 1")
        except (TypeError, ValueError):
            self._logger.warning(
                "Invalid general.sync_dispatcher_workers=%r; defaulting to 4",
                raw_workers,
            )
            sync_workers = 4
        self.sync_dispatcher = SyncDispatcher(
            workers=sync_workers,
            logger=self._logger.getChild("sync_dispatcher"),
        )

    async def wait_until_ready(self):
        """Ensure initialization tasks are started and await completion.

        Per Commit 2b Option A: this is the canonical NetworkManager
        construction site. ``start()`` delegates here. Construction is
        serialized via ``_network_rebuild_lock`` so a hot-reload
        triggered during boot waits cleanly until init completes
        (cycle 3 HIGH-α). The lock is held across both NM construction
        AND the ``await asyncio.gather(*self._init_tasks)`` so a
        rebuild triggered mid-``network.start()`` cannot race the
        in-progress startup.

        Idempotent — repeated calls re-await the existing init tasks
        rather than reconstructing.
        """
        # Ensure event loop and maintenance task
        if self.main_event_loop is None:
            self.main_event_loop = asyncio.get_running_loop()
            if self.yaml_config.get("general", {}).get("asyncio_debug", False):
                self.main_event_loop.set_debug(True)
                self.main_event_loop.slow_callback_duration = 0.5

        if not self._init_tasks:
            async with self._network_rebuild_lock:
                # Re-check under lock: another concurrent caller may
                # have constructed while we awaited the lock. Without
                # this guard, two parallel ``wait_until_ready`` callers
                # would both attempt construction; the second would
                # double-register tasks + duplicate the NM instance.
                if not self._init_tasks:
                    self._init_tasks.append(asyncio.create_task(self.load_plugins()))
                    if getattr(self, "networking_enabled", False):
                        if self.network is None:
                            self.network = self._build_network_manager(self.yaml_config)
                        self._init_tasks.append(
                            asyncio.create_task(self.network.start())
                        )
                    # Hold the lock until init tasks complete so a
                    # concurrent hot-reload can't fire mid-
                    # ``network.start()`` (cycle 3 HIGH-α).
                    await asyncio.gather(*self._init_tasks)
                    return

        # Tasks already created (and possibly already done) — just
        # await completion. Lock not needed: the construction-phase
        # holder released after its own gather, so we observe
        # post-construction state.
        if self._init_tasks:
            await asyncio.gather(*self._init_tasks)

    async def start(self):
        """Initialize background tasks, load plugins, and start networking.

        Thin shim that delegates to ``wait_until_ready()``. Per
        Commit 2b Option A: all NetworkManager construction lives in
        ``wait_until_ready()`` under ``_network_rebuild_lock`` so the
        boot path is serialized with concurrent hot-reload calls.

        Pre-Commit-2b, ``start()`` separately constructed NM, then
        called ``wait_until_ready()`` which would skip construction
        (NM was already non-None). Now: ``start()`` just initializes
        the loop reference + delegates. Behavior preserved on the
        happy path; concurrent hot-reload races are now serialized
        correctly.
        """
        await self.wait_until_ready()

    # ── B-073: Internal event bus ─────────────────────────────────────

    def internal_observe(
        self,
        plugin_uuid: str,
        topic: str,
        callback: Callable[[str, dict], None],
    ) -> None:
        """Register a sync observer for a ``_core/...`` framework topic.

        Loop-thread only. Plugin authors call via ``Plugin.internal_observe``
        (utils.py) which auto-fills ``plugin_uuid``; direct callers (test
        code, framework-internal) must pass ``plugin_uuid`` explicitly.

        Observers are called sync from the loop thread inside
        ``_internal_emit``; must return quickly (< 1ms). Heavy work goes
        to caller-spawned tasks. ``Exception`` subclasses raised by a
        callback are logged + swallowed (do not propagate to other
        observers). ``BaseException`` subclasses (``CancelledError``,
        ``KeyboardInterrupt``, ``SystemExit``) propagate up to the
        framework caller — observer authors must NOT raise those.

        Auto-cleanup: when the plugin owning ``plugin_uuid`` is popped via
        ``_pop_plugin_under_lock``, every observer registration owned by
        this plugin_uuid is removed (mirrors ``topic_registry.unsubscribe_plugin``).

        Idempotent: registering the same ``(topic, callback)`` pair twice
        for the same ``plugin_uuid`` is a no-op — single registration per
        pair, single dispatch per emit. This keeps ``_internal_observers``
        (per-topic list) and ``_observer_owners`` (per-plugin set) in
        symmetric step so ``_unobserve_plugin`` cleans up exactly what
        was registered.

        Worker-thread call from a sync endpoint dispatched via
        ``_plugin_executor`` races with loop-thread emits; not supported.
        Bridge via ``asyncio.run_coroutine_threadsafe(...)`` if needed.
        """
        owned = self._observer_owners.setdefault(plugin_uuid, set())
        pair = (topic, callback)
        if pair in owned:
            return  # idempotent — already registered
        owned.add(pair)
        self._internal_observers.setdefault(topic, []).append(callback)

    def internal_unobserve(
        self,
        plugin_uuid: str,
        topic: str,
        callback: Callable[[str, dict], None],
    ) -> bool:
        """Remove an observer registration. Returns ``True`` if removed.

        Removes the FIRST matching ``(topic, callback)`` pair owned by
        ``plugin_uuid`` via ``list.remove`` (equality-based; bound methods
        compare by ``(func, instance)`` identity). Silent no-op if the
        registration is absent (returns ``False``). Idempotent.

        Cleans up empty topic lists and empty owner sets so the dicts
        don't grow indefinitely under register/unregister churn.
        """
        lst = self._internal_observers.get(topic)
        if not lst:
            return False
        try:
            lst.remove(callback)
        except ValueError:
            return False
        if not lst:
            self._internal_observers.pop(topic, None)
        owned = self._observer_owners.get(plugin_uuid)
        if owned is not None:
            owned.discard((topic, callback))
            if not owned:
                self._observer_owners.pop(plugin_uuid, None)
        return True

    def _unobserve_plugin(self, plugin_uuid: str) -> int:
        """Remove all observer registrations owned by ``plugin_uuid``.

        Called from ``_pop_plugin_under_lock`` alongside
        ``topic_registry.unsubscribe_plugin`` so observer state mirrors
        topic-sub state on plugin removal. Without this, a popped plugin's
        bound-method observers keep the Plugin instance alive in
        ``_internal_observers`` indefinitely (memory leak) AND continue
        firing against a torn-down plugin instance.

        Returns count removed. Cleans up empty topic lists.
        """
        owned = self._observer_owners.pop(plugin_uuid, set())
        count = 0
        for topic, callback in owned:
            lst = self._internal_observers.get(topic)
            if not lst:
                continue
            try:
                lst.remove(callback)
                count += 1
                # Cleanup empty list inside the try so it only runs on a
                # successful remove. Outside the try, a ValueError (callback
                # absent) would still hit the cleanup against the unmodified
                # non-empty list — harmless today but a latent footgun under
                # future refactor.
                if not lst:
                    self._internal_observers.pop(topic, None)
            except ValueError:
                pass
        return count

    def _internal_emit(self, topic: str, /, **payload: Any) -> None:
        """Fire ``topic`` to all registered observers synchronously.

        Module-level ``_EMIT_DEPTH`` ContextVar guards against pathological
        recursive emits (depth >= ``_MAX_EMIT_DEPTH = 5`` aborts + logs).
        ``token = _EMIT_DEPTH.set(...)`` + ``_EMIT_DEPTH.reset(token)``
        restores parent-task state correctly under
        ``asyncio.create_task`` context inheritance — naive
        ``set(get() - 1)`` corrupts the parent slot.

        Snapshots the observer list before iteration so unregister-during-
        emit (e.g. an observer calling ``internal_unobserve`` on itself)
        is safe. ``Exception`` subclasses raised by an observer are logged
        + swallowed; never propagate to other observers or up to the
        caller. ``BaseException`` subclasses (``CancelledError``,
        ``KeyboardInterrupt``, ``SystemExit``) DO propagate — observer
        authors must NOT raise those.

        No-op fast path when no observer is registered for ``topic``
        (~50ns dict lookup). Per-event cost negligible at any reasonable
        load.

        Framework-internal: bypasses the leading-underscore validator
        that rejects plugin-author topics starting with ``_`` (Step 9).

        **Observer contract — payload is a plain dict, NOT unpacked
        kwargs.** Even though this method takes ``**payload`` kwargs at
        the emitter side, the observer is called as ``cb(topic, payload)``
        where ``payload`` is the captured-kwargs ``dict``. Observer
        signature is ``Callable[[str, dict], None]``:

            # emitter (framework code):
            self._internal_emit("_core/request/started", request_id="abc", plugin="X")

            # observer (plugin code):
            def my_observer(topic: str, payload: dict) -> None:
                request_id = payload["request_id"]
                plugin_name = payload["plugin"]

        Writing ``def my_observer(topic, **payload)`` would receive the
        dict as a single positional arg ``payload``, NOT the unpacked
        kwargs — TypeError on first key access.
        """
        listeners = self._internal_observers.get(topic)
        if not listeners:
            return
        depth = _EMIT_DEPTH.get()
        if depth >= _MAX_EMIT_DEPTH:
            self._logger.warning(
                "B-073 RECURSIVE EMIT DEPTH EXCEEDED at %d for topic %r — "
                "dropping event. Observer fan-out exceeded max depth %d; "
                "check observers for re-entrant framework calls.",
                depth,
                topic,
                _MAX_EMIT_DEPTH,
            )
            return
        snapshot = list(listeners)
        token = _EMIT_DEPTH.set(depth + 1)
        try:
            for cb in snapshot:
                try:
                    cb(topic, payload)
                except Exception:
                    self._logger.exception(
                        "B-073 internal observer raised on topic %r; "
                        "swallowed and continuing to next observer",
                        topic,
                    )
        finally:
            _EMIT_DEPTH.reset(token)

    async def close(self):
        """Gracefully shutdown: drain requests, disable plugins in reverse order, stop networking.

        B-073 Session 2 Step 4: ``running_loop`` + ``cleanup_requests``
        + ``cleanup_request_interval`` knob removed entirely. Done-callback
        eviction (Step 2's producer-finally pops + Step 3's outer-finally
        pops at all 7 framework Request migration sites) replaces the
        polling reap. There is no maintenance loop to stop on shutdown.
        """
        # 1. Wait for all in-flight request tasks to finish (up to 30s)
        # Snapshot via list() so concurrent done_callback eviction can't
        # mutate the set during iteration. (Single-threaded loop already
        # makes this safe but the snapshot keeps the intent explicit.)
        pending = [t for t in list(self.task_list) if not t.done()]
        if pending:
            self._logger.info(
                "Shutdown: waiting for %d in-flight request(s)...", len(pending)
            )
            done, still_pending = await asyncio.wait(pending, timeout=30)
            if still_pending:
                self._logger.warning(
                    "Shutdown: %d request(s) still running after 30s, cancelling...",
                    len(still_pending),
                )
                for t in still_pending:
                    t.cancel()
                await asyncio.gather(*still_pending, return_exceptions=True)
        # In-place clear so any callback firing after this still
        # operates on the same set object — discard() of an already-
        # absent key is a no-op.
        self.task_list.clear()

        # 2. Shutdown the SyncDispatcher (PR3 Stage A, Q17 + C8).
        # MUST happen AFTER the 30s in-flight drain. Per C8 spec: wrap
        # executor.shutdown(wait=True) in asyncio.wait_for with 30s
        # timeout. On timeout, fall through to wait=False semantics
        # (drop pending queued items, let still-running handlers
        # finish in the background).
        if hasattr(self, "sync_dispatcher") and self.sync_dispatcher is not None:
            self._logger.info("Shutdown: stopping sync dispatcher...")
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self.sync_dispatcher.executor.shutdown, wait=True
                    ),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                self._logger.warning(
                    "SyncDispatcher graceful shutdown exceeded 30s; forcing wait=False"
                )
                with contextlib.suppress(Exception):
                    self.sync_dispatcher.shutdown(wait=False)
            except Exception:
                self._logger.exception("SyncDispatcher shutdown failed")

        # 3. Disable plugins in REVERSE config order.
        #    Reverse order ensures dependents shut down before their dependencies.
        #    e.g. Discord_Bot_Plugin → DataCollection → PostgreSQL
        plugin_names = list(self.plugins.keys())
        plugin_names.reverse()

        for name in plugin_names:
            plugin = self.plugins.get(name)
            if not plugin or not plugin.enabled:
                continue
            self._logger.info("Shutdown: disabling %s...", name)
            try:
                # Delegate to _disable_plugin_under_lock instead of
                # duplicating the lifecycle_ready.clear() + on_disable +
                # unregister + state-transition sequence inline. The 30s
                # on_disable timeout is hardcoded here (shutdown cap);
                # runtime callers (disable_plugin, _pop_plugin_under_lock)
                # use the configurable plugin_disable_timeout per the
                # B-009 fix.
                #
                # Stage O: each plugin's lifecycle_lock instead of the
                # global plugin_lock. Concurrent ops (e.g. an in-flight
                # request still using a not-yet-disabled plugin) on
                # OTHER names are not blocked by THIS plugin's shutdown.
                lifecycle_lock = self._get_lifecycle_lock(name)
                async with lifecycle_lock:
                    await self._disable_plugin_under_lock(name, on_disable_timeout=30.0)
                self._logger.info("Shutdown: %s disabled", name)
            except asyncio.TimeoutError:
                self._logger.warning(
                    "Shutdown: %s on_disable timed out after 30s", name
                )
            except Exception as e:
                self._logger.error("Shutdown: %s on_disable failed: %s", name, e)

        # Sweep any plugin-source per-logger thresholds. Covers never-enabled
        # plugins (the disable loop above skips them via the `enabled` guard)
        # and is idempotent for plugins already cleaned via pop_plugin.
        for sweep_name, sweep_plugin in list(self.plugins.items()):
            sweep_uuid = getattr(sweep_plugin, "plugin_uuid", None)
            if sweep_uuid:
                LogUtil.clear_logger_levels_owned_by(sweep_name, sweep_uuid)

        # 4. Stop networking
        if hasattr(self, "network") and self.network is not None:
            stop = getattr(self.network, "stop", None)
            if callable(stop):
                self._logger.info("Shutdown: stopping networking...")
                with contextlib.suppress(Exception):
                    await stop()

        # 5. Shutdown dedicated plugin executor
        if hasattr(self, "_plugin_executor") and self._plugin_executor:
            self._plugin_executor.shutdown(wait=False)

        self._logger.info("Shutdown complete")

    def _load_yaml_dict(self, config_path: str) -> dict:
        """Pure parse + integrity check. Does NOT mutate ``self``.

        Returns the parsed dict (or raises ConfigException on integrity
        failure / yaml.YAMLError on parse failure). The returned dict is
        the SAME object the caller will pass to ``_apply_yaml``;
        ``apply_configvalues`` (called from ``_apply_yaml``) mutates it
        in place, writing back resolved defaults (e.g. ``hostname``,
        ``port``, ``networking.enabled``). Callers wanting a stable
        pre-apply snapshot must deep-copy the dict before passing it on
        — Step 4's ``_normalize_networking_for_diff`` does exactly this.

        Split out so the hot-reload path can pre-validate a candidate
        config before any mutation hits self. Closes cycle 2 HIGH-3
        state-lie (``apply_configvalues`` used to mutate ``self`` before
        ``check_config_integrity`` completed; if integrity raised,
        ``self.networking_*`` already reflected the new yaml but
        ``self.network`` was old).

        NOT decorated with ``@log_errors`` — the public
        ``load_config_yaml`` wrapper carries the decorator, so failures
        are logged once at the public-API boundary instead of twice on
        the unwind.
        """
        self._logger.info(f"Loading config from config_path: {config_path}")
        yaml_dict = ConfigUtil.load_config(config_path)
        self._logger.info(yaml_dict)
        ConfigUtil.check_config_integrity(yaml_dict, self._logger)
        return yaml_dict

    def _apply_yaml(self, yaml_dict: dict) -> None:
        """Apply parsed yaml to ``self`` state. Caller is responsible for
        having already integrity-checked ``yaml_dict`` via
        ``_load_yaml_dict``.

        Mutates ``self.yaml_config`` (assigned to the passed-in dict by
        reference — same object), then calls
        ``ConfigUtil.apply_configvalues`` which writes resolved defaults
        BACK into ``self.yaml_config`` in place (e.g. ``hostname``,
        ``port``, ``networking.enabled``). The dict the caller passed is
        therefore mutated as a side effect — see ``_load_yaml_dict``'s
        docstring for the implication.

        Also re-applies LogUtil thresholds so hot-reload picks up
        log-level changes.

        NOT decorated with ``@log_errors`` — see ``_load_yaml_dict``'s
        note.
        """
        self.yaml_config = yaml_dict
        ConfigUtil.apply_configvalues(self)

        # Apply logging-related config last so hot-reload picks up changes.
        # On first boot LogUtil.create() already used the same values from the
        # bootstrap pre-read; on async_load_config_yaml() this is the only
        # place that re-applies them.
        general = self.yaml_config.get("general", {})
        if not isinstance(general, dict):
            general = {}
        LogUtil.change_level(general.get("console_log_level", "DEBUG"))
        LogUtil.change_file_level(general.get("file_log_level", "DEBUG"))
        LogUtil.apply_logger_levels_config(general.get("logger_levels", {}))

    @log_errors
    def load_config_yaml(self, config_path: str):
        """Sync entry point — load + integrity check + apply.

        Behavior change vs pre-Step-1: integrity-check raises now leave
        ``self.yaml_config`` unmodified (was previously overwritten with
        the bad-but-parsed dict). Closes cycle 2 HIGH-3 state-lie.
        """
        self._apply_yaml(self._load_yaml_dict(config_path))

    async def async_load_config_yaml(self, config_path: str):
        """Async config loader with hot-reload orchestration.

        Behavior matrix:

        * Bootstrap (``self.yaml_config is None``): apply new config
          directly. NO networking action — NetworkManager construction
          happens later in ``wait_until_ready()`` via Option A. NOTE:
          unreachable in practice — ``Plexus.__init__`` calls
          ``load_config_yaml`` synchronously, populating
          ``self.yaml_config`` before any caller reaches this method.
          Kept as a defensive guard.
        * No networking change (``_networking_config_changed=False``):
          apply new config + update live NM attrs in place via
          ``_update_networking_in_place``.
        * Networking change (any field in ``_REBUILD_FIELDS``):
          pre-validate via ``_validate_networking_config`` (raises →
          abort, no state mutation), then ``_rebuild_networking``
          (which acquires the rebuild lock + does the ordered
          tear-down + rebuild).

        Rebuild orchestrator design per Commit 2b cycle 3 settled spec.
        """
        new_yaml = self._load_yaml_dict(config_path)
        old_yaml = self.yaml_config

        if old_yaml is None:
            # Defensive path — unreachable under current __init__
            # ordering, but kept so a future change to construction
            # order doesn't silently bypass the rebuild orchestrator.
            self._apply_yaml(new_yaml)
            return

        if self._networking_config_changed(old_yaml, new_yaml):
            # Pre-validate before any state mutation. Bad config →
            # abort cleanly; old network keeps running.
            try:
                self._validate_networking_config(new_yaml)
            except Exception as e:
                self._logger.error(
                    "async_load_config_yaml: networking config "
                    "validation failed; reload aborted, old config "
                    "remains active. Error: %s",
                    e,
                    exc_info=True,
                )
                return
            await self._rebuild_networking(new_yaml)
        else:
            self._apply_yaml(new_yaml)
            self._update_networking_in_place(new_yaml)

    async def _rebuild_networking(self, new_yaml: dict) -> None:
        """Rebuild the NetworkManager for hot-reload of peers /
        enabled / port / hostname / keys_dir changes.

        Acquires ``_network_rebuild_lock`` so concurrent boot or other
        rebuild calls serialize. Per cycle 3 HIGH-α + Option A.

        Ordering (cycle 3 design — DO NOT REORDER):

        1. Build new NM via ``_build_network_manager(new_yaml)``. If
           raises, no state mutation; old keeps running. (Skipped if
           new yaml has ``networking.enabled=False`` — there's nothing
           to construct in that case.)
        2. ``_apply_yaml(new_yaml)`` — applies new config to
           ``self.yaml_config`` and ``self.networking_*``.
        3. Snapshot ``old_nm = self.network``.
        4. ``self.network = None`` — guards in 4 sites + 7 snapshot
           sites observe None from here. Held until end of rebuild.
        5. Drain in-flight remote requests (10s budget) +
           ``old_nm._inflight_publishes``. Pessimistic drain (per
           cycle 7 fix): includes any not-done request not explicitly
           stamped ``_is_remote=False``, since the stamp is set
           INSIDE ``_process_request*`` AFTER the request is already
           registered in ``self.requests``. Local requests resolve
           quickly so this is safe.
        6. ``await old_nm.stop()`` — try/except + log + continue. If
           ``stop()`` raises, the lock is still held; rebuild
           proceeds.
        7. ``await new_nm.start()`` — on raise: ``self.network = None``
           PERMANENTLY, log CRITICAL. Operator must reload-config to
           recover.
        8. ``self.network = new_nm`` — atomic assignment, end of gap.

        If new yaml has ``networking.enabled=False``, steps 1, 7, 8
        are skipped: ``self.network`` stays None permanently
        (network is now disabled by config).

        Cancellation: if this coroutine is cancelled between step 4
        and step 8, ``self.network`` stays None permanently —
        operator reload required to recover. Same terminal state as
        new-NM start failure. No try/finally restores old_nm because
        old has been stopped (step 6) and stopping a stopped NM is
        undefined.

        Documented imperfections (B-079):

        * Drain MISSES ``request_event_remote`` /
          ``request_event_stream_remote`` direct-await calls (no
          Request object → no ``_is_remote`` stamp + no
          ``self.requests`` registration). Caller may see
          ``ConnectionResetError`` mid-rebuild; retry on caller side.
        * Drain races against ``_drop_peer_advert_state`` cleanup —
          if a peer disconnect fires concurrent with rebuild, drain
          may miss tasks already cancelled by disconnect. Same
          retry-on-caller acceptable.
        * ``peer_stats`` counters reset (fresh NM = fresh dict).
          Per O7 acceptable.
        * Surviving tasks past the 10s drain timeout get cancelled
          silently by ``old_nm.stop()`` via
          ``_drop_peer_advert_state``. The drain warning fires first;
          no second log when stop() does the cancellation.
        """
        async with self._network_rebuild_lock:
            # Step 1: build new NM (no state mutation if this raises).
            nw_cfg = new_yaml.get("networking") or {}
            new_enabled = bool(nw_cfg.get("enabled", False))
            new_nm = None
            if new_enabled:
                try:
                    new_nm = self._build_network_manager(new_yaml)
                except Exception as e:
                    self._logger.error(
                        "_rebuild_networking: NetworkManager "
                        "construction failed; old network remains "
                        "active. Error: %s",
                        e,
                        exc_info=True,
                    )
                    return  # NO state mutation

            # Step 2: apply new yaml to self state.
            self._apply_yaml(new_yaml)

            # Step 3-4: snapshot old + null self.network for the gap.
            # Guards in the 4 + 7 sites observe None from here on.
            old_nm = self.network
            self.network = None

            # Step 5: drain in-flight remote requests + inflight
            # publishes (best-effort; surviving tasks log warning +
            # continue).
            await self._drain_for_rebuild(old_nm, timeout=10.0)

            # Step 6: stop old (best-effort; log on failure, continue).
            if old_nm is not None:
                try:
                    await old_nm.stop()
                except Exception as e:
                    self._logger.warning(
                        "_rebuild_networking: old NetworkManager "
                        "stop() raised; continuing. Error: %s",
                        e,
                        exc_info=True,
                    )

            # Step 7+8: start new NM and assign atomically.
            if new_nm is not None:
                try:
                    await new_nm.start()
                except Exception as e:
                    self._logger.critical(
                        "_rebuild_networking: new NetworkManager "
                        "start() failed; networking is DOWN until "
                        "next reload. Error: %s",
                        e,
                        exc_info=True,
                    )
                    # self.network stays None — operator-recovery via
                    # reload.
                    return
                self.network = new_nm
            # else: new yaml has networking.enabled=False → leave
            # self.network = None.

    async def _drain_for_rebuild(self, old_nm, timeout: float = 10.0) -> None:
        """Drain in-flight remote-bound work for a clean teardown.

        Two snapshot sources:

        * ``self.requests`` filtered for not-done + not explicitly
          ``_is_remote=False``. **Pessimistic filter** (cycle 7 HIGH-1
          fix): the stamp is set INSIDE ``_process_request*`` AFTER
          the request is already in ``self.requests``, so a request
          that hasn't yet reached the RemotePlugin branch wouldn't
          be caught by a strict ``_is_remote=True`` filter. Including
          unclassified requests is safe — local ones complete quickly
          via the local dispatch path so ``asyncio.wait`` returns them
          immediately.
        * ``old_nm._inflight_publishes`` — fire-and-forget per-peer
          publish tasks (PR3 Stage C).

        Best-effort: surviving tasks log warning at timeout expiry;
        the rebuild continues regardless. Per cycle 1 HIGH-1 + cycle
        2 HIGH-C.
        """
        pending = []

        # Snapshot Request-tracked in-flight requests under lock.
        # Pessimistic: include any not-done request that isn't
        # explicitly stamped ``_is_remote=False`` (no request ever is,
        # so this catches stamped-True + unstamped).
        async with self.request_lock:
            for req in list(self.requests.values()):
                if req._future.done():
                    continue
                if getattr(req, "_is_remote", None) is False:
                    continue
                pending.append(req._future)

        # Snapshot old_nm's _inflight_publishes under its struct lock.
        if old_nm is not None:
            try:
                async with old_nm._adverts_struct_lock:
                    for peer_set in list(old_nm._inflight_publishes.values()):
                        pending.extend(t for t in peer_set if not t.done())
            except Exception:
                self._logger.debug(
                    "_drain_for_rebuild: snapshot "
                    "_inflight_publishes raised; continuing with "
                    "partial drain.",
                    exc_info=True,
                )

        if not pending:
            return

        self._logger.info(
            "_rebuild_networking: draining %d in-flight task(s) " "(timeout=%.1fs)...",
            len(pending),
            timeout,
        )
        try:
            done, still_pending = await asyncio.wait(pending, timeout=timeout)
            if still_pending:
                self._logger.warning(
                    "_rebuild_networking: %d task(s) still pending "
                    "after %.1fs; continuing rebuild (surviving "
                    "tasks will be cancelled by old NM's stop()).",
                    len(still_pending),
                    timeout,
                )
        except Exception as e:
            self._logger.warning(
                "_rebuild_networking: drain await failed: %s; " "continuing rebuild.",
                e,
            )

    def _update_networking_in_place(self, yaml_config: dict) -> None:
        """Update non-rebuild networking fields on the live NM in
        place. Called from the rebuild orchestrator's else-branch
        when ``_networking_config_changed`` returned False.

        Fields updated:

        * ``heartbeat_interval`` / ``lookup_interval`` /
          ``liveness_timeout`` — live effect: heartbeat / discovery /
          liveness loops use the new value on next tick.
        * ``discover_nodes`` — toggle live; lookup_loop checks attr
          each tick.
        * ``direct_discoverable`` / ``auto_discoverable`` — read by
          the INFO handler; next inbound INFO observes new value.
        * ``pool_size`` — best-effort: only affects pools created
          AFTER this call. Existing pools keep their construction-
          time ``maxsize``.
        * ``secret`` / ``cert_file`` / ``key_file`` — best-effort:
          legacy fields; effective only at NM construction time.
          Updating attrs has no real effect on existing connections.

        NO rebuild needed because these don't change wire identity
        or server bind state. No-op when ``self.network`` is None
        (networking disabled or boot incomplete).
        """
        nm = self.network
        if nm is None:
            return

        nw_cfg = yaml_config.get("networking") or {}
        if not isinstance(nw_cfg, dict):
            return

        from .networking import (
            DEFAULT_HEARTBEAT_INTERVAL as _DEF_HB,
            DEFAULT_LOOKUP_INTERVAL as _DEF_LOOK,
            DEFAULT_LIVENESS_TIMEOUT as _DEF_LIVE,
        )

        def _safe_float(val, default):
            try:
                f = float(val)
                return f if f > 0 else default
            except (TypeError, ValueError):
                return default

        nm.heartbeat_interval = _safe_float(
            nw_cfg.get("heartbeat_interval", _DEF_HB), _DEF_HB
        )
        nm.lookup_interval = _safe_float(
            nw_cfg.get("lookup_interval", _DEF_LOOK), _DEF_LOOK
        )
        nm.liveness_timeout = _safe_float(
            nw_cfg.get("liveness_timeout", _DEF_LIVE), _DEF_LIVE
        )
        nm.discover_nodes = nw_cfg.get("discover_nodes", False)
        nm.direct_discoverable = nw_cfg.get("direct_discoverable", False)
        nm.auto_discoverable = nw_cfg.get("auto_discoverable", False)
        if nm.auto_discoverable and not nm.direct_discoverable:
            nm.direct_discoverable = True
        nm.pool_size = nw_cfg.get("pool_size", 5)
        # Legacy fields — best-effort attr update, no real effect
        # without a rebuild.
        if nw_cfg.get("secret") is not None:
            s = nw_cfg.get("secret")
            nm.secret = s.encode() if isinstance(s, str) else s
        nm.cert_file = nw_cfg.get("cert_file")
        nm.key_file = nw_cfg.get("key_file")

    def _build_network_manager(self, yaml_config: dict) -> NetworkManager:
        """Construct a fresh NetworkManager from a yaml_config dict.

        Reads EVERY field from ``yaml_config["networking"]`` dict — NOT
        from ``self.networking_*`` attrs (which may be stale during a
        rebuild that hasn't yet called ``_apply_yaml``). This independence
        is the core of the rebuild ordering:
          1. Build new NetworkManager from new_yaml (no self-state read)
          2. If construct raises → no state mutation, abort cleanly
          3. _apply_yaml(new_yaml) — only after construction succeeds

        Per cycle 3 HIGH-γ.

        Field source-of-truth (per cycle 3 HIGH-γ — must NOT read
        ``self.networking_*``):

        * ``port`` / ``auto_discoverable`` / ``direct_discoverable`` —
          read from ``yaml_config["networking"]``. ``apply_configvalues``
          writes these back to the dict in place after parsing
          (utils.py:1056-1092), so post-apply or fresh-parse gives
          identical values.
        * ``heartbeat_interval`` / ``lookup_interval`` /
          ``liveness_timeout`` — read from yaml_config and parsed via
          ``_safe_float`` (matches ``apply_configvalues``' parsing with
          identical ``<= 0`` rejection boundary, utils.py:1124-1166).
        * ``secret`` / ``cert_file`` / ``key_file`` / ``pool_size`` —
          read raw from yaml_config. ``apply_configvalues`` currently
          passes these through unchanged (utils.py:1098-1101), so
          behavior matches the inline construction sites'
          ``getattr(self, "networking_*")`` path.

        ASSUMPTION: any future change to ``apply_configvalues`` that
        transforms ``secret`` / ``cert_file`` / ``key_file`` /
        ``pool_size`` MUST either mirror that transformation here too,
        or move the storage to a write-back-into-yaml_config style so
        this helper continues to read the resolved value.

        Mirrors the auto/direct_discoverable forcing rule from
        ``ConfigUtil.apply_configvalues`` (auto=True → direct=True).
        All numeric fields fall back to defaults on bad-type input,
        matching ``apply_configvalues``' defensive parsing.
        """
        from pathlib import Path as _Path
        from .networking import (
            DEFAULT_HEARTBEAT_INTERVAL as _DEF_HB,
            DEFAULT_LOOKUP_INTERVAL as _DEF_LOOK,
            DEFAULT_LIVENESS_TIMEOUT as _DEF_LIVE,
        )

        nw_cfg = yaml_config.get("networking") or {}

        def _safe_float(val, default):
            try:
                f = float(val)
                return f if f > 0 else default
            except (TypeError, ValueError):
                return default

        auto_disc = nw_cfg.get("auto_discoverable", False)
        direct_disc = nw_cfg.get("direct_discoverable", False)
        if auto_disc and not direct_disc:
            direct_disc = True

        cfg_dir = _Path(self.config_path).parent

        return NetworkManager(
            self,
            self._logger.getChild("networking"),
            node_ips=nw_cfg.get("node_ips", []),
            discover_nodes=nw_cfg.get("discover_nodes", False),
            direct_discoverable=direct_disc,
            auto_discoverable=auto_disc,
            port=nw_cfg.get("port", 2510),
            secret=nw_cfg.get("secret"),
            cert_file=nw_cfg.get("cert_file"),
            key_file=nw_cfg.get("key_file"),
            pool_size=nw_cfg.get("pool_size", 5),
            networking_config=nw_cfg,
            config_dir=cfg_dir,
            heartbeat_interval=_safe_float(
                nw_cfg.get("heartbeat_interval", _DEF_HB), _DEF_HB
            ),
            lookup_interval=_safe_float(
                nw_cfg.get("lookup_interval", _DEF_LOOK), _DEF_LOOK
            ),
            liveness_timeout=_safe_float(
                nw_cfg.get("liveness_timeout", _DEF_LIVE), _DEF_LIVE
            ),
        )

    def _normalize_networking_for_diff(self, yaml_dict: dict) -> dict:
        """Return a copy of ``yaml_dict`` with rebuild-relevant defaults
        filled in.

        Used by Step 4's ``_networking_config_changed`` so the diff
        doesn't false-positive when one side has had
        ``apply_configvalues`` mutate it (writing back resolved defaults
        in-place) while the other is freshly parsed.

        Per cycle 3 HIGH-δ (originally cycle 3 LOW-1, upgraded).

        Fields normalised:

        * ``networking.enabled`` / ``networking.port`` /
          ``networking.auto_discoverable`` /
          ``networking.direct_discoverable`` — written back by
          ``apply_configvalues`` (utils.py:1056-1092). Defaults match
          ``apply_configvalues``'.
        * ``networking.keys_dir`` — read by ``NetworkManager.__init__``
          with default ``"_keys"`` (networking.py:176). Not written back
          by ``apply_configvalues`` but in ``_REBUILD_FIELDS``, so an
          implicit ``"_keys"`` candidate must compare equal to an
          explicit ``"_keys"`` live yaml.
        * ``general.hostname`` — written back by ``apply_configvalues``
          with ``socket.gethostname()`` fallback (utils.py:970-973).
          Also in ``_REBUILD_FIELDS`` (lives under ``general``, not
          ``networking``).
        * Auto-forces-direct rule mirrored (auto=True → direct=True)
          so a candidate with explicit auto/no-direct doesn't
          false-diff against post-apply live yaml that already had
          direct flipped to True.

        Top-level dict is shallow-copied; ``networking`` and ``general``
        sub-dicts are shallow-copied. Other sections share refs with
        the input — diff only inspects the rebuild fields, so deeper
        isolation is unnecessary.
        """
        import socket as _socket

        out = dict(yaml_dict)

        # Networking section defaults.
        nw_in = out.get("networking") or {}
        if not isinstance(nw_in, dict):
            nw_in = {}
        nw_out = dict(nw_in)
        nw_out.setdefault("enabled", False)
        nw_out.setdefault("port", 2510)
        nw_out.setdefault("auto_discoverable", False)
        nw_out.setdefault("direct_discoverable", False)
        nw_out.setdefault("keys_dir", "_keys")
        # Step 4 cycle 1: peers absent vs explicit empty list both
        # equal post-normalization. Without this default, an old
        # yaml with peers: [] vs a new yaml dropping the key (or
        # vice versa) would diff-as-different and trigger a spurious
        # rebuild on a change that has no operational effect.
        nw_out.setdefault("peers", [])
        if nw_out["auto_discoverable"] and not nw_out["direct_discoverable"]:
            nw_out["direct_discoverable"] = True
        out["networking"] = nw_out

        # General section: hostname is a rebuild field. apply_configvalues
        # fills socket.gethostname() when absent or empty (utils.py:970-973);
        # mirror exactly so a fresh-parsed candidate without hostname
        # doesn't false-positive against a post-apply live yaml.
        gen_in = out.get("general") or {}
        if not isinstance(gen_in, dict):
            gen_in = {}
        gen_out = dict(gen_in)
        if not gen_out.get("hostname"):
            gen_out["hostname"] = _socket.gethostname()
        out["general"] = gen_out

        return out

    def _validate_networking_config(self, yaml_config: dict) -> None:
        """Pre-flight validation of a candidate networking config. Raises
        on first malformed peer or bad config; returns None on success.

        Called from the rebuild orchestrator BEFORE any state mutation
        AND BEFORE the new ``NetworkManager`` is constructed. Lets the
        rebuild abort cleanly on bad config — the live network keeps
        running, no torn-down state.

        No side effects: no SSL context creation, no socket binds, no
        temp files, no instance-state mutation. Re-uses
        ``NetworkManager._parse_peers_dryrun`` static helper.

        Disabled-network candidate (``networking.enabled: false``)
        short-circuits with no validation — nothing to validate when
        the rebuild target is "stop networking".

        Empty-peers-when-enabled is NOT caught here — it propagates to
        the construction-failure path in Step 7 (``NetworkManager.start()``
        already raises with an actionable message at networking.py:1072+
        when peers is empty). The pre-validation gate covers per-peer
        parse / fingerprint / endpoint-uniqueness errors that would
        otherwise tear down the live network just to surface a config
        typo.
        """
        nw_cfg = yaml_config.get("networking") or {}
        if not isinstance(nw_cfg, dict):
            raise ValueError("networking section must be a mapping")
        if not nw_cfg.get("enabled", False):
            return  # disabled → nothing to validate

        port_default = nw_cfg.get("port", 2510)

        from pathlib import Path as _Path

        keys_dir_str = nw_cfg.get("keys_dir", "_keys")
        keys_dir_path = _Path(keys_dir_str)
        if not keys_dir_path.is_absolute():
            keys_dir_path = (_Path(self.config_path).parent / keys_dir_path).resolve()

        NetworkManager._parse_peers_dryrun(
            self._logger.getChild("networking"),
            nw_cfg.get("peers") or [],
            port_default=port_default,
            keys_dir=keys_dir_path,
        )

    def _networking_config_changed(self, old_yaml: dict, new_yaml: dict) -> bool:
        """Return True iff any field in ``_REBUILD_FIELDS`` differs
        between ``old_yaml`` and ``new_yaml``.

        Both sides are normalized via ``_normalize_networking_for_diff``
        before comparison so a fresh-parsed candidate (no defaults
        filled) doesn't false-positive against a post-apply live yaml
        (where ``apply_configvalues`` has written resolved defaults
        back in place). Per cycle 3 HIGH-δ.

        Hostname has TWO source paths (cycle 4 finding):

        * ``networking.hostname`` — read by ``NetworkManager.__init__``
          at networking.py:175 for ``self.hostname`` (the value the
          network layer uses on the wire).
        * ``general.hostname`` — read by ``apply_configvalues`` at
          utils.py:970-973 for ``plexus.hostname`` (the value the
          framework uses for topic-dispatch / sub author-id / etc.).

        ``apply_configvalues`` writes ONLY to ``general.hostname``
        (with ``socket.gethostname()`` fallback when absent/empty); it
        does NOT write to ``networking.hostname``. ``NetworkManager``
        falls back to ``socket.gethostname()`` independently for its
        own ``self.hostname`` if ``networking.hostname`` is absent.

        Both source paths must trigger rebuild on change so the live
        ``NetworkManager`` instance picks up a new hostname for either
        purpose. The diff compares both.

        Other networking fields (``heartbeat_interval`` /
        ``lookup_interval`` / ``liveness_timeout`` / ``pool_size`` /
        ``discover_nodes`` / ``direct_discoverable`` /
        ``auto_discoverable`` / ``secret`` / ``cert_file`` /
        ``key_file``) update the live NetworkManager attrs in place
        via ``_update_networking_in_place`` (Step 7) — NOT in
        ``_REBUILD_FIELDS``.

        Caller contract: ``old_yaml`` and ``new_yaml`` MUST be non-None
        dicts. The Step 7 orchestrator short-circuits the
        ``old_yaml is None`` bootstrap case before reaching this
        method; ``new_yaml`` is guaranteed non-None because
        ``_load_yaml_dict``'s integrity check rejects empty/null yaml.
        """
        old_n = self._normalize_networking_for_diff(old_yaml)
        new_n = self._normalize_networking_for_diff(new_yaml)

        old_nw = old_n.get("networking") or {}
        new_nw = new_n.get("networking") or {}
        if old_nw.get("peers") != new_nw.get("peers"):
            return True
        if old_nw.get("enabled") != new_nw.get("enabled"):
            return True
        if old_nw.get("port") != new_nw.get("port"):
            return True
        if old_nw.get("keys_dir") != new_nw.get("keys_dir"):
            return True
        # networking.hostname — NetworkManager-internal source path.
        # apply_configvalues never writes this key, so both sides read
        # raw yaml. Both absent → both None → equal. Both fall back to
        # socket.gethostname() inside NetworkManager.__init__ at
        # construction time.
        if old_nw.get("hostname") != new_nw.get("hostname"):
            return True

        # general.hostname — framework-canonical source path filled by
        # apply_configvalues with socket.gethostname() fallback.
        # _normalize_networking_for_diff mirrors that fallback so an
        # absent-vs-explicit-default doesn't false-positive.
        old_gen = old_n.get("general") or {}
        new_gen = new_n.get("general") or {}
        if old_gen.get("hostname") != new_gen.get("hostname"):
            return True

        return False

    # ── Per-logger threshold API (delegates to LogUtil) ─────────────
    # Plugins should call self.set_logger_level / self.clear_logger_level /
    # self.list_logger_levels via the Plugin base helpers — those auto-fill
    # the owner tuple. These wrappers exist so the helpers have something to
    # delegate to and so admin/CLI code can pass owner explicitly if needed.

    def set_logger_level(
        self,
        name: str,
        *,
        console: Optional[str] = None,
        file: Optional[str] = None,
        plugin_name: str,
        plugin_uuid: str,
    ) -> None:
        LogUtil.set_logger_level(
            name, console=console, file=file, owner=(plugin_name, plugin_uuid)
        )

    def clear_logger_level(
        self,
        name: str,
        *,
        console: bool = True,
        file: bool = True,
        plugin_name: Optional[str] = None,
        plugin_uuid: Optional[str] = None,
    ) -> None:
        if plugin_name is None or plugin_uuid is None:
            owner = None
        else:
            owner = (plugin_name, plugin_uuid)
        LogUtil.clear_logger_level(name, console=console, file=file, owner=owner)

    def list_logger_levels(self) -> dict:
        return LogUtil.list_logger_levels()

    # ── Config file editing ──────────────────────────────────────────

    @log_errors
    def list_config_files(self) -> Dict[str, str]:
        """Return {label: absolute_path} for main config and all plugin configs."""
        files = {}
        main_config = os.path.abspath(self.config_path)
        files["config.yml (main)"] = main_config

        for entry in self.yaml_config.get("plugins", []):
            name = entry.get("name", "")
            path = entry.get("path") or os.path.join(self.plugin_package, name)
            cfg = os.path.join(os.path.abspath(path), "plugin_config.yml")
            if os.path.isfile(cfg):
                files[f"{name}/plugin_config.yml"] = cfg

        return files

    @log_errors
    def read_config_file(self, path: str) -> str:
        """Read and return raw content of a known config file.

        Args:
            path: Absolute path to the config file.

        Returns:
            File content as string.

        Raises:
            FileNotFoundError: If path doesn't exist.
            ValueError: If path not in list_config_files().
        """
        abs_path = os.path.abspath(path)
        allowed = set(self.list_config_files().values())
        if abs_path not in allowed:
            raise ValueError(f"Path not in known config files: {path}")

        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()

    @log_errors
    def save_config_file(self, path: str, content: str, backup: bool = True) -> None:
        """Validate YAML, create .bak backup, and write content.

        Does NOT re-apply main config — call load_config_yaml() explicitly
        if you need settings to take effect immediately.

        Args:
            path: Absolute path to the config file.
            content: New YAML content to write.
            backup: Whether to create a .yml.bak before overwriting.

        Raises:
            ValueError: If path not in known config files or content parses to empty.
            yaml.YAMLError: If content is invalid YAML.
        """
        abs_path = os.path.abspath(path)
        allowed = set(self.list_config_files().values())
        if abs_path not in allowed:
            raise ValueError(f"Path not in known config files: {path}")

        parsed = yaml.safe_load(content)
        if not parsed:
            raise ValueError("Config content is empty or null after parsing")

        with self._config_write_lock:
            if backup and os.path.exists(abs_path):
                with open(abs_path, "r", encoding="utf-8") as f:
                    old_content = f.read()
                bak_path = abs_path + ".bak"
                with open(bak_path, "w", encoding="utf-8") as f:
                    f.write(old_content)

            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)

    def is_main_config(self, path: str) -> bool:
        """Check if path points to the main config.yml."""
        return os.path.abspath(path) == os.path.abspath(self.config_path)

    @async_log_errors
    async def load_plugins(self):
        # Load the plugins
        await self.get_plugins()

        # Enable them
        await self.start_plugins()

        self._logger.info(f"Finished Loading plugins!")

    @async_log_errors
    async def get_plugins(self) -> None:

        for plugin_entry in self.yaml_config.get("plugins", []):

            # Load and initiate the pluginclass
            await self.load_plugin_with_conf(plugin_entry)

    @async_log_errors
    async def start_plugins(self) -> None:
        """Start all plugin loops.

        Session 3 (v0.26.0): check state directly rather than via the
        Plugin.enabled property. The property returns True for ENABLING
        (mid-enable), so a stuck-ENABLING plugin would otherwise be
        silently skipped here. State == INACTIVE is the only enabling
        candidate; everything else is either enabled, mid-transition,
        or failed-load.
        """
        tasks = []
        task_plugins = []
        for plugin in self.plugins.values():
            ps = self.plugin_states.get(plugin.plugin_name)
            if ps is not None and ps.state == State.INACTIVE:
                tasks.append(self.enable_plugin(plugin.plugin_name))
                task_plugins.append(plugin)

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for plugin, result in zip(task_plugins, results):
                if isinstance(result, Exception):
                    self._logger.warning(
                        f'Error occured while enabling plugin with name "{plugin.plugin_name}": {type(result).__name__}: {result}'
                    )
                # task = asyncio.create_task(self.enable_plugin(plugin.plugin_name))
                # self.task_list.append(task)

    @async_log_errors
    async def load_plugin_with_conf(self, plugin_entry: list) -> None:

        async def error_config(message):
            self._logger.error(f"Plugin '{name}': {message}")
            await self.pop_plugin(name)

        async def warn_config(message):
            self._logger.warning(f"Plugin '{name}': {message}")

        # Get values
        name = plugin_entry["name"]

        # Validate plugin name shape (identifier + non-reserved). Done up
        # front so the rejection happens before file I/O.
        try:
            _validate_identifier_name(name, context="plugin name")
        except ValueError as e:
            await error_config(str(e))
            return

        if not plugin_entry.get("enabled"):
            self._logger.debug(
                f'Plugin "{name}" wont be loaded due to it being disabled'
            )
            if name in self.plugins:
                # Existing instance from a prior load — pop it. pop_plugin
                # transitions ENABLED→...→UNLOADED itself.
                await self.pop_plugin(name)
            else:
                # No instance to pop. Create or transition the plugin_states
                # entry so TUI/external code can see the plugin exists in
                # config but has no instance.
                if name in self.plugin_states:
                    if self.plugin_states[name].state != State.UNLOADED:
                        self._transition_plugin(name, State.UNLOADED)
                else:
                    self.plugin_states[name] = PluginState(
                        name=name, state=State.UNLOADED
                    )
            return

        if name in list(self.plugins.keys()):
            self._logger.info(
                f'Plugin "{name}" has an old instance, that will be overwritten'
            )
            await self.pop_plugin(name)

        # Resolve plugin directory
        path = plugin_entry.get("path") or os.path.join(self.plugin_package, name)
        path = os.path.abspath(path)
        if not os.path.exists(path):
            await error_config(f"Plugin directory missing: {name} ({path})")
            return

        # Load plugin config
        try:
            with open(
                os.path.join(path, "plugin_config.yml"), "r", encoding="utf-8"
            ) as f:
                plugin_config = yaml.safe_load(f)
        except Exception as e:
            await error_config(f"Failed loading config for {name}: {e}")
            return

        # Validate plugin config
        for field in ["description", "version", "remote", "arguments", "endpoints"]:
            if field not in plugin_config:
                await warn_config(f"{name} missing {field} in plugin_config.yml")

        # Early shape check on RAW endpoints config (PR2: dict keyed by
        # access_name). Catches the legacy list-form before override
        # merge runs — overrides on a list-shaped base would silently
        # discard the base. Detail validation (required fields, types,
        # access_name keys, internal_name shape) runs AFTER override
        # merge so override-introduced violations are also caught (C12).
        endpoints_raw = plugin_config.get("endpoints")
        if endpoints_raw is None:
            # Absent or null endpoints -> plugin has 0 endpoints. Skip rest.
            pass
        elif isinstance(endpoints_raw, list):
            await error_config(
                "endpoints: must be a dict keyed by access_name; list-form was "
                "removed in PR2. Convert each list entry to a dict entry "
                "keyed by its access_name."
            )
            return
        elif not isinstance(endpoints_raw, dict):
            await error_config(
                f"endpoints: must be a dict keyed by access_name; got "
                f"{type(endpoints_raw).__name__}."
            )
            return

        # ── Argument override application ────────────────────────────────
        # Base args from plugin_config.yml. Must be dict-or-null.
        base_args = plugin_config.get("arguments")
        if base_args is not None and not isinstance(base_args, dict):
            await error_config(
                f"top-level 'arguments' in plugin_config.yml must be a mapping (dict) "
                f"or omitted; got {type(base_args).__name__}"
            )
            return

        # Detect legacy top-level `arguments:` on the plugin entry — Q22:
        # field was renamed to `overrides.arguments:` in PR2.
        if "arguments" in plugin_entry:
            await warn_config(
                "main config 'arguments:' on plugin entry is a legacy field; "
                "use `overrides.arguments:` instead. Ignored."
            )

        # Q22: warn on any other unrecognized plugin-entry-level field.
        # Common mistake: writing `prefix:` or `verbose_notifier:` at the
        # plugin-entry level instead of inside `overrides:`. Warn + ignore.
        _KNOWN_PLUGIN_ENTRY_KEYS = frozenset(
            {
                "name",
                "enabled",
                "path",
                "overrides",
                # legacy `arguments:` already handled above with a tailored
                # message; include here so we don't double-warn.
                "arguments",
            }
        )
        for stray_key in plugin_entry.keys():
            if stray_key in _KNOWN_PLUGIN_ENTRY_KEYS:
                continue
            await warn_config(
                f"main config plugin entry for {name!r}: unknown field "
                f"{stray_key!r}; expected one of "
                f"{sorted(_KNOWN_PLUGIN_ENTRY_KEYS - {'arguments'})} "
                f"or place plugin_config overrides inside `overrides:` "
                f"(Q22). Ignored."
            )

        # Apply broader `overrides:` block from main config plugin entry.
        # Type-check (dict-or-None or warn-and-ignore deployment misconfig).
        override = plugin_entry.get("overrides")
        if override is None:
            merged_config = dict(plugin_config)
        elif not isinstance(override, dict):
            await warn_config(
                f"main config 'overrides' for '{name}' must be a mapping; "
                f"got {type(override).__name__}; ignoring overrides"
            )
            merged_config = dict(plugin_config)
        else:
            try:
                merged_config = apply_overrides(
                    plugin_config, override, name, self._logger
                )
            except ValueError as e:
                await error_config(f"override application failed: {e}")
                return

        # Validate MERGED endpoints (post-override). Override-introduced
        # violations (e.g. `__replace__: true` that drops required fields,
        # or a wrong-type boolean) are caught here. C12 contract.
        merged_endpoints = merged_config.get("endpoints")
        if merged_endpoints is None:
            # No endpoints after merge - fine, plugin has 0 endpoints.
            pass
        elif not isinstance(merged_endpoints, dict):
            # Should not happen given apply_overrides type checks, but
            # defensive guard. apply_overrides would raise on a non-dict
            # `endpoints:` override, and the early shape check above
            # rejected list/non-dict raw form.
            await error_config(
                f"endpoints (post-override) must be a dict keyed by access_name; "
                f"got {type(merged_endpoints).__name__}"
            )
            return
        else:
            for ep_key, endpoint in merged_endpoints.items():
                # The dict key is the canonical access_name. Validate it.
                try:
                    _validate_identifier_name(ep_key, context="endpoint access_name")
                except ValueError as e:
                    await error_config(str(e))
                    return

                if not isinstance(endpoint, dict):
                    await error_config(
                        f"endpoint '{ep_key}': value must be a mapping; got "
                        f"{type(endpoint).__name__}"
                    )
                    return

                # access_name field on the entry is optional; if present and
                # different from the dict key, warn and use the key.
                if "access_name" in endpoint:
                    ep_access = endpoint.get("access_name")
                    if ep_access != ep_key:
                        await warn_config(
                            f"endpoint '{ep_key}': 'access_name' field "
                            f"{ep_access!r} differs from dict key; using key "
                            f"{ep_key!r}"
                        )

                # remote, accessible_by_other_plugins still required (C12:
                # `__replace__: true` that omits these triggers this check).
                for field in ["remote", "accessible_by_other_plugins"]:
                    if field not in endpoint:
                        await error_config(
                            f"endpoint '{ep_key}' is missing {field} in plugin_config.yml"
                        )
                        return

                # internal_name optional; if present, validate as str/non-empty/ascii.
                if "internal_name" in endpoint:
                    iv = endpoint["internal_name"]
                    if type(iv) != str:
                        await error_config(
                            f"endpoint '{ep_key}': internal_name has wrong type "
                            f"{type(iv)} in plugin_config.yml as it must be a "
                            f"{str}"
                        )
                        return
                    if not iv.strip():
                        await error_config(
                            f"endpoint '{ep_key}': internal_name is empty in "
                            f"plugin_config.yml"
                        )
                        return
                    if not iv.isascii():
                        await error_config(
                            f"endpoint '{ep_key}': internal_name contains non "
                            f"ascii chars in plugin_config.yml"
                        )
                        return

                # Type checks for required boolean fields.
                for check in [
                    ("remote", bool, False, False),
                    ("accessible_by_other_plugins", bool, False, False),
                ]:  # ({config_option}, {type}, {empty_allowed}, {check_ascii})
                    if type(endpoint[check[0]]) != check[1]:
                        await error_config(
                            f"endpoint '{ep_key}': {check[0]} has wrong type "
                            f"{type(endpoint[check[0]])} in plugin_config.yml "
                            f"as it must be a {check[1]}"
                        )
                        return

        merged_args = merged_config.get("arguments")

        # Dynamic import
        module_path = os.path.join(path, "plugin.py")
        spec = importlib.util.spec_from_file_location(name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Find first Plugin subclass
        plugin_class = next(
            (
                cls
                for _, cls in inspect.getmembers(module, inspect.isclass)
                if issubclass(cls, Plugin) and cls != Plugin
            ),
            None,
        )
        if plugin_class is None:
            await error_config(f"No Plugin subclass found in {module_path}")
            return

        # Session 3 (v0.26.0): pre-create state entry BEFORE Plugin(...)
        # so the @property read inside Plugin.__init__ works (returns False
        # — only ENABLED state returns True from the property). On reload,
        # transition any existing entry (UNLOADED / FAILED_LOAD) to INACTIVE.
        if name in self.plugin_states:
            if self.plugin_states[name].state != State.INACTIVE:
                self._transition_plugin(name, State.INACTIVE)
            # C-021 fix: clear ONLY the prior Phase.LOAD error. Phase.ENABLE
            # / Phase.DISABLE errors from previous lifecycle cycles are
            # preserved so operators can still see why the plugin's last
            # enable / disable failed — re-load shouldn't wipe that
            # diagnostic context. The original `.clear()` wiped the whole
            # dict and lost those errors silently.
            self.plugin_states[name].last_errors.pop(Phase.LOAD, None)
        else:
            self.plugin_states[name] = PluginState(name=name, state=State.INACTIVE)

        # Instantiate with merged arguments. on_load runs inside __init__;
        # any raise (validation, missing config, plugin author error) puts
        # plugin_states[name] into FAILED_LOAD with traceback recorded.
        try:
            plugin = plugin_class(
                self._logger.getChild(name),
                self,
                arguments=merged_args,
                plugin_name=name,
            )
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                self.plugin_states[name].last_errors[Phase.LOAD] = ErrorRecord(
                    exception=exc,
                    traceback=traceback.format_exc(),
                    ts=time.time(),
                )
            self._transition_plugin(name, State.FAILED_LOAD)
            self._logger.error(
                f"Plugin '{name}': on_load raised — " f"{type(exc).__name__}: {exc}"
            )
            raise

        plugin.plugin_name = name
        plugin.version = merged_config.get("version") or "0.0.0 - not given"
        plugin.remote = merged_config.get("remote") or False
        plugin.description = merged_config.get("description") or "UNKNOWN"
        plugin.arguments = merged_args

        # PR3 Stage B: prefix + verbose_notifier plugin-level fields. prefix
        # defaults to plugin_name (per LOCKED J — author-default fallback).
        # verbose_notifier defaults to False (Q18). Both are overridable
        # via the standard overrides mechanism.
        prefix_val = merged_config.get("prefix")
        if prefix_val is None or (
            isinstance(prefix_val, str) and not prefix_val.strip()
        ):
            prefix_val = name
        if not isinstance(prefix_val, str):
            await warn_config(
                f"prefix must be a string; got {type(prefix_val).__name__}; "
                f"falling back to plugin_name"
            )
            prefix_val = name
        plugin.prefix = prefix_val

        # Q5: warn (allow) when another already-loaded plugin uses the
        # same prefix. Two instances sharing a prefix isn't an error
        # (intentional use case for running two Discord bots etc.) but
        # the warning helps the author spot accidental collisions.
        for existing_name, existing_plugin in self.plugins.items():
            if existing_name == name:
                continue
            existing_prefix = getattr(existing_plugin, "prefix", None)
            if existing_prefix == prefix_val:
                await warn_config(
                    f"plugin {name!r} prefix {prefix_val!r} collides with "
                    f"already-loaded plugin {existing_name!r} (Q5: warn + "
                    f"allow). Topic templates using {{prefix}} on either "
                    f"plugin will resolve to the same prefix segment — "
                    f"intentional only if you want shared advertisement."
                )

        verbose_val = merged_config.get("verbose_notifier", False)
        if not isinstance(verbose_val, bool):
            await warn_config(
                f"verbose_notifier must be a bool; got "
                f"{type(verbose_val).__name__}; falling back to False"
            )
            verbose_val = False
        plugin.verbose_notifier = verbose_val

        endpoints_cfg = merged_config.get("endpoints") or {}
        if not isinstance(endpoints_cfg, dict):
            # apply_overrides + the validator above already enforce dict shape.
            # Defensive guard against post-merge misshape.
            await warn_config(
                f"endpoints must be a dict keyed by access_name in "
                f"plugin_config.yml; got {type(endpoints_cfg).__name__}"
            )
            endpoints_cfg = {}
        plugin.endpoints = endpoints_cfg
        # Alias retained for backward-compatible callsite naming. Same dict.
        plugin._endpoint_by_access = plugin.endpoints

        # ── PR3 Stage B: parse events: and subscriptions: sections ─────
        # Both sections are optional, default to empty dict. Per LOCKED A.
        # Validation rules (LOCKED L + Q15 + Q16 + C20) applied here at
        # load. Topic templates resolved against load-time placeholders
        # ({prefix}, {plugin_name}, {hostname}, {plugin_uuid}); unknown
        # {var} placeholders are LEFT INTACT for runtime templating.
        events_cfg = merged_config.get("events")
        if events_cfg is None:
            events_cfg = {}
        if not isinstance(events_cfg, dict):
            await error_config(
                f"events: must be a mapping (dict keyed by event_id); got "
                f"{type(events_cfg).__name__}"
            )
            return

        subs_cfg = merged_config.get("subscriptions")
        if subs_cfg is None:
            subs_cfg = {}
        if not isinstance(subs_cfg, dict):
            await error_config(
                f"subscriptions: must be a mapping (dict keyed by "
                f"declared_id); got {type(subs_cfg).__name__}"
            )
            return

        hostname_val = self.hostname
        plugin_events: Dict[str, Dict[str, Any]] = {}
        for event_id, entry in events_cfg.items():
            try:
                _validate_identifier_name(event_id, context="event_id")
            except ValueError as e:
                await error_config(str(e))
                return
            if entry is None:
                entry = {}
            if not isinstance(entry, dict):
                await error_config(
                    f"events.{event_id}: entry must be a mapping; got "
                    f"{type(entry).__name__}"
                )
                return

            raw_topic = entry.get("topic")
            if raw_topic is None or not isinstance(raw_topic, str):
                await error_config(
                    f"events.{event_id}: 'topic' field is required and must "
                    f"be a string"
                )
                return

            # Resolve load-time placeholders (LOCKED J).
            resolved_topic = _resolve_load_time_template(
                raw_topic,
                prefix=plugin.prefix,
                plugin_name=name,
                hostname=hostname_val,
                plugin_uuid=plugin.plugin_uuid,
            )

            # Validate post-templating shape. Events forbid wildcards
            # (LOCKED L #1) but ALLOW {var} runtime placeholders (so we
            # only reject embedded * mid-segment + empty middle segments
            # + empty topic. Wildcards == any '*' character — but
            # _validate_topic_static checks segment-by-segment with
            # allow_wildcards=False rejecting ANY '*'. {var} placeholders
            # are FINE because they don't contain '*'.).
            try:
                stripped_topic = _validate_topic_static(
                    resolved_topic,
                    context=f"events.{event_id}.topic",
                    allow_wildcards=False,
                )
            except ValueError as e:
                await error_config(str(e))
                return

            # Validate event-entry hosts/blocked_hosts via the same
            # normalizer execute_sync uses (rejects empty list, empty
            # string in list, non-str items, etc.). Raw YAML values
            # otherwise reach _publisher_targets_local unchecked,
            # silently mishandling forms like `hosts: []` (would drop
            # all local fan-out without warning).
            try:
                eh = _normalize_hosts(
                    entry.get("hosts"),
                    param_name=f"events.{event_id}.hosts",
                    default=None,
                )
                ebh = _normalize_hosts(
                    entry.get("blocked_hosts"),
                    param_name=f"events.{event_id}.blocked_hosts",
                    default=None,
                )
            except ValueError as e:
                await error_config(str(e))
                return

            entry_dict = {
                "topic": stripped_topic,
                "hosts": eh,
                "blocked_hosts": ebh,
                "enabled": (bool(entry["enabled"]) if "enabled" in entry else True),
            }
            plugin_events[event_id] = entry_dict
        plugin.events = plugin_events

        plugin_subs: Dict[str, Dict[str, Any]] = {}
        for declared_id, entry in subs_cfg.items():
            try:
                _validate_identifier_name(declared_id, context="declared_id")
            except ValueError as e:
                await error_config(str(e))
                return
            if entry is None:
                entry = {}
            if not isinstance(entry, dict):
                await error_config(
                    f"subscriptions.{declared_id}: entry must be a mapping; "
                    f"got {type(entry).__name__}"
                )
                return

            raw_topic = entry.get("topic")
            if raw_topic is None or not isinstance(raw_topic, str):
                await error_config(
                    f"subscriptions.{declared_id}: 'topic' field is required "
                    f"and must be a string"
                )
                return

            target_access = entry.get("target_access_name")
            if (
                target_access is None
                or not isinstance(target_access, str)
                or not target_access.strip()
            ):
                await error_config(
                    f"subscriptions.{declared_id}: 'target_access_name' "
                    f"field is required and must be a non-empty string"
                )
                return

            # Resolve load-time placeholders FIRST (then reject {var} +
            # other invalid shapes). LOCKED J says unknown {var} is left
            # intact at load time; subscriptions then reject any
            # remaining {var} syntax (LOCKED L #2). Reserved-template
            # vars are resolved away here, so only USER {var} survives,
            # which is then rejected.
            resolved_topic = _resolve_load_time_template(
                raw_topic,
                prefix=plugin.prefix,
                plugin_name=name,
                hostname=hostname_val,
                plugin_uuid=plugin.plugin_uuid,
            )
            try:
                stripped_topic = _validate_subscription_topic(
                    resolved_topic,
                    context=f"subscriptions.{declared_id}.topic",
                )
            except ValueError as e:
                await error_config(str(e))
                return

            # Normalize sub-level filter values via _normalize_hosts (parity
            # with the events: section fix from cycle 6). Without this,
            # YAML forms like `hosts: []` (empty list — spec says invalid)
            # would silently produce a sub that rejects all delivery, with
            # no warning at load time. Same logic applied to blocked_hosts,
            # authors, blocked_authors.
            try:
                sh = _normalize_hosts(
                    entry.get("hosts", "any"),
                    param_name=f"subscriptions.{declared_id}.hosts",
                    default="any",
                )
                sbh = _normalize_hosts(
                    entry.get("blocked_hosts"),
                    param_name=f"subscriptions.{declared_id}.blocked_hosts",
                    default=None,
                )
                sa = _normalize_hosts(
                    entry.get("authors"),
                    param_name=f"subscriptions.{declared_id}.authors",
                    default=None,
                )
                sba = _normalize_hosts(
                    entry.get("blocked_authors"),
                    param_name=f"subscriptions.{declared_id}.blocked_authors",
                    default=None,
                )
            except ValueError as e:
                await error_config(str(e))
                return

            entry_dict = {
                "topic": stripped_topic,
                "target_access_name": target_access,
                "target_plugin": entry.get("target_plugin", name),
                "target_plugin_uuid": entry.get("target_plugin_uuid"),
                "hosts": sh,
                "blocked_hosts": sbh,
                "authors": sa,
                "blocked_authors": sba,
                "enabled": (bool(entry["enabled"]) if "enabled" in entry else True),
            }
            plugin_subs[declared_id] = entry_dict
        plugin.subscriptions = plugin_subs

        async with self.plugin_lock:
            self.plugins[name] = plugin
            # Maintain uuid index if available
            plugin_uuid = getattr(plugin, "plugin_uuid", None)
            if plugin_uuid:
                self.plugins_by_uuid[plugin_uuid] = plugin
            # Session 3: bind instance into plugin_states (state already
            # INACTIVE from pre-create above).
            self.plugin_states[name].instance = plugin

        # PR3 Stage B moved YAML subscription registration to
        # _register_yaml_subscriptions (called from enable_plugin) so
        # that disable -> re-enable re-registers subs. Stage D removed
        # the legacy `topic:` field auto-registration path entirely.

        self._logger.info(
            f"Successfully loaded plugin: {name} (Version: {plugin.version}, Path: {path})"
        )

    @async_log_errors
    async def pop_plugin(self, plugin_name: str) -> None:
        """Remove a plugin from the runtime.

        Session 3 (v0.26.0) state-machine semantics: the resulting
        plugin_states entry depends on whether config still references
        the plugin.

        - Config has the entry → state becomes UNLOADED (entry kept;
          can be re-enabled later via enable_plugin).
        - Config dropped the entry → entry is removed from plugin_states
          entirely.

        For FAILED_LOAD or UNLOADED entries with no live instance,
        pop_plugin still applies these semantics — useful for clearing
        a stale FAILED_LOAD record after the underlying issue is fixed
        and the config entry has been removed.
        """
        self._logger.info(f"Popping plugin: {plugin_name}")
        try:
            config_has_entry = any(
                p.get("name") == plugin_name
                for p in self.yaml_config.get("plugins", [])
            )

            if plugin_name not in self.plugins:
                if plugin_name in self.plugin_states:
                    if config_has_entry:
                        if self.plugin_states[plugin_name].state != State.UNLOADED:
                            self._transition_plugin(plugin_name, State.UNLOADED)
                    else:
                        del self.plugin_states[plugin_name]
                else:
                    self._logger.warning(
                        f'Plugin with name "{plugin_name}" doesnt exist'
                    )
                return

            lifecycle_lock = self._get_lifecycle_lock(plugin_name)
            async with lifecycle_lock:
                try:
                    await self._pop_plugin_under_lock(plugin_name)
                finally:
                    # Post-pop state transition. Cycle 1 review: must run
                    # in finally so a partial failure inside
                    # _pop_plugin_under_lock doesn't leave plugin_states
                    # inconsistent. Only fires if the instance is gone
                    # (i.e. the dict-pop step inside _pop_plugin_under_lock
                    # ran). If _pop_plugin_under_lock raised before the
                    # dict pop, the plugin is still in self.plugins, so
                    # we skip the UNLOADED transition — its state was
                    # set to INACTIVE by _disable_plugin_under_lock's
                    # finally and the next pop attempt can resume cleanly.
                    if (
                        plugin_name in self.plugin_states
                        and plugin_name not in self.plugins
                    ):
                        if config_has_entry:
                            # Cycle 3 fix: clear instance BEFORE the
                            # state transition so observers of
                            # _core/plugin/state_changed reading
                            # `instance` for state == UNLOADED see None,
                            # not a stale reference to the just-popped
                            # plugin.
                            self.plugin_states[plugin_name].instance = None
                            self._transition_plugin(plugin_name, State.UNLOADED)
                        else:
                            del self.plugin_states[plugin_name]
        except Exception as error:
            raise Exception(f'Error while popping plugin "{plugin_name}": {error}')

    @async_log_errors
    async def purge_plugins(self):
        # B-005 fix: delegate to pop_plugin per-name. pop_plugin fails
        # pending requests (request_lock loop) BEFORE disable, then
        # disables, pops dicts, unsubscribes, and clears logger levels
        # — the full cleanup path. The previous implementation called
        # disable_plugin per plugin then swept the dicts in a single
        # plugin_lock acquisition; that path skipped the pending-request
        # cancellation step (B-005). Reuses Stage O's
        # _pop_plugin_under_lock shared helper.
        #
        # Behavior on per-plugin failure: pop_plugin raises on error,
        # the loop aborts, and remaining plugins stay loaded. Plugins
        # already popped are fully cleaned up (different from the
        # previous all-at-once dict sweep, which left disabled-but-
        # still-in-dicts plugins on partial failure). Both shapes are
        # partial cleanups; this one cleans up incrementally.
        self._logger.info("Purging plugins")
        try:
            for plugin_name in list(self.plugins.keys()):
                await self.pop_plugin(plugin_name)
            self._logger.info("Purged all plugins")
        except Exception as error:
            raise Exception(f"Error while purging plugins: {error}")

    @async_log_errors
    async def purge_plugins_except(self, excluded_names: List[str]):
        """Purge all plugins except those in the excluded_names list.

        B-005 fix: delegate to pop_plugin per-name. See purge_plugins
        for the full rationale.
        """
        self._logger.info(f"Purging plugins except: {excluded_names}")
        try:
            plugins_to_purge = [
                name for name in list(self.plugins.keys()) if name not in excluded_names
            ]
            for plugin_name in plugins_to_purge:
                await self.pop_plugin(plugin_name)
            self._logger.info(
                f"Purged {len(plugins_to_purge)} plugins, kept {len(excluded_names)}"
            )
        except Exception as error:
            raise Exception(f"Error while purging plugins: {error}")

    @async_log_errors
    async def get_plugin_info(self, plugin_name: str) -> Optional[Dict[str, Any]]:
        """Get structured information about a plugin."""
        async with self.plugin_lock:
            plugin = self.plugins.get(plugin_name)
            if not plugin:
                return None

            return {
                "name": plugin.plugin_name,
                "version": getattr(plugin, "version", "unknown"),
                "uuid": plugin.plugin_uuid,
                "enabled": plugin.enabled,
                "remote": getattr(plugin, "remote", False),
                "description": getattr(plugin, "description", "No description"),
                "arguments": getattr(plugin, "arguments", None),
            }

    @async_log_errors
    async def get_plugin_endpoints(
        self, plugin_name: str
    ) -> Optional[List[Dict[str, Any]]]:
        """Get all endpoints for a plugin."""
        async with self.plugin_lock:
            plugin = self.plugins.get(plugin_name)
            if not plugin:
                return None

            endpoints = getattr(plugin, "endpoints", {})
            if not isinstance(endpoints, dict):
                return []

            return [
                {
                    "access_name": ep_key,
                    "internal_name": ep.get("internal_name", ep_key),
                    "remote": ep.get("remote", False),
                    "accessible_by_other_plugins": ep.get(
                        "accessible_by_other_plugins", False
                    ),
                    "description": ep.get("description", ""),
                    "tags": ep.get("tags", []),
                }
                for ep_key, ep in endpoints.items()
                if isinstance(ep, dict)
            ]

    @async_log_errors
    async def list_plugins_state(self) -> List[Dict[str, Any]]:
        """Return list of all loaded plugins with name, enabled, and description."""
        async with self.plugin_lock:
            return [
                {
                    "name": p.plugin_name,
                    "enabled": p.enabled,
                    "description": getattr(p, "description", "No description"),
                }
                for p in self.plugins.values()
            ]

    @async_log_errors
    async def graceful_shutdown(self):
        """Gracefully shutdown the system by closing Plexus."""
        self._logger.info("Initiating graceful shutdown...")
        await self.close()
        # Note: Stopping the event loop should be handled by the main application

    # ── Stage O lock helpers ─────────────────────────────────────────
    # Strict lock-acquisition order (deadlock-prevention rule):
    #   1. lifecycle_lock(plugin_name) — per-plugin, returned by
    #      _get_lifecycle_lock. Held across user on_enable / on_disable
    #      so concurrent ops on the SAME plugin serialize, but ops on
    #      DIFFERENT plugins do not (B-046 fix).
    #   2. request_lock — global. May be nested INSIDE lifecycle_lock
    #      (pop_plugin / _reload_plugin do this to fail pending requests
    #      atomically with the lifecycle transition); MUST NOT be
    #      acquired while holding plugin_lock.
    #   3. plugin_lock — global. Held briefly for self.plugins /
    #      self.plugins_by_uuid dict reads/writes; MUST be released
    #      before any user callback runs.
    #   4. topic_registry._lock — internal to TopicRegistry; acquired
    #      inside subscribe / unsubscribe.
    #
    # Network I/O note: _register_yaml_subscriptions returns the list of
    # newly-registered sub_uuids without broadcasting them; broadcast
    # happens AFTER plugin_lock is released (avoiding network I/O while
    # holding the global dict lock). Same for unregister: broadcast
    # remove-deltas happen outside plugin_lock too.
    def _get_lifecycle_lock(self, plugin_name: str) -> asyncio.Lock:
        """Return the per-plugin lifecycle lock, creating it on demand.

        Locks are leaked across the process lifetime — bounded by the
        number of distinct plugin names ever loaded. Lock entries are
        not removed on pop_plugin so a concurrent waiter on a
        popped-and-reloaded plugin keeps lock identity (so a sequence
        like: enable starts -> caller wants to pop -> reload re-creates
        -> caller still serializes against reload's enable).
        """
        lock = self._lifecycle_locks.get(plugin_name)
        if lock is None:
            lock = asyncio.Lock()
            self._lifecycle_locks[plugin_name] = lock
        return lock

    async def _wait_for_plugin_ready(self, plugin: Plugin) -> None:
        """Stage O readiness gate: wait for both readiness events.

        Waits for plugin._lifecycle_ready (framework-controlled, set
        after on_enable returns) and plugin.ready (author-controlled).
        Honors the configured timeout (`general.plugin_ready_timeout`)
        and emits a slow-wait WARNING when the wait took more than 1s
        so legitimate-but-slow startups are visible in logs.

        Raises asyncio.TimeoutError on expiry.

        R1 HIGH-1 fix: wait on BOTH events under a single
        asyncio.wait_for budget. The previous split-timeout pattern
        (wait on _lifecycle_ready, then on ready with `remaining`) hit
        a zero-budget bug — when _lifecycle_ready consumed the entire
        timeout, `remaining=0.0` and asyncio.wait_for(coro, timeout=0.0)
        raises TimeoutError immediately even when plugin.ready was
        already set.
        """
        timeout = getattr(self, "plugin_ready_timeout", DEFAULT_PLUGIN_READY_TIMEOUT)
        loop = self.main_event_loop or asyncio.get_running_loop()
        start = loop.time()

        async def _both():
            await plugin._lifecycle_ready.wait()
            await plugin.ready.wait()

        await asyncio.wait_for(_both(), timeout=timeout)
        elapsed = loop.time() - start
        if elapsed > 1.0:
            self._logger.warning(
                "[STAGE_O] readiness gate waited %.2fs for plugin %s",
                elapsed,
                plugin.plugin_name,
            )

    # ── Stage O locked-body helpers ──────────────────────────────────
    # The "_under_lock" suffix means: caller MUST already hold
    # self._get_lifecycle_lock(plugin_name). These bodies are reused by
    # pop_plugin / _reload_plugin without recursive lock acquisition.
    async def _enable_plugin_under_lock(self, plugin_name: str) -> None:
        """Body of enable_plugin minus the lifecycle_lock acquisition.

        Stage O: plugin_lock is held only for the dict read + state
        transition + YAML sub registration (microseconds). It is RELEASED
        before the user on_enable callback runs so concurrent ops on
        OTHER plugins (which acquire plugin_lock briefly themselves) are
        not blocked. _lifecycle_ready is set after on_enable returns.

        Session 3 (v0.26.0): the INACTIVE → ENABLING transition happens
        UNDER plugin_lock so concurrent observers see the consistent
        state. ENABLING → ENABLED happens after on_enable succeeds.
        ENABLING → INACTIVE happens in the rollback path. last_errors
        [Phase.ENABLE] is populated on Exception (not CancelledError).

        PR3 Stage B (Q23 + C15): YAML subs register BEFORE on_enable so
        the plugin starts with subs already live; events arriving during
        on_enable are dispatched to handlers (which exist by definition
        — methods on the plugin class). The Stage O readiness gate then
        blocks fan-out to a still-not-ready handler.
        """
        async with self.plugin_lock:
            plugin = self.plugins.get(plugin_name)
            ps = self.plugin_states.get(plugin_name)
            # Skip already-enabled or mid-enable plugins. Cycle 1 review:
            # ENABLING included so a defensive re-entry (any caller that
            # somehow bypasses the lifecycle_lock serialisation) cannot
            # double-register YAML subs.
            if (
                plugin is None
                or ps is None
                or ps.state in (State.ENABLING, State.ENABLED)
            ):
                return
            # Register YAML subs FIRST. Disabled subs (Q13 `enabled:
            # false`) ARE registered, but with the Subscription.enabled=
            # False flag so find_all/find_first skip them. Broadcast of
            # add-deltas happens AFTER plugin_lock release (below) — see
            # the Network I/O note in the lock-ordering rule.
            new_sub_uuids = await self._register_yaml_subscriptions(plugin)
            # Transition INACTIVE → ENABLING per Q23 + Q11 so handlers
            # are callable for self-publish-from-on_enable. Rollback to
            # INACTIVE on raise. POSS-W-D1-002 / W-A1-001: state flip
            # MUST stay under plugin_lock so a concurrent re-entry
            # checking ps.state ∈ {ENABLING, ENABLED} cannot double-
            # register YAML subs (cycle 1 review invariant) — but the
            # observer emit is deferred to AFTER lock release so a
            # sync observer scheduling async work cannot deadlock on
            # plugin_lock.
            enable_state_change = self._set_plugin_state_no_emit(
                plugin_name, State.ENABLING
            )

        # plugin_lock RELEASED. lifecycle_lock still held. Emit the
        # deferred state-change now that observers can safely use
        # async APIs that acquire plugin_lock. The broadcast
        # loop and on_enable call run together under one cancellation-
        # aware try/except/finally so a CancelledError mid-flight (which
        # is a BaseException, NOT Exception, so a plain `except Exception:`
        # would skip cleanup) still triggers full rollback.
        self._emit_plugin_state_change(plugin_name, *enable_state_change)
        ok = False
        try:
            # Broadcast add-deltas to peers OUTSIDE plugin_lock so a
            # slow/multi-peer broadcast doesn't block other dict ops
            # cluster-wide. _broadcast_yaml_sub_added is a no-op when
            # networking is disabled / not ready.
            for sub_uuid in new_sub_uuids:
                await self._broadcast_yaml_sub_added(sub_uuid)

            if asyncio.iscoroutinefunction(plugin.on_enable):
                await plugin.on_enable()
            else:
                await self.main_event_loop.run_in_executor(
                    self._plugin_executor, plugin.on_enable
                )
            # Stage O: signal lifecycle-ready AFTER on_enable returns
            # successfully. Other plugins blocked in the readiness gate
            # unblock here.
            plugin._lifecycle_ready.set()
            self._transition_plugin(plugin_name, State.ENABLED)
            ok = True
        except BaseException as exc:
            # Session 3: capture on_enable failure for last_errors. Skip
            # CancelledError — cancellation is not a plugin error (same
            # pattern as _disable_plugin_under_lock skipping TimeoutError).
            if not isinstance(exc, asyncio.CancelledError):
                self.plugin_states[plugin_name].last_errors[Phase.ENABLE] = ErrorRecord(
                    exception=exc,
                    traceback=traceback.format_exc(),
                    ts=time.time(),
                )
            raise
        finally:
            if not ok:
                # Rollback runs on Exception, CancelledError, or any
                # other BaseException out of the try block above.
                #
                # Sync state updates first (event clear/set — atomic,
                # cannot raise) so they're guaranteed regardless of
                # what happens during async cleanup below.
                plugin._lifecycle_ready.clear()
                # R1 HIGH-2 fix: restore plugin.ready to its
                # default-set state. If the plugin author cleared
                # self.ready inside on_enable and then on_enable
                # raised / was cancelled, the cleared event would
                # otherwise persist on the same Plugin instance and
                # stall any subsequent enable / cross-plugin call
                # behind the gate until timeout. The author's next
                # on_enable can clear it again if they want manual
                # control.
                plugin.ready.set()
                # B-004 fix: call user on_disable to give the plugin
                # a chance to undo partial setup from the failed
                # on_enable (per README: "on_enable must be fully
                # undoable by on_disable"). Author must write
                # on_disable defensively against partial state
                # (e.g. `if self.db_pool: await self.db_pool.close()`).
                #
                # Nested try/finally chain mirrors the shape used by
                # _disable_plugin_under_lock so the `enabled = False`
                # flip is the LAST action and runs unconditionally —
                # even on CancelledError mid-await. Each async cleanup
                # is wrapped in try/except Exception so a non-fatal
                # raise doesn't skip the next step. CancelledError
                # propagates through both try blocks (Exception
                # doesn't catch it) but the outer finally chain still
                # runs, guaranteeing the flag flip.
                #
                # POSS-W-D2-010 fix: wrap rollback on_disable in the
                # same asyncio.wait_for timeout that the runtime
                # disable_plugin / _pop_plugin_under_lock paths use
                # (B-009 fix), so a misbehaving rollback on_disable
                # cannot pin lifecycle_lock indefinitely. Sync
                # on_disable caveat from _disable_plugin_under_lock
                # docstring applies here too: wait_for cancels the
                # awaitable but cannot interrupt a thread blocked
                # inside the user's synchronous callback running in
                # _plugin_executor.
                rollback_disable_timeout = getattr(
                    self,
                    "plugin_disable_timeout",
                    DEFAULT_PLUGIN_DISABLE_TIMEOUT,
                )
                try:
                    try:
                        if asyncio.iscoroutinefunction(plugin.on_disable):
                            await asyncio.wait_for(
                                plugin.on_disable(),
                                timeout=rollback_disable_timeout,
                            )
                        else:
                            await asyncio.wait_for(
                                self.main_event_loop.run_in_executor(
                                    self._plugin_executor, plugin.on_disable
                                ),
                                timeout=rollback_disable_timeout,
                            )
                    except asyncio.TimeoutError:
                        self._logger.warning(
                            "[STAGE_O] _enable_plugin_under_lock rollback: "
                            "on_disable exceeded %.1fs timeout for plugin %r "
                            "— continuing rollback (subs will still be "
                            "unregistered, state will transition to INACTIVE)",
                            rollback_disable_timeout,
                            plugin.plugin_name,
                        )
                    except Exception:
                        self._logger.exception(
                            "[STAGE_O] _enable_plugin_under_lock rollback: "
                            "on_disable raised for plugin %r — partial "
                            "cleanup incomplete (original on_enable error "
                            "still propagates)",
                            plugin.plugin_name,
                        )
                finally:
                    try:
                        try:
                            await self._unregister_plugin_subscriptions(plugin)
                        except Exception:
                            self._logger.exception(
                                "[STAGE_O] _enable_plugin_under_lock rollback: "
                                "_unregister_plugin_subscriptions raised for "
                                "plugin %r — best-effort cleanup incomplete",
                                plugin.plugin_name,
                            )
                    finally:
                        # Session 3: sync state transition (cannot raise),
                        # guaranteed to run via the outer finally chain
                        # even if both async cleanups above are cancelled.
                        self._transition_plugin(plugin_name, State.INACTIVE)

    async def _disable_plugin_under_lock(
        self,
        plugin_name: str,
        on_disable_timeout: Optional[float] = None,
    ) -> None:
        """Body of disable_plugin minus the lifecycle_lock acquisition.

        Stage O: clears _lifecycle_ready BEFORE on_disable so any
        in-flight gate wait against this plugin times out rather than
        dispatching to a tearing-down plugin. plugin_lock is held only
        for the dict reads + state transition; user on_disable runs
        without it held.

        Session 3 (v0.26.0): the ENABLED → DISABLING transition happens
        UNDER plugin_lock so observers see the consistent state. The
        DISABLING → INACTIVE transition happens in the finally block —
        unconditional regardless of whether on_disable raised, was
        cancelled, or timed out (per O3). last_errors[Phase.DISABLE]
        is populated on Exception (not CancelledError or TimeoutError —
        TimeoutError is per-spec routine).

        PR3 Stage B (C15): YAML + runtime subs are unregistered AFTER
        on_disable returns. User code can publish/receive events during
        shutdown teardown.

        Optional ``on_disable_timeout`` wraps the user on_disable
        callback in ``asyncio.wait_for``. On expiry, raises
        ``asyncio.TimeoutError``; the finally block still runs the
        unregister + state transition, so callers that catch the
        TimeoutError can safely treat the plugin as INACTIVE. Callers
        who do NOT want a timeout pass None.

        Sync ``on_disable`` caveat: ``wait_for`` cancels the awaitable
        but cannot interrupt a thread blocked inside the user's
        synchronous callback running in ``_plugin_executor``. The
        event loop unblocks on time and the framework's bookkeeping
        (subs, state, dict pop) all complete; the worker thread keeps
        running until the user code naturally returns and may hold
        thread-pool capacity / external resources until then.

        Callers and their timeout values:
          - ``close()``                → 30.0 (hardcoded shutdown cap)
          - ``disable_plugin``         → ``plugin_disable_timeout`` (B-009 fix)
          - ``_pop_plugin_under_lock`` → ``plugin_disable_timeout`` (B-009 fix)

        ``_enable_plugin_under_lock``'s rollback-on-failure path calls
        ``plugin.on_disable()`` directly (not via this helper) but is
        now wrapped in the same ``asyncio.wait_for`` timeout via
        ``plugin_disable_timeout`` so a misbehaving rollback on_disable
        cannot pin ``lifecycle_lock``. See the rollback site comment.
        """
        async with self.plugin_lock:
            plugin = self.plugins.get(plugin_name)
            ps = self.plugin_states.get(plugin_name)
            if plugin is None or ps is None or ps.state != State.ENABLED:
                return
            # Session 3: transition ENABLED → DISABLING under plugin_lock
            # so any concurrent enable check sees DISABLING (not ENABLED)
            # and bails. Flip happens before plugin_lock release.
            # POSS-W-D1-002 / W-A1-001: state mutation stays under-lock
            # for the race-protection invariant above; observer emit is
            # deferred to AFTER lock release so a sync observer
            # scheduling async work cannot deadlock on plugin_lock.
            disable_state_change = self._set_plugin_state_no_emit(
                plugin_name, State.DISABLING
            )

        # plugin_lock RELEASED. Emit the deferred state-change now
        # that observers can safely use async APIs.
        self._emit_plugin_state_change(plugin_name, *disable_state_change)
        # Stage O: clear lifecycle-ready BEFORE on_disable so any
        # in-flight gate wait either re-fires against the cleared event
        # (and times out) rather than dispatching to a tearing-down
        # plugin.
        plugin._lifecycle_ready.clear()

        # plugin_lock RELEASED — run on_disable without holding it.
        # Outer try/except/finally guarantees the cleanup runs on any
        # exit path including CancelledError. Cleanup is itself nested
        # in try/finally so the state transition is the LAST action and
        # is unconditional — _transition_plugin is sync and cannot be
        # interrupted by cancellation. Without this nesting, a
        # cancellation hitting during _unregister_plugin_subscriptions
        # would skip the transition and leave the plugin in a stuck
        # DISABLING state.
        try:
            if asyncio.iscoroutinefunction(plugin.on_disable):
                if on_disable_timeout is not None:
                    await asyncio.wait_for(
                        plugin.on_disable(), timeout=on_disable_timeout
                    )
                else:
                    await plugin.on_disable()
            else:
                executor_call = self.main_event_loop.run_in_executor(
                    self._plugin_executor, plugin.on_disable
                )
                if on_disable_timeout is not None:
                    await asyncio.wait_for(executor_call, timeout=on_disable_timeout)
                else:
                    await executor_call
        except BaseException as exc:
            # Session 3: capture on_disable failure for last_errors. Skip
            # CancelledError (cancellation is not a plugin error) and
            # TimeoutError (per-spec routine, callers handle it cleanly).
            if not isinstance(exc, (asyncio.CancelledError, asyncio.TimeoutError)):
                self.plugin_states[plugin_name].last_errors[Phase.DISABLE] = (
                    ErrorRecord(
                        exception=exc,
                        traceback=traceback.format_exc(),
                        ts=time.time(),
                    )
                )
            raise
        finally:
            # Unregister all subs (YAML + runtime) regardless of
            # whether on_disable raised, was cancelled, or timed out.
            # Symmetric with rollback in _enable_plugin_under_lock.
            try:
                try:
                    await self._unregister_plugin_subscriptions(plugin)
                except Exception:
                    # B-050 fix: don't let an unregister failure mask
                    # the original on_disable exception. Without this
                    # except, a TimeoutError from on_disable's wait_for
                    # would be replaced by whatever
                    # _unregister_plugin_subscriptions raised, and any
                    # caller's `except asyncio.TimeoutError` would miss
                    # it. Mirrors the pattern in
                    # _enable_plugin_under_lock rollback.
                    self._logger.exception(
                        "_disable_plugin_under_lock: "
                        "_unregister_plugin_subscriptions raised for "
                        "plugin %r — original on_disable exception (if "
                        "any) still propagates; best-effort cleanup "
                        "incomplete",
                        plugin.plugin_name,
                    )
            finally:
                # Session 3: sync state transition (cannot raise),
                # guaranteed to run even if the unregister await above
                # is cancelled. plugin_lock is not needed here: any
                # find_endpoint reader that briefly sees DISABLING
                # before this line is already covered by the
                # _lifecycle_ready.clear() at the top (gate blocks).
                self._transition_plugin(plugin_name, State.INACTIVE)

    async def _pop_plugin_under_lock(self, plugin_name: str) -> bool:
        """Body of pop_plugin minus the lifecycle_lock acquisition.

        Caller MUST already hold ``self._get_lifecycle_lock(plugin_name)``.
        Returns True if a plugin was popped, False if absent (or popped
        by a concurrent caller between the caller's lock acquisition and
        the dict mutation below — defensive).

        Stage O: shared by ``pop_plugin`` and ``_reload_plugin`` so the
        pop body is not duplicated. Both callers already hold the
        per-plugin lifecycle_lock; this helper does not re-acquire it.
        Lock-ordering: request_lock (nested inside lifecycle_lock per
        the rule at ``_get_lifecycle_lock``) -> _disable_plugin_under_lock
        (briefly takes plugin_lock) -> plugin_lock for the final dict pop.
        """
        if plugin_name not in self.plugins:
            return False

        # B-073 (Session 2 Step 2 prep): snapshot under the lock,
        # iterate outside. Producer-side finally pops in
        # ``_process_request*`` mutate ``self.requests`` lock-free
        # (Python dict ``pop`` is GIL-atomic). Iterating directly
        # under the lock would risk ``RuntimeError: dictionary changed
        # size during iteration`` if a producer completes mid-await.
        # Snapshot copies references; already-popped entries' futures
        # short-circuit via the ``not done()`` check below.
        async with self.request_lock:
            snapshot = list(self.requests.values())
        for req in snapshot:
            if req.target_plugin == plugin_name and not req._future.done():
                await req.set_result(
                    f"Plugin {plugin_name} was unloaded while request was pending",
                    error=True,
                )

        ps_check = self.plugin_states.get(plugin_name)
        if (
            plugin_name in self.plugins
            and ps_check is not None
            and ps_check.state == State.ENABLED
        ):
            # B-009 fix: wrap user on_disable in asyncio.wait_for via
            # the configured runtime timeout (default 30s) so a hanging
            # on_disable can't block this plugin's lifecycle_lock
            # indefinitely. Symmetric with close()'s 30s cap.
            disable_timeout = getattr(
                self,
                "plugin_disable_timeout",
                DEFAULT_PLUGIN_DISABLE_TIMEOUT,
            )
            try:
                await self._disable_plugin_under_lock(
                    plugin_name, on_disable_timeout=disable_timeout
                )
            except asyncio.TimeoutError:
                # B-009: on_disable exceeded the runtime timeout. The
                # under-lock body's finally already transitioned to
                # INACTIVE and unregistered subs, so it's safe to
                # proceed with the dict pop below. Without this catch
                # the TimeoutError propagates up and leaves the plugin
                # half-removed (still in self.plugins / plugins_by_uuid,
                # topics + logger-level entries never cleaned).
                self._logger.warning(
                    "pop_plugin %r: on_disable exceeded %.1fs timeout; "
                    "continuing with pop (subs already unregistered, "
                    "state already INACTIVE)",
                    plugin_name,
                    disable_timeout,
                )

        async with self.plugin_lock:
            plugin = self.plugins.pop(plugin_name, None)
            if plugin is None:
                return False
            plugin_uuid = getattr(plugin, "plugin_uuid", None)
            if plugin_uuid and plugin_uuid in self.plugins_by_uuid:
                self.plugins_by_uuid.pop(plugin_uuid, None)
            if plugin_uuid:
                await self.topic_registry.unsubscribe_plugin(plugin_uuid)
                # B-073: bulk-unobserve every internal-event-bus observer
                # this plugin registered. Mirrors the topic-sub cleanup
                # above so observer state can't outlive the Plugin
                # instance (memory leak) AND post-pop emits don't
                # dispatch to a torn-down plugin's bound methods.
                self._unobserve_plugin(plugin_uuid)
                LogUtil.clear_logger_levels_owned_by(plugin_name, plugin_uuid)
        return True

    @async_handle_errors(None)
    async def enable_plugin(self, plugin_name: str):
        """Public-facing enable that acquires the per-plugin
        lifecycle_lock (Stage O) and delegates to
        _enable_plugin_under_lock. Concurrent enable on the SAME plugin
        serializes here; concurrent ops on OTHER plugins do not block.

        Session 3 (v0.26.0): renamed from `_enable_plugin` to public
        `enable_plugin`. State machine transitions are emitted via
        _transition_plugin under plugin_lock.
        """
        lifecycle_lock = self._get_lifecycle_lock(plugin_name)
        async with lifecycle_lock:
            await self._enable_plugin_under_lock(plugin_name)

    @async_log_errors
    async def disable_plugin(self, plugin_name: str):
        """Public-facing disable that acquires the per-plugin
        lifecycle_lock (Stage O) and delegates to
        _disable_plugin_under_lock. Waits for any in-progress
        enable_plugin on the same name to complete first.

        Session 3 (v0.26.0): renamed from `_disable_plugin` to public
        `disable_plugin`. State machine transitions are emitted via
        _transition_plugin.

        B-009 fix: user on_disable wrapped in asyncio.wait_for via
        on_disable_timeout — symmetric with close()'s 30s cap.
        Configurable via general.plugin_disable_timeout. On timeout
        the under-lock body's finally still unregisters subs and
        transitions to INACTIVE; this wrapper logs and returns cleanly
        rather than propagating asyncio.TimeoutError to callers
        (parity with close()'s per-plugin TimeoutError catch).
        """
        disable_timeout = getattr(
            self, "plugin_disable_timeout", DEFAULT_PLUGIN_DISABLE_TIMEOUT
        )
        lifecycle_lock = self._get_lifecycle_lock(plugin_name)
        async with lifecycle_lock:
            try:
                await self._disable_plugin_under_lock(
                    plugin_name, on_disable_timeout=disable_timeout
                )
            except asyncio.TimeoutError:
                self._logger.warning(
                    "disable_plugin %r: on_disable exceeded %.1fs timeout",
                    plugin_name,
                    disable_timeout,
                )

    def _transition_plugin(self, name: str, new_state: State) -> None:
        """Atomic state transition + _core/plugin/state_changed emit.

        Session 3 (v0.26.0). All state mutations on PluginState go through
        this method (D2). Direct field writes on PluginState are forbidden
        — code review enforces.

        Sync method (no await) so callers under existing locks can
        transition without re-acquiring. Observer dispatch in
        _internal_emit is sync per E2'. Sync observers must NOT acquire
        plugin_lock / lifecycle_lock / request_lock — see api_reference.md
        observer contract.

        POSS-W-D1-002 / D1-006: call sites that already hold
        ``plugin_lock`` should use ``_set_plugin_state_no_emit`` +
        ``_emit_plugin_state_change`` to separate the mutation (which
        must be under-lock for race protection) from the emit (which
        must NOT be under-lock so observers cannot self-deadlock).
        """
        old_state, _, ts = self._set_plugin_state_no_emit(name, new_state)
        self._emit_plugin_state_change(name, old_state, new_state, ts)

    def _set_plugin_state_no_emit(
        self, name: str, new_state: State
    ) -> Tuple[State, State, float]:
        """Mutate PluginState.state without firing the observer emit.

        Returns ``(old_state, new_state, ts)``. Callers under
        ``plugin_lock`` use this to keep the state flip atomic with
        their lock-protected work, then call
        ``_emit_plugin_state_change`` AFTER releasing the lock — so
        observers that schedule async work cannot deadlock on a lock
        still held by the transition path.
        """
        ps = self.plugin_states[name]
        old_state = ps.state
        ps.state = new_state
        ps.last_state_change = time.time()
        return old_state, new_state, ps.last_state_change

    def _emit_plugin_state_change(
        self, name: str, old_state: State, new_state: State, ts: float
    ) -> None:
        """Companion to ``_set_plugin_state_no_emit``. Fires the
        ``_core/plugin/state_changed`` internal-bus event for callers
        that deferred the emit until after their lock release.
        """
        self._internal_emit(
            "_core/plugin/state_changed",
            name=name,
            from_state=old_state.value,
            to_state=new_state.value,
            ts=ts,
        )

    @async_log_errors
    async def get_unloaded_metadata(self, plugin_name: str) -> Optional[Dict[str, Any]]:
        """Return metadata for an UNLOADED plugin by reading its on-disk
        plugin_config.yml.

        Returns None if the plugin is not in plugin_states or its state
        is not UNLOADED. For ENABLED / INACTIVE / etc., callers should
        use get_plugin_info(plugin_name) which reads from the live
        instance.

        Session 3 (v0.26.0): supports TUI listing of disabled-in-config
        plugins (per B1 design decision).
        """
        ps = self.plugin_states.get(plugin_name)
        if ps is None or ps.state != State.UNLOADED:
            return None
        entry = next(
            (
                p
                for p in self.yaml_config.get("plugins", [])
                if p.get("name") == plugin_name
            ),
            None,
        )
        if not entry:
            return None
        path = entry.get("path") or os.path.join(self.plugin_package, plugin_name)
        path = os.path.abspath(path)
        try:
            with open(
                os.path.join(path, "plugin_config.yml"),
                "r",
                encoding="utf-8",
            ) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            return None
        return {
            "name": plugin_name,
            "version": cfg.get("version", "unknown"),
            "description": cfg.get("description", ""),
            "path": path,
            "declared_endpoints": list((cfg.get("endpoints") or {}).keys()),
            "declared_events": list((cfg.get("events") or {}).keys()),
            "declared_subscriptions": list((cfg.get("subscriptions") or {}).keys()),
        }

    async def _register_yaml_subscriptions(self, plugin: Plugin) -> List[str]:
        """Register every YAML-declared subscription for ``plugin`` per
        Q23 + C15 + LOCKED A subscriptions: shape. Returns the list of
        newly-registered sub_uuids — caller is responsible for invoking
        ``_broadcast_yaml_sub_added`` on each AFTER releasing
        ``plugin_lock``. This keeps network I/O out of the global lock
        per the lock-ordering rule documented at _get_lifecycle_lock.

        Subscription registration runs at on_enable-time (not load-time)
        so that disable -> re-enable cycles re-register subs naturally.

        Disabled subs (``enabled: false``) get a Subscription with
        ``enabled=False`` so they live in the registry (visible to
        introspection / future advertisement) but are skipped by
        find_all/find_first matching.
        """
        # PR3 subscriptions: section.
        new_sub_uuids: List[str] = []
        subs_dict = getattr(plugin, "subscriptions", {}) or {}
        if isinstance(subs_dict, dict):
            for declared_id, entry in subs_dict.items():
                sub_uuid = await self.topic_registry.subscribe(
                    topic_pattern=entry["topic"],
                    plugin_name=plugin.plugin_name,
                    plugin_uuid=plugin.plugin_uuid,
                    target_plugin=entry.get("target_plugin", plugin.plugin_name),
                    target_access_name=entry["target_access_name"],
                    target_plugin_uuid=entry.get("target_plugin_uuid"),
                    hosts=entry.get("hosts", "any"),
                    blocked_hosts=entry.get("blocked_hosts"),
                    authors=entry.get("authors"),
                    blocked_authors=entry.get("blocked_authors"),
                    declared_id=declared_id,
                    enabled=bool(entry.get("enabled", True)),
                )
                plugin._sub_uuids.append(sub_uuid)
                new_sub_uuids.append(sub_uuid)
        return new_sub_uuids

    async def _broadcast_yaml_sub_added(self, sub_uuid: str) -> None:
        """Helper used by YAML-registration sites to push add-delta to
        peers. Wraps the get_subscription + ready-flag check in one place
        so the YAML loop stays clean."""
        # Snapshot nm. Per Commit 2b cycle 2 MED-B: a mid-block
        # hot-reload could otherwise leak the broadcast call onto a
        # stopped NM. Single-call site so the practical race window is
        # tiny, but snapshotting matches the pattern used by the loop
        # sites (publish_event / request_event / etc.) for consistency.
        nm = self.network
        if not (
            getattr(self, "networking_enabled", False)
            and nm is not None
            and getattr(nm, "is_ready", False)
        ):
            return
        sub = await self.topic_registry.get_subscription(sub_uuid)
        if sub is None:
            return
        try:
            await nm.broadcast_local_sub_added(sub)
        except Exception:
            self._logger.debug(
                "_broadcast_yaml_sub_added: broadcast failed", exc_info=True
            )

    async def _unregister_plugin_subscriptions(self, plugin: Plugin) -> None:
        """Unregister every sub (YAML + runtime) for ``plugin`` at
        on_disable end (C15). Uses unsubscribe_plugin which removes by
        plugin_uuid (covers BOTH YAML subs registered via the wrapper
        AND runtime subs registered via Plugin.subscribe — both share
        plugin_uuid). plugin._sub_uuids is cleared as a side-effect.
        """
        plugin_uuid = getattr(plugin, "plugin_uuid", None)
        if plugin_uuid:
            # PR3 Stage C remove-delta loop (locked #18 item 5). Snapshot
            # subs BEFORE the bulk-unsubscribe, then per-sub broadcast.
            # Snapshot nm (Commit 2b cycle 2 MED-B): the per-sub broadcast
            # loop below would otherwise leak calls onto a stopped NM if
            # a hot-reload swaps self.network mid-loop.
            nm = self.network
            if (
                getattr(self, "networking_enabled", False)
                and nm is not None
                and getattr(nm, "is_ready", False)
            ):
                try:
                    subs_to_remove = await self.topic_registry.get_plugin_subscriptions(
                        plugin_uuid
                    )
                except Exception:
                    subs_to_remove = []
                for sub in subs_to_remove:
                    try:
                        await nm.broadcast_local_sub_removed(sub)
                    except Exception:
                        self._logger.debug(
                            "_unregister_plugin_subscriptions: broadcast failed",
                            exc_info=True,
                        )
            await self.topic_registry.unsubscribe_plugin(plugin_uuid)
        plugin._sub_uuids = []

    @async_handle_errors(None)
    async def _reload_plugin(self, plugin_name: str):
        """Reload a plugin by disabling, removing, re-loading from
        config, and re-enabling.

        Stage O: the WHOLE pop+load+enable chain runs under the
        per-plugin lifecycle_lock so a concurrent enable_plugin caller
        on the same name can't interleave between the pop and the
        re-enable. The locked-body helpers (_pop_plugin_under_lock /
        _enable_plugin_under_lock) avoid recursive lock acquisition.

        Session 3 (v0.26.0): the natural state transition sequence is:
            ENABLED → DISABLING → INACTIVE → UNLOADED → INACTIVE → ENABLING → ENABLED
        for an enabled source. For an INACTIVE source:
            INACTIVE → UNLOADED → INACTIVE
        The INACTIVE → UNLOADED transition happens after
        _pop_plugin_under_lock (which only transitions ENABLED→INACTIVE).
        load_plugin_with_conf transitions back to INACTIVE on re-instantiation.
        """
        lifecycle_lock = self._get_lifecycle_lock(plugin_name)
        async with lifecycle_lock:
            ps = self.plugin_states.get(plugin_name)
            previously_enabled = ps is not None and ps.state == State.ENABLED

            await self._pop_plugin_under_lock(plugin_name)
            # Session 3: transition INACTIVE → UNLOADED to match the
            # public pop_plugin contract. load_plugin_with_conf will
            # transition back to INACTIVE on re-instantiation. Cycle 3
            # fix: clear instance BEFORE the transition so observers see
            # instance=None for state==UNLOADED.
            if plugin_name in self.plugin_states:
                self.plugin_states[plugin_name].instance = None
                if self.plugin_states[plugin_name].state != State.UNLOADED:
                    self._transition_plugin(plugin_name, State.UNLOADED)

            entry = next(
                (
                    p
                    for p in self.yaml_config.get("plugins", [])
                    if p.get("name") == plugin_name
                ),
                None,
            )
            if not entry:
                raise Exception(
                    f"Plugin '{plugin_name}' not found in config for reload"
                )

            await self.load_plugin_with_conf(entry)
            if previously_enabled:
                await self._enable_plugin_under_lock(plugin_name)

    @contextlib.asynccontextmanager
    async def request_context_async(self, request: Request):
        """Async context manager to handle requests.

        B-073 Session 2 Step 3: ``set_collected`` migrated to
        ``self.requests.pop`` per the done-callback eviction model.
        ``Request.set_collected`` was a no-op flag-setter; the producer's
        finally in ``_process_request`` already pops the request, but
        this outer context-manager pop is symmetric (idempotent under
        ``pop(key, None)``) and matches the migration pattern across
        all 6 framework Request sites.
        """
        try:
            result, error, timed_out = await request.wait_for_result_async()
            if error:
                raise Exception(f"Request {request.id} failed: {request.result}")
            yield result
        finally:
            self.requests.pop(request.id, None)

    @contextlib.contextmanager
    def request_context_sync(self, request: Request):
        """Sync context manager to handle requests.

        B-073 Session 2 Step 3: replaced the ``run_coroutine_threadsafe``
        bridge to ``set_collected`` with a direct sync ``pop``. The pop
        is GIL-atomic so it's safe to call from a worker thread without
        a loop-bridge — Python dict ``pop`` is implemented as a single
        bytecode op. ``_pop_plugin_under_lock`` snapshots iteration so
        concurrent eviction never trips dict-mutation-during-iteration.
        """
        try:
            result = request.get_result_sync()
            if request.error:
                raise Exception(f"Request failed: {request.result}")
            yield result
        finally:
            self.requests.pop(request.id, None)

    @async_log_errors
    async def create_request(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[
            str, list, None
        ] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Request:
        """Create a new request asynchronously."""

        if author_host is None:
            author_host = self.hostname

        request = Request(
            author_host,
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            request_id,
            self.main_event_loop,
        )

        async with self.request_lock:
            self.requests[request.id] = request

        self._logger.debug(
            f"Request {request.id} created by {author} targeting {plugin}.{method}"
        )

        self._spawn_tracked(
            self._process_request(request),
            name=f"request:{plugin}.{method}#{request.id[:8]}",
        )

        return request

    @log_errors
    def create_request_sync(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[
            str, list, None
        ] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Request:
        """Create a new request synchronously."""
        coro = self.create_request(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            author_host,
            request_id,
        )
        future = asyncio.run_coroutine_threadsafe(coro, self.main_event_loop)
        return future.result()

    @async_log_errors
    async def create_gen_request(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[
            str, list, None
        ] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> GeneratorRequest:
        """Create a new request asynchronously."""

        if author_host is None:
            author_host = self.hostname

        request = GeneratorRequest(
            author_host,
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            request_id,
            self.main_event_loop,
        )

        async with self.request_lock:
            self.requests[request.id] = request

        self._logger.debug(
            f"GeneratorRequest {request.id} created by {author} targeting {plugin}.{method}"
        )

        task = self._spawn_tracked(
            self._process_request_stream(request),
            name=f"request_stream:{plugin}.{method}#{request.id[:8]}",
        )
        request._producer_task = task  # B-002: enable cancel-on-collect

        return request

    @log_errors
    def create_gen_request_sync(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[
            str, list, None
        ] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Request:
        """Create a new request synchronously."""
        coro = self.create_gen_request(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            author_host,
            request_id,
        )
        future = asyncio.run_coroutine_threadsafe(coro, self.main_event_loop)
        return future.result()

    @async_log_errors
    async def find_endpoints_by_tag(self, tag: str) -> Optional[List[Dict[str, Any]]]:
        """
        Finds all endpoints by a tag.

        Args:
            tag: The tag to search for

        Returns:
            List of endpoints
            For local: (Plugin, endpoint, endpoint_description, endpoint_arguments)
            For remote: (RemotePlugin, endpoint, endpoint_description, endpoint_arguments)
        """
        endpoints = []
        for plugin in self.plugins.values():
            plugin: Plugin
            if plugin.enabled:
                for endpoint in plugin.endpoints.values():
                    if tag in (endpoint.get("tags") or []):
                        endpoints.append(
                            (
                                plugin,
                                endpoint,
                                endpoint.get("description"),
                                endpoint.get("arguments"),
                            )
                        )

        # Snapshot self.network once. Per Commit 2b cycle 3 HIGH-A:
        # during a hot-reload rebuild, self.network is set to None for
        # the entire rebuild duration; per cycle 2 MED-B: a mid-block
        # swap would otherwise leak calls onto a stopped NM. Both
        # conditions resolve cleanly here — None falls through to
        # return the local-only endpoints list (existing no-match path
        # for "no remote nodes available").
        nm = self.network
        if (
            self.networking_enabled
            and nm is not None
            and getattr(nm, "is_ready", False)
        ):
            for node in nm.nodes:
                node: Node
                if node.enabled:
                    result = await nm.node_get_tagged_endpoints(node.IP, tag)
                    if result:
                        endpoints.extend(result)
        return endpoints

    @async_log_errors
    async def find_endpoint(
        self,
        access_name: str,
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[
            str, list, None
        ] = None,  # blocked hosts (str keyword, list, or None)
        plugin_uuid: Optional[str] = None,
        requester_id: Optional[str] = None,
        target_plugin: Optional[str] = None,
    ) -> tuple[
        Optional[Union[Plugin, RemotePlugin]],
        Optional[dict],
        Optional[Node],
    ]:
        """
        Finds a plugin endpoint locally or on remote nodes with access control.

        Args:
            access_name: The access_name of the endpoint to find
            hosts: Target hosts — "local", "remote", "any", a hostname, or a
                list of hostnames (whitelist). Caller is expected to have
                already passed this through _validate_host_args.
            blocked_hosts: Hosts to exclude — same shape as `hosts`, or None
                for no blocking.
            plugin_uuid: Specific plugin UUID to search for (optional)
            requester_id: UUID of the plugin making the request (for access control)
            target_plugin: Optional plugin name filter

        Returns:
            A 3-tuple. On a found endpoint:
                Local:  (Plugin, endpoint_dict, None)
                Remote: (RemotePlugin, endpoint_dict, Node)
            On a miss: (None, None, None) — NOT bare None. Callers must
            unpack-then-check the first element rather than
            `if result is None`. Stage M (B-048): annotation reads accurately;
            behavior unchanged (every callsite already uses unpack-then-check
            after PR3 Stage B cycle 5 fixed the one mismatched caller in
            request_event_stream).
        """

        self._logger.debug(
            f"Finding endpoint: access_name='{access_name}', hosts={hosts!r}, "
            f"blocked_hosts={blocked_hosts!r}, plugin_uuid={plugin_uuid}, "
            f"requester_id={requester_id}, target_plugin={target_plugin}"
        )

        # Determine request provenance
        # This is used to determine if a REMOTE NODE is checking OUR endpoints
        # (handled by _handle_has_endpoint on the remote node)
        is_local_system = requester_id == self.hostname
        is_local_plugin = requester_id in self.plugins_by_uuid
        is_remote_request = not (is_local_system or is_local_plugin)

        if is_remote_request:
            self._logger.debug(
                f"Remote request detected from requester_id: {requester_id}"
            )

        def _matches_local():
            if isinstance(hosts, str):
                return hosts in ("local", "any", self.hostname)
            if isinstance(hosts, list):
                return "local" in hosts or self.hostname in hosts
            return False

        def _is_local_blocked():
            if blocked_hosts is None:
                return False
            if isinstance(blocked_hosts, str):
                return blocked_hosts in ("local", "any", self.hostname)
            if isinstance(blocked_hosts, list):
                return "local" in blocked_hosts or self.hostname in blocked_hosts
            return False

        # Check locally first (only if host includes local)
        # Note: Remote accessibility checks for OUR endpoints should only happen
        # when a remote node is querying us (is_remote_request=True).
        # When WE are searching for remote endpoints, we don't check remote accessibility here.
        if _matches_local() and not _is_local_blocked():
            for plugin in self.plugins.values():
                # Skip if plugin doesn't match UUID filter
                if plugin_uuid and plugin.plugin_uuid != plugin_uuid:
                    continue

                if target_plugin and plugin.plugin_name != target_plugin:
                    continue

                # Check if plugin is enabled and has endpoints
                if not (plugin.enabled and hasattr(plugin, "endpoints")):
                    continue

                # Fast-path by access_name
                endpoint = getattr(plugin, "_endpoint_by_access", {}).get(access_name)
                if endpoint:
                    # Check access based on request type
                    if is_remote_request:
                        # A remote node is checking OUR endpoints - check remote accessibility
                        # This is the ONLY place where we check remote accessibility
                        if not getattr(plugin, "remote", False):
                            continue
                        if not endpoint.get("remote", False):
                            continue
                        return plugin, endpoint, None
                    else:
                        # Local request: check accessible_by_other_plugins flag
                        if (
                            not endpoint.get("accessible_by_other_plugins", False)
                            and plugin.plugin_uuid != requester_id
                        ):
                            pass
                        else:
                            return plugin, endpoint, None

        def _matches_remote_node(node_hostname):
            if isinstance(hosts, str):
                return hosts in ("remote", "any") or hosts == node_hostname
            if isinstance(hosts, list):
                return node_hostname in hosts
            return False

        def _is_remote_node_blocked(node_hostname):
            if blocked_hosts is None:
                return False
            if isinstance(blocked_hosts, str):
                return (
                    blocked_hosts in ("remote", "any") or blocked_hosts == node_hostname
                )
            if isinstance(blocked_hosts, list):
                return node_hostname in blocked_hosts
            return False

        def _other_than_local():
            if isinstance(hosts, str):
                return not hosts in ("local", self.hostname)
            if isinstance(hosts, list):
                return (
                    len(hosts) - hosts.count("local") - hosts.count(self.hostname)
                ) > 0
            return True

        # Check remote nodes if networking is enabled
        # Snapshot nm once. Per Commit 2b cycle 3 HIGH-A: during a
        # hot-reload rebuild, self.network = None for the entire
        # rebuild duration. cycle 2 MED-B: snapshot prevents mid-block
        # swap from leaking calls onto a stopped NM. None falls through
        # to the bottom `return None, None, None` no-match path.
        nm = self.network
        if (
            getattr(self, "networking_enabled", False)
            and _other_than_local()
            and nm is not None
            and getattr(nm, "is_ready", False)
        ):

            for node in nm.nodes:
                if not (node.enabled and await node.is_alive()):
                    continue

                if not _matches_remote_node(node.hostname) or _is_remote_node_blocked(
                    node.hostname
                ):
                    continue

                # Check remote node for endpoint
                result = await nm.node_has_endpoint(
                    node.IP,
                    access_name,
                    plugin_uuid if plugin_uuid != "remote" else None,
                    requester_id,
                    target_plugin,
                )

                if result and result.get("available", False):
                    # Create RemotePlugin from response data
                    plugin_info = result.get("plugin_info", {})
                    endpoint_info = result.get("endpoint", {})

                    remote_plugin = RemotePlugin(
                        name=plugin_info.get("name", "unknown"),
                        version=plugin_info.get("version", "unknown"),
                        uuid=plugin_info.get("uuid"),
                        enabled=True,
                        remote=True,
                        description=plugin_info.get("description", "Remote plugin"),
                        arguments=[],
                        hostname=result.get("hostname", node.hostname),
                    )

                    return remote_plugin, endpoint_info, node

        return None, None, None

    async def is_requester_allowed(self, requester_id: str) -> bool:
        """
        Checks if a requester plugin exists and is allowed to access endpoints.

        Args:
            requester_id: UUID of the plugin making the request

        Returns:
            True if the requester is allowed, False otherwise
        """
        # Check if requester exists locally
        requester_plugin = self.plugins_by_uuid.get(requester_id)
        if not requester_plugin:
            return False

        # Check if requester is enabled
        if not requester_plugin.enabled:
            return False

        # Add additional access control logic here if needed
        # For example, check if the requester has permission to access remote endpoints

        return True

    @async_handle_errors(None)
    async def _process_request(self, request: Request) -> None:
        """Process a request by invoking the target plugin method.

        B-073 Session 2 Step 2: ``finally`` block evicts the Request
        from ``self.requests`` on every completion path (success, error,
        cancellation). Replaces the 10s polling reap performed by the
        ``cleanup_requests`` maintenance loop (killed in Step 4 — both
        mechanisms run idempotently in the meantime). The pop is sync
        + GIL-atomic; ``_pop_plugin_under_lock`` snapshots
        ``self.requests`` under the lock so concurrent eviction never
        triggers ``RuntimeError: dictionary changed size during iteration``.
        """
        try:
            plugin_name = request.target_plugin
            function_name = request.target_method

            # B-073 Step 8 emit: request started.
            # Phase 2a (B-080-adjacent): ``kind`` carries request.kind so
            # observers can distinguish execute / publish_event fan-out /
            # request_event fan-out children without correlating against
            # _core/event/* topics. Backward-compatible — observers ignore
            # unknown keys per the (topic, payload) contract.
            self._internal_emit(
                "_core/request/started",
                request_id=request.id,
                plugin=plugin_name,
                method=function_name,
                author=request.author,
                kind=request.kind,
                ts=time.time(),
            )

            # PR3 Stage A: prefer request.requester_id (set by Stage B
            # fan-out to sub OWNER's plugin_uuid per C18) over
            # author_id. None on execute-path Requests → falls back to
            # author_id, preserving find_endpoint's existing access
            # check semantics.
            requester = request.requester_id or request.author_id
            plugin, endpoint, node = await self.find_endpoint(
                request.target_method,
                request.target_hosts,
                request.blocked_hosts,
                request.target_plugin_uuid,
                requester,
                request.target_plugin,
            )

            if not plugin:
                await self._set_request_result(
                    request, f"Endpoint {function_name} not found", True
                )
                return

            # Stage O: readiness gate. Skip for remote plugins (no ready
            # field) and for self-calls (Q23 — a plugin's own on_enable
            # publishing to its own subscriber must not deadlock against
            # its own _lifecycle_ready).
            if isinstance(plugin, Plugin) and plugin.plugin_uuid != requester:
                try:
                    await self._wait_for_plugin_ready(plugin)
                except asyncio.TimeoutError:
                    timeout = getattr(
                        self,
                        "plugin_ready_timeout",
                        DEFAULT_PLUGIN_READY_TIMEOUT,
                    )
                    await self._set_request_result(
                        request,
                        f"Plugin {plugin.plugin_name!r} not ready within "
                        f"{timeout}s",
                        True,
                    )
                    return

            host_label = (
                f"(local) {self.hostname}"
                if isinstance(plugin, Plugin)
                else f"{node.IP}#{node.hostname}"
            )
            self._logger.debug(
                f"Found {plugin_name} (ID: {plugin.plugin_uuid}) for Request with ID {request.id} on host {host_label}"
            )

            if isinstance(
                plugin, RemotePlugin
            ):  # NOTE: Fix the timeout thing. Warn if ping is higher than timeout
                # Stamp _is_remote BEFORE the network check so the
                # rebuild drain (Step 7) catches in-flight remote
                # requests even when self.network transitions to None
                # mid-await. Per cycle 1 HIGH-1 — drain filter uses
                # this attribute to distinguish remote-bound requests
                # from local execute path requests.
                request._is_remote = True
                # Snapshot nm. Per Commit 2b cycle 3 HIGH-A: during a
                # hot-reload rebuild, self.network is None for the
                # entire rebuild duration. cycle 3 HIGH-β requires a
                # distinct fail-fast semantic here (not silent return)
                # so the caller's future resolves with a clear error
                # rather than hanging forever. Also gate on
                # ``is_ready=False`` to cover the post-rebuild window
                # where NM has been assigned but its ``start()`` task
                # hasn't completed yet (cycle 6 fresh-eyes MED).
                nm = self.network
                if nm is None or not getattr(nm, "is_ready", False):
                    await self._set_request_result(
                        request,
                        "Network unavailable mid-rebuild",
                        True,
                    )
                    return
                result = await nm.execute_remote(
                    IP=node.IP,
                    plugin=plugin_name,
                    method=function_name,
                    args=request.args,
                    plugin_uuid=request.target_plugin_uuid,
                    author=f"{self.hostname} - {request.author}#{request.author_id}",
                    author_id=request.author_id,
                    timeout=(request.timeout_duration, request.created_at),
                    request_id=request.id,
                )

            else:
                # PR2: internal_name is optional in plugin_config; defaults to
                # access_name (the dict key, also the request target_method).
                internal_name = endpoint.get("internal_name") or function_name
                func = getattr(plugin, internal_name, None)
                if not callable(func):
                    await self._set_request_result(
                        request,
                        f"Function {function_name}({internal_name}) not found in plugin {plugin_name}",
                        True,
                    )
                    return

                if inspect.isasyncgenfunction(func) or inspect.isgeneratorfunction(
                    func
                ):
                    await self._set_request_result(
                        request,
                        f"For Request {request.id}: The method you requested is a generator. Use execute_stream for generators",
                        True,
                    )
                    return

                try:
                    chain = getattr(request, "_call_chain", ())
                    result = await self._call_endpoint(
                        func, request.args, chain, request=request
                    )
                except Exception as e:
                    await self._set_request_result(request, str(e), True)
                    return

            await self._set_request_result(request, result)

        except Exception as e:
            # Safety net: if anything above failed without resolving the future,
            # resolve it now so the caller doesn't hang forever
            if not request._future.done():
                await self._set_request_result(
                    request, f"Unhandled error processing request: {e}", True
                )
        finally:
            # B-073 Step 8 emit: request completed. Observer-presence
            # gate skips the future-state read + payload assembly when
            # nothing is listening — the production-default no-observer
            # path saves ~500ns-1µs per request. The pop below stays
            # outside the gate (eviction is unconditional). Cached
            # ``now`` shared between latency calc + ts payload.
            if "_core/request/completed" in self._internal_observers:
                # Defensive future-state read — cancelled future raises
                # on .result(); guard explicitly. Latency clamped to
                # 0.0 against clock jumps.
                if request._future.done() and not request._future.cancelled():
                    try:
                        _, _err_flag, _ = request._future.result()
                        errored = bool(_err_flag)
                    except Exception:
                        errored = True  # future yielded an exception
                else:
                    errored = request._future.cancelled()
                now = time.time()
                # Phase 2a: kind=request.kind for symmetry with /started.
                self._internal_emit(
                    "_core/request/completed",
                    request_id=request.id,
                    latency=max(0.0, now - request.created_at),
                    error=errored,
                    kind=request.kind,
                    ts=now,
                )

            # B-073 Session 2 Step 2: done-callback eviction. Pop the
            # Request entry from ``self.requests`` on every completion
            # path (success, exception, cancellation). Replaces the
            # ``cleanup_requests`` polling reap (killed in Step 4).
            # ``pop(key, None)`` is GIL-atomic and idempotent — safe
            # under cancel mid-finally.
            self.requests.pop(request.id, None)

    async def _call_endpoint(
        self,
        func: Callable,
        args: Any,
        call_chain: tuple = (),
        request: Optional[Request] = None,
    ) -> Any:
        """Call endpoint function handling sync/async and arg shapes.

        PR3 Stage A: when ``request`` is provided AND ``request.kind`` is
        an event kind (``"publish_event"`` / ``"request_event"``), the
        handler receives a single positional ``Event`` argument instead of
        the unpacked ``args`` shape used by the execute path. Sync event
        handlers are dispatched on the dedicated SyncDispatcher executor
        via ``run_in_executor`` (Q17 + C3), NOT the shared
        ``_plugin_executor``. The execute path (``request is None`` OR
        ``request.kind == "execute"``) takes ZERO new code paths.
        """
        # PR3 Stage A: kind-aware Event branch. Only fires for event
        # kinds; the execute path (kind="execute" or no request) falls
        # through to the original implementation untouched.
        if request is not None and request.kind in (
            "publish_event",
            "request_event",
        ):
            event = Event.from_request(request)

            if asyncio.iscoroutinefunction(func):
                # Async handlers awaited directly on the main event loop.
                return await func(event)

            # Sync event handlers run on the dedicated SyncDispatcher
            # executor (Q17 + C3 + C8). run_in_executor pattern — NOT
            # submit + done_callback — so the awaiting fan-out task is
            # naturally long-lived and integrates with task_list / the
            # 30s shutdown drain.
            def _tracked_event(ev):
                _sync_call_chain.chain = call_chain
                try:
                    return func(ev)
                finally:
                    _sync_call_chain.chain = ()

            return await self.main_event_loop.run_in_executor(
                self.sync_dispatcher.executor, _tracked_event, event
            )

        # Execute path — unchanged from PR2 behavior.
        if asyncio.iscoroutinefunction(func):
            if isinstance(args, tuple):
                return await func(*args)
            if isinstance(args, dict):
                return await func(**args)
            if args is None:
                return await func()
            return await func(args)

        # sync function -> threadpool, propagate call chain for cycle detection
        def _tracked(*a, **kw):
            _sync_call_chain.chain = call_chain
            try:
                return func(*a, **kw)
            finally:
                _sync_call_chain.chain = ()

        if isinstance(args, tuple):
            return await self.main_event_loop.run_in_executor(
                self._plugin_executor, _tracked, *args
            )
        if isinstance(args, dict):
            return await self.main_event_loop.run_in_executor(
                self._plugin_executor, functools.partial(_tracked, **args)
            )
        if args is None:
            return await self.main_event_loop.run_in_executor(
                self._plugin_executor, _tracked
            )
        return await self.main_event_loop.run_in_executor(
            self._plugin_executor, _tracked, args
        )

    # @async_handle_errors(None)
    async def _process_request_stream(self, request: GeneratorRequest) -> None:
        """Process a request by invoking the target plugin method.

        B-073 Session 2 Step 2: ``finally`` block evicts the
        GeneratorRequest from ``self.requests`` on every completion path
        (success, error, cancellation). Symmetric with ``_process_request``
        for non-stream Requests. Replaces the ``cleanup_requests``
        polling reap (killed in Step 4). Sync GIL-atomic ``pop``;
        ``_pop_plugin_under_lock`` snapshots iteration so concurrent
        eviction never trips dict-mutation-during-iteration.

        ``GeneratorRequest.set_collected`` (utils.py B-002 logic)
        continues to be called by the consumer side — it cancels the
        producer task + pushes EndOfQueue sentinel, distinct from
        eviction. Both fire on completion: producer-side finally pops
        from ``self.requests``; consumer-side ``set_collected`` handles
        producer-task lifecycle. Symmetric, single-source-of-truth per
        concern.
        """
        try:
            plugin_name = request.target_plugin
            function_name = request.target_method

            # PR3 Stage A: same requester_id-aware lookup as
            # _process_request (C18). None on execute-path Requests →
            # falls back to author_id.
            requester = request.requester_id or request.author_id
            plugin, endpoint, node = await self.find_endpoint(
                request.target_method,
                request.target_hosts,
                request.blocked_hosts,
                request.target_plugin_uuid,
                requester,
                request.target_plugin,
            )

            if not plugin:
                await self._set_gen_request_result(
                    request, f"Endpoint {function_name} not found", True
                )
                return

            # Stage O: readiness gate (stream variant). Same skip rules
            # as _process_request — remote plugins and self-calls bypass.
            if isinstance(plugin, Plugin) and plugin.plugin_uuid != requester:
                try:
                    await self._wait_for_plugin_ready(plugin)
                except asyncio.TimeoutError:
                    timeout = getattr(
                        self,
                        "plugin_ready_timeout",
                        DEFAULT_PLUGIN_READY_TIMEOUT,
                    )
                    await self._set_gen_request_result(
                        request,
                        f"Plugin {plugin.plugin_name!r} not ready within "
                        f"{timeout}s",
                        True,
                    )
                    return

            host_label = (
                f"(local) {self.hostname}"
                if isinstance(plugin, Plugin)
                else f"{node.IP}#{node.hostname}"
            )
            self._logger.debug(
                f"Found {plugin_name} (ID: {plugin.plugin_uuid}) for Request with ID {request.id} on host {host_label}"
            )

            if isinstance(plugin, RemotePlugin):
                # Stamp _is_remote BEFORE the network check (cycle 1
                # HIGH-1) so the rebuild drain catches in-flight
                # streaming-remote requests via the filter. Symmetry
                # with the non-stream path's stamp.
                request._is_remote = True
                # Snapshot nm. Per Commit 2b cycle 3 HIGH-A + HIGH-β:
                # mid-rebuild self.network is None; resolve the
                # generator request with an error so the consumer sees
                # a clean RequestException via B-044 chain rather than
                # hanging on an empty queue. Also gate on
                # ``is_ready=False`` to cover the post-rebuild window
                # where NM has been assigned but ``start()`` hasn't
                # completed (cycle 6 fresh-eyes MED — symmetry with
                # _process_request guard).
                nm = self.network
                if nm is None or not getattr(nm, "is_ready", False):
                    await self._set_gen_request_result(
                        request,
                        "Network unavailable mid-rebuild",
                        True,
                    )
                    return
                async for result in nm.execute_remote_stream(
                    IP=node.IP,
                    plugin=plugin_name,
                    method=function_name,
                    args=request.args,
                    plugin_uuid=request.target_plugin_uuid,
                    author=f"{self.hostname} - {request.author}#{request.author_id}",
                    author_id=request.author_id,
                    timeout=(request.timeout_duration, request.created_at),
                    request_id=request.id,
                ):
                    await request.queue.put((result, False, False))

            else:
                # if not plugin.remote and request.author_id:
                #    await self._set_request_result(request, NetworkRequestException(f"Plugin {plugin_name} is not accessable anymore"), True)
                #    return
                # PR2: internal_name is optional in plugin_config; defaults to
                # access_name (the dict key, also the request target_method).
                internal_name = endpoint.get("internal_name") or function_name
                func = getattr(plugin, internal_name, None)
                if not callable(func):  # or not inspect.isfunction(func):
                    # await request.queue.put((result, False, False))
                    await self._set_gen_request_result(
                        request,
                        f"Function {function_name}({internal_name}) not found in plugin {plugin_name}",
                        True,
                    )
                    return

                if asyncio.iscoroutinefunction(func):
                    await self._set_gen_request_result(
                        request,
                        f"For Request {request.id}: The method you requested is a non-generator async function. Use execute for non-generators",
                        True,
                    )
                    return

                elif inspect.isasyncgenfunction(func):
                    if isinstance(request.args, tuple):
                        async for result in func(*request.args):
                            await request.queue.put((result, False, False))
                    elif isinstance(request.args, dict):
                        async for result in func(**request.args):
                            await request.queue.put((result, False, False))
                    elif request.args is None:
                        async for result in func():
                            await request.queue.put((result, False, False))
                    else:
                        async for result in func(request.args):
                            await request.queue.put((result, False, False))

                elif inspect.isgeneratorfunction(func):
                    if isinstance(request.args, tuple):
                        generator = func(*request.args)
                    elif isinstance(request.args, dict):
                        generator = func(**request.args)
                    elif request.args is None:
                        generator = func()
                    else:
                        generator = func(request.args)

                    # B-041 fix: thread the caller's sync call chain
                    # into the threadpool worker before each next() so
                    # cycle detection works for sync generators that
                    # call execute_sync internally. The chain comes
                    # from request._call_chain (stamped by
                    # execute_stream_sync); falls back to () for
                    # async-loop callers that bypass the sync wrapper.
                    #
                    # Switched from asyncio.to_thread (default loop
                    # executor) to self._plugin_executor — the
                    # framework's dedicated pool for sync ENDPOINT
                    # methods, matching _call_endpoint's sync branch.
                    # The default pool was a pre-existing inconsistency:
                    # sync gen endpoints competed with framework-
                    # internal default-pool work and risked starvation.
                    # NOT sync_dispatcher.executor — that's for sync
                    # SUBSCRIBER handlers (Q17 + C3) and used by
                    # request_event_stream, not endpoint dispatch.
                    chain = getattr(request, "_call_chain", ())

                    def _next_with_chain(g, sent, ch):
                        _sync_call_chain.chain = ch
                        try:
                            return next(g, sent)
                        finally:
                            _sync_call_chain.chain = ()

                    sentinel = object()
                    while True:
                        result = await self.main_event_loop.run_in_executor(
                            self._plugin_executor,
                            _next_with_chain,
                            generator,
                            sentinel,
                            chain,
                        )
                        if result is sentinel:
                            break
                        await request.queue.put(
                            (result, False, False)
                        )  # FIXME Add in utils

                else:
                    await self._set_gen_request_result(
                        request,
                        f"For Request {request.id}: The method you requested is a non-generator sync function. Use execute for non-generators",
                        True,
                    )
                    return
                    # result = await self.main_event_loop.run_in_executor(self._plugin_executor, func, request.args)

            await self._set_gen_request_result(request)

        except Exception as e:
            # Safety net: resolve the future so consumers don't hang forever
            if not request._future.done():
                await self._set_gen_request_result(
                    request, f"Unhandled error processing stream request: {e}", True
                )
        finally:
            # B-073 Session 2 Step 2: done-callback eviction. Symmetric
            # with ``_process_request``'s finally — pop on any completion
            # path. Sync, GIL-atomic, idempotent.
            self.requests.pop(request.id, None)

    async def _process_request_event_stream(
        self,
        request: GeneratorRequest,
        target_plugin: Plugin,
        endpoint: dict,
        event_meta: Event,
        timeout: Optional[float] = None,
        caller_chain: Optional[tuple] = None,
        verbose_notifier: bool = False,
    ) -> None:
        """Producer for request_event_stream LOCAL fan-out (B-054 fix).

        Mirrors _process_request_stream but for the topic-based
        streaming path. Iterates the handler (async or sync generator),
        wraps the first chunk in Event metadata (LOCKED I), and pushes
        chunks into request.queue. The consumer side
        (request_event_stream) reads from request.get_queue_stream()
        and yields to the caller.

        Spawned via _spawn_tracked, so close()'s 30s drain catches
        in-flight streams and pop_plugin's pending-request walk can
        fail the GeneratorRequest entry.

        timeout is passed as a parameter (NOT read from
        request.timeout_duration which is intentionally None to disable
        get_queue_stream's redundant consumer-side timeout enforcement
        — see consumer-site comment in request_event_stream).

        B-073 Session 2 Step 2: ``finally`` block evicts the
        GeneratorRequest from ``self.requests`` on every completion path
        (success, RequestException, generic Exception). Symmetric with
        ``_process_request`` and ``_process_request_stream``. Sync
        GIL-atomic ``pop``.
        """
        try:
            internal = endpoint.get("internal_name") or request.target_method
            func = getattr(target_plugin, internal, None)
            if func is None or not (
                inspect.isasyncgenfunction(func) or inspect.isgeneratorfunction(func)
            ):
                # Defense-in-depth — request_event_stream's consumer
                # body checks this before spawning, so this branch is
                # normally unreachable.
                await self._set_gen_request_result(
                    request,
                    "request_event_stream: handler is not a generator function",
                    True,
                )
                return

            # Q8: timeout = whole-stream budget. Tracked via per-chunk
            # asyncio.wait_for with the residual deadline.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout if timeout is not None else None
            # B-074 Step 10: producer-side first-chunk timing baseline.
            producer_t0 = loop.time()

            def _residual() -> Optional[float]:
                if deadline is None:
                    return None
                rem = deadline - loop.time()
                if rem <= 0:
                    raise RequestException(
                        f"request_event_stream timed out after "
                        f"{timeout}s (whole-stream budget per Q8)"
                    )
                return rem

            first = True

            if inspect.isasyncgenfunction(func):
                ait = func(event_meta).__aiter__()
                try:
                    while True:
                        rem = _residual()
                        try:
                            if rem is None:
                                chunk = await ait.__anext__()
                            else:
                                chunk = await asyncio.wait_for(
                                    ait.__anext__(), timeout=rem
                                )
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError as e:
                            raise RequestException(
                                f"request_event_stream timed out after "
                                f"{timeout}s (whole-stream budget per Q8)"
                            ) from e
                        if first:
                            first = False
                            wrapped = Event(
                                topic=event_meta.topic,
                                payload=chunk,
                                author=event_meta.author,
                                author_id=event_meta.author_id,
                                author_host=event_meta.author_host,
                                subscription_id=event_meta.subscription_id,
                                timestamp=event_meta.timestamp,
                            )
                            # B-073 Step 8 emit: event streamed first chunk.
                            self._internal_emit(
                                "_core/event/streamed",
                                publisher=event_meta.author,
                                topic=event_meta.topic,
                                phase="first_chunk",
                                ts=time.time(),
                            )
                            # B-074 Step 10 verbose log: first chunk timing.
                            if verbose_notifier:
                                self._logger.debug(
                                    "request_event_stream topic=%r first chunk "
                                    "yielded after %.3fs",
                                    event_meta.topic,
                                    loop.time() - producer_t0,
                                )
                            await request.queue.put((wrapped, False, False))
                        else:
                            await request.queue.put((chunk, False, False))
                finally:
                    with contextlib.suppress(Exception):
                        await ait.aclose()
            else:
                # Sync generator branch — chain propagation +
                # sync_dispatcher.executor (per Q17 + C3, this IS
                # subscriber dispatch, so use the dedicated subscriber
                # pool — NOT _plugin_executor like _process_request_stream
                # uses for sync endpoints).
                sentinel = object()
                gen = func(event_meta)
                # Mirrors current inline code (core.py:4780-4784):
                # request_event_stream_sync passes non-None caller_chain;
                # Plugin.request_event_stream (utils.py:1572) does NOT,
                # so async callers leave it as None — fallback reads
                # the loop thread's threadlocal (always () in current
                # code, kept defensively for forward-compat).
                stream_chain = (
                    caller_chain
                    if caller_chain is not None
                    else getattr(_sync_call_chain, "chain", ())
                )

                def _next_with_chain(g, sent, ch):
                    _sync_call_chain.chain = ch
                    try:
                        return next(g, sent)
                    finally:
                        _sync_call_chain.chain = ()

                try:
                    while True:
                        rem = _residual()
                        fut = loop.run_in_executor(
                            self.sync_dispatcher.executor,
                            _next_with_chain,
                            gen,
                            sentinel,
                            stream_chain,
                        )
                        try:
                            if rem is None:
                                chunk = await fut
                            else:
                                chunk = await asyncio.wait_for(fut, timeout=rem)
                        except asyncio.TimeoutError as e:
                            raise RequestException(
                                f"request_event_stream timed out after "
                                f"{timeout}s (whole-stream budget per Q8)"
                            ) from e
                        if chunk is sentinel:
                            break
                        if first:
                            first = False
                            wrapped = Event(
                                topic=event_meta.topic,
                                payload=chunk,
                                author=event_meta.author,
                                author_id=event_meta.author_id,
                                author_host=event_meta.author_host,
                                subscription_id=event_meta.subscription_id,
                                timestamp=event_meta.timestamp,
                            )
                            # B-073 Step 8 emit: event streamed first chunk.
                            self._internal_emit(
                                "_core/event/streamed",
                                publisher=event_meta.author,
                                topic=event_meta.topic,
                                phase="first_chunk",
                                ts=time.time(),
                            )
                            # B-074 Step 10 verbose log: first chunk timing.
                            if verbose_notifier:
                                self._logger.debug(
                                    "request_event_stream topic=%r first chunk "
                                    "yielded after %.3fs",
                                    event_meta.topic,
                                    loop.time() - producer_t0,
                                )
                            await request.queue.put((wrapped, False, False))
                        else:
                            await request.queue.put((chunk, False, False))
                finally:
                    with contextlib.suppress(Exception):
                        gen.close()

            # Normal completion — push EndOfQueue terminator + resolve future.
            await self._set_gen_request_result(request)
        except RequestException as e:
            if not request._future.done():
                await self._set_gen_request_result(request, str(e), True)
        except Exception as e:
            if not request._future.done():
                await self._set_gen_request_result(
                    request,
                    f"Unhandled error in request_event_stream producer: {e}",
                    True,
                )
        finally:
            # B-073 Session 2 Step 2: done-callback eviction. Symmetric
            # with ``_process_request`` and ``_process_request_stream``
            # finally blocks — pop the GeneratorRequest from
            # ``self.requests`` on every completion path. Sync, GIL-atomic,
            # idempotent.
            self.requests.pop(request.id, None)

    @async_handle_errors(None)
    async def _set_request_result(
        self, request: Request, result: Any, error: bool = False
    ) -> None:
        """Set the result of a request."""
        if isinstance(result, asyncio.Future):
            try:
                result = await result
            except Exception as e:
                # B-013 fix: a returned Future whose await raises must
                # still resolve the request — otherwise @async_handle_errors
                # swallows here and the caller hangs on request._future.
                # CancelledError (BaseException) propagates uncaught so
                # task cancellation tears down cleanly.
                await request.set_result(f"{type(e).__name__}: {e}", True)
                return
        await request.set_result(result, error)

    @async_handle_errors(None)
    async def _set_gen_request_result(
        self, request: GeneratorRequest, result: Any = None, error: bool = False
    ) -> None:
        """Set the result of a request."""

        await request.set_result(result, error)

    def _spawn_tracked(self, coro, *, name: str) -> asyncio.Task:
        """Create + register a tracked async task (B-047 fix).

        Must be called from the event loop thread — uses
        asyncio.create_task which requires a running loop in the
        current thread. All current call sites are inside async def
        methods that always run on the loop thread; sync entry points
        (create_request_sync, publish_event_sync) bridge via
        run_coroutine_threadsafe so the actual _spawn_tracked call
        still happens on the loop. Calling from a worker thread
        raises RuntimeError("no running event loop").

        Returns the task. Callers should hold the returned reference
        if they need it (e.g. request._producer_task = task); the
        framework already holds a strong reference via self.task_list
        so the task will not be GC'd mid-flight (per asyncio docs:
        save a strong reference to created tasks).

        The done_callback evicts from self.task_list on completion
        (any terminal state — normal return, raise, cancel),
        increments tasks_completed_total, and appends a record to
        self.recent_completed (bounded deque). cancelled() is
        checked BEFORE exception() because exception() raises
        CancelledError on cancelled tasks. The whole introspection
        block is wrapped in try/except so a pathological failure
        cannot break asyncio's internal callback dispatch.
        """
        task = asyncio.create_task(coro, name=name)
        self.task_list.add(task)
        self.tasks_started_total += 1
        started = time.monotonic()

        def _on_done(t: asyncio.Task, _name=name, _started=started) -> None:
            self.task_list.discard(t)
            self.tasks_completed_total += 1
            err: Optional[str] = None
            try:
                if t.cancelled():
                    err = "cancelled"
                else:
                    exc = t.exception()
                    if exc is not None:
                        err = f"{type(exc).__name__}: {exc}"
            except Exception:
                err = "introspection_failed"
            self.recent_completed.append(
                {
                    "name": _name,
                    "started": _started,
                    "completed": time.monotonic(),
                    "error": err,
                }
            )

        task.add_done_callback(_on_done)
        return task

    # B-073 Session 2 Step 4: ``running_loop`` + ``cleanup_requests``
    # removed. Pre-Step-2 the maintenance loop ticked every
    # ``cleanup_request_interval`` seconds and reaped Request entries
    # whose ``collected`` flag was set. Steps 2+3 replaced the polling
    # reap with done-callback eviction at all 7 framework Request sites
    # (3 producer-finally pops in ``_process_request*`` + 6 outer-finally
    # pops at ``execute``/``request_event``/etc.). The maintenance loop
    # has no work to do — eviction is now O(1) at completion time, no
    # sweep needed. ``cleanup_request_interval`` config knob also
    # removed from ``apply_configvalues`` in utils.py.

    def _validate_host_args(self, hosts, blocked_hosts):
        """Normalize hosts/blocked_hosts and warn on redundant combos.

        Returns (hosts, blocked_hosts) ready to pass to find_endpoint.
        Raises ValueError on structural input errors. Idempotent — safe to
        call on already-normalized values.
        """
        hosts = _normalize_hosts(hosts, param_name="hosts", default="local")
        blocked_hosts = _normalize_hosts(
            blocked_hosts,
            param_name="blocked_hosts",
            default=None,
        )
        _warn_redundant_host_combos(hosts, blocked_hosts, self._logger)
        return hosts, blocked_hosts

    # One-liner methods for plugin communication
    # @async_handle_errors(default_return=None)
    # async def execute(self, target: str, args: Any = None, author: str = "system", timeout: Optional[float] = None) -> Any:
    @async_log_errors
    async def execute(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[
            str, list, None
        ] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Any:
        """
        One-liner to execute a plugin method and get its result with built-in error handling.
        This combines request creation, processing, and result retrieval in one method.

        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds

        Returns:
            The result from the plugin method or None if any error occurs
        """
        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)

        if author == "system":
            author = self.hostname
            author_id = self.hostname

        request = await self.create_request(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            author_host,
            request_id,
        )
        try:
            result, error, _ = await request.wait_for_result_async()
            if error:
                self._logger.warning(
                    f"Error executing {plugin}.{method} (Req-ID: {request.id}): {result}. You can check the logs for this Req-ID."
                )
                raise RequestException(result)
            return result
        finally:
            # B-073 Session 2 Step 3: done-callback eviction. Runs on
            # normal return, RequestException, AND CancelledError —
            # without this, a cancelled caller would leave the Request
            # lingering in self.requests forever. Sync, GIL-atomic,
            # idempotent with the producer-side pop in _process_request.
            self.requests.pop(request.id, None)

    @log_errors
    def execute_sync(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[
            str, list, None
        ] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Any:
        """
        Synchronous one-liner to execute a plugin method with built-in error handling.

        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds

        Returns:
            The result from the plugin method or None if any error occurs
        """

        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)

        if author == "system":
            author = self.hostname
            author_id = self.hostname

        # Detect circular sync calls that would deadlock the threadpool
        chain = getattr(_sync_call_chain, "chain", ())
        target = f"{plugin}.{method}"
        if target in chain:
            raise RequestException(
                f"Circular sync call: {' -> '.join(chain)} -> {target}"
            )

        future = asyncio.run_coroutine_threadsafe(
            self._execute_sync_tracked(
                chain + (target,),
                plugin,
                method,
                args,
                plugin_uuid,
                hosts,
                blocked_hosts,
                author,
                author_id,
                timeout,
                author_host,
                request_id,
            ),
            self.main_event_loop,
        )
        return future.result()

    async def _execute_sync_tracked(
        self,
        call_chain,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[
            str, list, None
        ] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Any:
        """Like execute(), but attaches the sync call chain to the request."""
        request = await self.create_request(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            author_host,
            request_id,
        )
        request._call_chain = call_chain
        try:
            result, error, _ = await request.wait_for_result_async()
            if error:
                self._logger.warning(
                    f"Error executing {plugin}.{method} (Req-ID: {request.id}): {result}"
                )
                raise RequestException(result)
            return result
        finally:
            # B-073 Session 2 Step 3: done-callback eviction. Idempotent
            # with the producer-side pop in _process_request.
            self.requests.pop(request.id, None)

    @async_gen_log_errors
    async def execute_stream(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[
            str, list, None
        ] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Any:
        """
        One-liner to execute a plugin method and get its result with built-in error handling.
        This combines request creation, processing, and result retrieval in one method.

        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds

        Returns:
            The result from the plugin method or None if any error occurs
        """

        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)

        if author == "system":
            author = self.hostname
            author_id = self.hostname

        request = await self.create_gen_request(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            author_host,
            request_id,
        )
        try:
            async for result, error, _ in request.get_queue_stream():
                if error:
                    self._logger.warning(
                        f"Error executing {plugin}.{method} (GenReq-ID: {request.id}): {result}. You can check the logs for this Req-ID."
                    )
                    raise RequestException(result)
                yield result
        finally:
            # Mark for cleanup. Runs on normal completion, RequestException,
            # AND CancelledError (e.g. when caller breaks out of `async for`
            # early) — without this, cancelled stream consumers would leave
            # the GeneratorRequest lingering forever.
            await request.set_collected()

    @gen_log_errors
    def execute_stream_sync(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[
            str, list, None
        ] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Any:
        """
        Synchronous one-liner to execute a plugin method with built-in error handling.

        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds

        Returns:
            The result from the plugin method or None if any error occurs
        """

        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)

        if author == "system":
            author = self.hostname
            author_id = self.hostname

        # Detect circular sync calls that would deadlock the threadpool
        chain = getattr(_sync_call_chain, "chain", ())
        target = f"{plugin}.{method}"
        if target in chain:
            raise RequestException(
                f"Circular sync call: {' -> '.join(chain)} -> {target}"
            )

        request = self.create_gen_request_sync(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            author_host,
            request_id,
        )
        # B-041 fix: stamp the caller's sync call chain on the
        # GeneratorRequest so _process_request_stream's sync-gen
        # branch can propagate it to the handler's threadpool worker.
        # Mirrors _execute_sync_tracked's request._call_chain
        # assignment for the non-stream path. Without this, a
        # sync→stream→sync cycle (sync caller calls
        # execute_stream_sync, stream handler is a sync gen that
        # calls execute_sync back into the caller) deadlocks the
        # threadpool with no "Circular sync call" RequestException.
        request._call_chain = chain + (target,)

        try:
            for result, error, _ in request.get_queue_stream_sync():
                if error:
                    self._logger.warning(
                        f"Error executing {plugin}.{method} (GenReq-ID: {request.id}): {result}. You can check the logs for this Req-ID."
                    )
                    raise RequestException(result)
                yield result
        finally:
            asyncio.run_coroutine_threadsafe(
                request.set_collected(), self.main_event_loop
            )

    # ── Notifier system ───────────────────────────────────────────────

    async def subscribe(
        self,
        topic: str,
        plugin_name: str,
        plugin_uuid: str,
        target_plugin: Optional[str] = None,
        target_access_name: Optional[str] = None,
        target_plugin_uuid: Optional[str] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        authors: Union[str, list, None] = None,
        blocked_authors: Union[str, list, None] = None,
        declared_id: Optional[str] = None,
        enabled: bool = True,
    ) -> str:
        """Register a topic subscription. Returns subscription ID.

        B-073 Step 9: validates ``topic`` against the subscription rules
        (rejects empty/wildcard-mid-segment/{var}-templating/leading-``_``
        framework prefix). Closes the bypass that runtime test/tooling
        callers previously used to skip validation.
        """
        _validate_subscription_topic(
            topic, context=f"Plexus.subscribe(plugin={plugin_name!r})"
        )
        return await self.topic_registry.subscribe(
            topic_pattern=topic,
            plugin_name=plugin_name,
            plugin_uuid=plugin_uuid,
            target_plugin=target_plugin,
            target_access_name=target_access_name,
            target_plugin_uuid=target_plugin_uuid,
            hosts=hosts,
            blocked_hosts=blocked_hosts,
            authors=authors,
            blocked_authors=blocked_authors,
            declared_id=declared_id,
            enabled=enabled,
        )

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a topic subscription by ID."""
        return await self.topic_registry.unsubscribe(subscription_id)

    # ── PR3 Stage B: new publish_event / request_event / subscribe API ─

    def _publisher_targets_local(
        self,
        eff_hosts: Union[str, list, None],
        eff_blocked: Union[str, list, None],
    ) -> bool:
        """Decide whether the publisher's hosts/blocked_hosts kwargs
        permit LOCAL fan-out at all (LOCKED IN — PUBLISHER hosts).

        Per spec: ``hosts`` defaults to "local". ``hosts="remote"``
        means peer-only — local subs are skipped. ``hosts=[<list>]``
        excluding "local"/own hostname also skips local. ``blocked_hosts``
        with "local"/own hostname/"any" also skips.

        Returns True if local fan-out should proceed; False to skip.
        """
        # Default per spec: hosts not provided → "local".
        if eff_hosts is None:
            eff_hosts = "local"

        def _hosts_allows_local(val) -> bool:
            if val == "any":
                return True
            if isinstance(val, str):
                return val in ("local", self.hostname)
            if isinstance(val, list):
                return "local" in val or self.hostname in val
            return False

        def _blocked_excludes_local(val) -> bool:
            if val is None:
                return False
            if isinstance(val, str):
                return val in ("local", self.hostname, "any")
            if isinstance(val, list):
                # Match _sub_accepts_local: "any" inside a list is
                # technically invalid per spec but defensively treated
                # as a wildcard block when present (consistent with
                # subscriber-side _sub_accepts_local)._
                return "local" in val or self.hostname in val or "any" in val
            return False

        return _hosts_allows_local(eff_hosts) and not _blocked_excludes_local(
            eff_blocked
        )

    def _sub_accepts_local(self, sub: Subscription) -> bool:
        """Stage B sub-level host filter for LOCAL fan-out.

        Sub's ``hosts``/``blocked_hosts`` interpreted against the
        framework's own hostname. Used by publish_event /
        request_event / request_event_stream alike (LOCKED H).
        """
        sub_hosts = sub.hosts
        sub_blocked = sub.blocked_hosts

        def _hosts_accepts(val) -> bool:
            if val is None or val == "any":
                return True
            if isinstance(val, str):
                return val in ("local", self.hostname)
            if isinstance(val, list):
                return "local" in val or self.hostname in val or "any" in val
            return False

        def _blocked(val) -> bool:
            if val is None:
                return False
            if isinstance(val, str):
                return val in ("local", self.hostname, "any")
            if isinstance(val, list):
                return "local" in val or self.hostname in val or "any" in val
            return False

        return _hosts_accepts(sub_hosts) and not _blocked(sub_blocked)

    def _sub_accepts_remote_publisher(
        self,
        sub,
        author_host: Optional[str],
        author: Optional[str],
    ) -> bool:
        """PR3 Stage C receiver-gate (locked #18 item 1). True iff this
        local sub should receive a publish_event/request_event coming
        from a peer at ``author_host`` published by ``author``.

        Distinct from `_sub_accepts_local` which gates LOCAL fan-out
        against our own hostname. Receiver-gate logic:
          - hosts="local"          → REJECT (sub opted out of remote)
          - hosts="any"/"remote"   → ACCEPT (then check blocked_hosts)
          - hosts=<str>            → ACCEPT iff str==author_host or "any"
          - hosts=[list]           → ACCEPT iff author_host in list, or
                                     "any"/"remote" in list
        blocked_hosts: REJECT iff blocked names author_host, "any", or
        "remote". Authors filter is applied separately via
        `_sub_accepts_author`.
        """
        sub_hosts = getattr(sub, "hosts", None)
        if sub_hosts == "local":
            return False

        if sub_hosts is None or sub_hosts in ("any", "remote"):
            accepts = True
        elif isinstance(sub_hosts, str):
            accepts = (sub_hosts == author_host) or sub_hosts == "any"
        elif isinstance(sub_hosts, list):
            accepts = (
                (author_host is not None and author_host in sub_hosts)
                or "any" in sub_hosts
                or "remote" in sub_hosts
            )
        else:
            return False

        if not accepts:
            return False

        sub_blocked = getattr(sub, "blocked_hosts", None)
        if sub_blocked is None:
            return True
        if isinstance(sub_blocked, str):
            if sub_blocked in ("any", "remote") or sub_blocked == author_host:
                return False
        elif isinstance(sub_blocked, list):
            if (
                "any" in sub_blocked
                or "remote" in sub_blocked
                or (author_host is not None and author_host in sub_blocked)
            ):
                return False

        return True

    def _sub_accepts_author(self, sub: Subscription, author: str) -> bool:
        """Stage B sub-level author filter (LOCKED H + Q4).

        Q4: system-originated events are implicitly trusted — they pass
        any explicit ``authors`` whitelist UNLESS ``blocked_authors``
        explicitly names "system".
        """
        authors = sub.authors
        blocked_authors = sub.blocked_authors

        def _accepts(val) -> bool:
            if val is None:
                return True
            if isinstance(val, str):
                return val == author or val == "any"
            if isinstance(val, list):
                return author in val or "any" in val
            return False

        def _blocked(val) -> bool:
            if val is None:
                return False
            if isinstance(val, str):
                return val == author or val == "any"
            if isinstance(val, list):
                return author in val or "any" in val
            return False

        # Q4: system bypasses authors whitelist (but blocked_authors
        # can still name "system" explicitly to lock it out).
        if author == "system":
            return not _blocked(blocked_authors)

        return _accepts(authors) and not _blocked(blocked_authors)

    def _lookup_event(self, plugin: Plugin, event_id: str) -> dict:
        """Look up event_entry by event_id (C1 step 1).

        Separated from topic_vars validation + template resolution so
        callers can do the ``enabled: false`` check (C2) BEFORE running
        the more expensive validation pass. Per C1 ORDER OF OPERATIONS:
        step 1 lookup → step 2 enabled check → ... → topic_vars validate.
        """
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event_id must be a non-empty string")

        events_dict = getattr(plugin, "events", {}) or {}
        event_entry = events_dict.get(event_id)
        if event_entry is None:
            raise ValueError(
                f"Event {event_id!r} not declared in events: section "
                f"of plugin {plugin.plugin_name!r} (LOCKED L #10)"
            )
        return event_entry

    def _resolve_topic_for_event(
        self,
        plugin: Plugin,
        event_id: str,
        topic_vars: Optional[Dict[str, str]],
        event_entry: Optional[dict] = None,
    ) -> tuple:
        """Look up event_id, validate topic_vars, resolve template.

        Returns (resolved_topic, event_entry). Raises ValueError on
        spec violations (LOCKED L #3-9 + Q15/Q16). The caller is
        expected to handle ``enabled: false`` semantics — this helper
        validates and resolves but does NOT decide whether to dispatch.

        ``event_entry`` may be passed by callers that already did
        ``_lookup_event`` (avoids double-lookup); when None, this helper
        does the lookup itself.
        """
        if event_entry is None:
            event_entry = self._lookup_event(plugin, event_id)

        # Validate topic_vars shape (LOCKED L #3-9).
        if topic_vars is None:
            tv: Dict[str, str] = {}
        elif isinstance(topic_vars, dict):
            tv = topic_vars
        else:
            raise TypeError(
                f"topic_vars must be a Dict[str, str] or None; "
                f"got {type(topic_vars).__name__} (LOCKED L #3)"
            )

        for k, v in tv.items():
            if not isinstance(k, str):
                raise TypeError(
                    f"topic_vars keys must be str; got {type(k).__name__} "
                    f"(LOCKED L #3)"
                )
            if k in _RESERVED_TEMPLATE_VARS:
                raise ValueError(
                    f"topic_vars key {k!r} is reserved (LOCKED L #6); "
                    f"reserved names: {sorted(_RESERVED_TEMPLATE_VARS)}"
                )
            if not isinstance(v, str):
                raise TypeError(
                    f"topic_vars[{k!r}] must be str; got {type(v).__name__} "
                    f"(LOCKED L #3)"
                )
            if "/" in v:
                raise ValueError(
                    f"topic_vars[{k!r}]={v!r} contains '/'; would inject "
                    f"extra topic segments (LOCKED L #4)"
                )
            if not v:
                raise ValueError(f"topic_vars[{k!r}] is empty string (LOCKED L #5)")
            stripped = v.strip()
            if not stripped:
                # Whitespace-only collapses to an empty segment per
                # LOCKED L #5; report it under the same rule for clarity.
                raise ValueError(
                    f"topic_vars[{k!r}]={v!r} is whitespace-only — "
                    f"collapses to empty segment (LOCKED L #5)"
                )
            if stripped != v:
                raise ValueError(
                    f"topic_vars[{k!r}]={v!r} has leading/trailing "
                    f"whitespace (LOCKED L)"
                )

        # Resolve {var} placeholders in the topic template.
        topic_template = event_entry["topic"]
        placeholders = set(_TEMPLATE_VAR_RE.findall(topic_template))

        if not placeholders and tv:
            self._logger.warning(
                "publish_event/request_event %s: topic %r is static but "
                "topic_vars=%r passed (LOCKED L #9 — likely confused "
                "payload vs topic_vars)",
                event_id,
                topic_template,
                tv,
            )

        # Check missing keys for {var} placeholders (LOCKED L #7).
        missing = placeholders - set(tv.keys())
        if missing:
            raise ValueError(
                f"event {event_id!r} topic {topic_template!r} requires "
                f"topic_vars keys {sorted(missing)} (LOCKED L #7)"
            )

        # Check extra topic_vars keys (LOCKED L #8 — WARN, not error).
        extra = set(tv.keys()) - placeholders
        if extra:
            self._logger.warning(
                "publish_event/request_event %s: topic_vars keys %r not "
                "used in topic %r (LOCKED L #8 — probably caller mistake)",
                event_id,
                sorted(extra),
                topic_template,
            )

        # Substitute. Use the same regex helper to keep behavior consistent.
        def _sub(match: re.Match) -> str:
            name = match.group(1)
            if name in tv:
                return tv[name]
            # Should be unreachable given the missing-key check above —
            # defensive guard.
            raise ValueError(f"event {event_id!r} unresolved placeholder {{{name}}}")

        resolved = _TEMPLATE_VAR_RE.sub(_sub, topic_template)

        # Post-resolution checks (Q15 reject empty + Q16 strip slashes).
        stripped_topic = resolved.strip("/")
        if not stripped_topic.strip():
            raise ValueError(f"event {event_id!r} resolved topic empty (Q15)")

        # Re-validate post-resolution (no embedded * mid-segment, no
        # empty middle segments). Wildcards forbidden in events. Per
        # LOCKED L's "ORDER OF OPERATIONS" + "FILTER LOOKUP" step 6.
        stripped_topic = _validate_topic_static(
            stripped_topic,
            context=f"event {event_id!r} resolved topic",
            allow_wildcards=False,
        )

        return stripped_topic, event_entry

    @async_log_errors
    async def publish_event(
        self,
        publisher: Plugin,
        event_id: str,
        payload: Any = None,
        topic_vars: Optional[Dict[str, str]] = None,
        hosts: Union[str, list, None] = None,
        blocked_hosts: Union[str, list, None] = None,
        *,
        _caller_chain: Optional[tuple] = None,
    ) -> int:
        """Publish an event (1:N fire-and-forget).

        Per PR3 PLAN F + LOCKED L FILTER LOOKUP. Returns the count of
        subscribers the dispatch was SCHEDULED for (local + remote,
        post-filter) — NOT a guarantee of delivery. Each per-sub
        fan-out runs as a fire-and-forget task; the count is computed
        and returned BEFORE those tasks execute. Subs whose target
        plugin is missing, whose handler signature is wrong, or whose
        handler raises mid-execution all count toward the return
        value (the per-sub Request resolves with error=True in those
        cases, but the publisher does not see it). Use ``request_event``
        when you need an actual delivery confirmation.

        Pure declaration model: ``event_id`` MUST exist in
        ``publisher.events``. Disabled events (``enabled: false``)
        silently drop and return 0 (C2).
        """
        # C1 ORDER OF OPERATIONS:
        # Step 1: event_id lookup.
        event_entry = self._lookup_event(publisher, event_id)

        # Step 2: enabled flag (C2 — silent drop on publish_event).
        # MUST precede topic_vars validation so disabled events with
        # malformed topic_vars don't raise ValueError.
        if not event_entry.get("enabled", True):
            self._logger.debug(
                "publish_event %s: event disabled, silent drop", event_id
            )
            return 0

        # Step 3: payload normalization (Q7).
        if payload is None:
            payload = {}

        # Step 4-5: topic_vars validation + template resolution +
        # post-resolution checks (Q15/Q16 + LOCKED L #3-9).
        resolved_topic, _ = self._resolve_topic_for_event(
            publisher, event_id, topic_vars, event_entry=event_entry
        )

        # Step 7: hosts/blocked_hosts default-and-override.
        # Validate caller-supplied values via _normalize_hosts (events:
        # defaults already normalized at YAML load). default=None so a
        # caller-None falls through to the event_entry's value cleanly;
        # an actual "local" default is then applied by
        # _publisher_targets_local. This catches malformed forms like
        # `hosts=[]` (empty list) at call time instead of silently
        # mishandling them downstream.
        if hosts is not None:
            hosts = _normalize_hosts(
                hosts,
                param_name="publish_event hosts",
                default=None,
            )
        if blocked_hosts is not None:
            blocked_hosts = _normalize_hosts(
                blocked_hosts,
                param_name="publish_event blocked_hosts",
                default=None,
            )
        eff_hosts = hosts if hosts is not None else event_entry.get("hosts")
        eff_blocked = (
            blocked_hosts
            if blocked_hosts is not None
            else event_entry.get("blocked_hosts")
        )
        # LOCKED IN — PUBLISHER hosts: emit WARNING for redundant combos
        # (e.g. hosts="any" + blocked_hosts="local" → equivalent to
        # hosts="remote", nudge caller toward the cleaner form).
        _warn_redundant_host_combos(eff_hosts, eff_blocked, self._logger)
        # Stage C will read eff_hosts/eff_blocked for the peer-level
        # filter (PR3 PLAN F step 5a). Stage B uses them ONLY to gate
        # whether local fan-out happens at all (e.g. hosts="remote"
        # means peer-only, no local delivery).

        # Publisher-level gate: skip local fan-out if publisher's
        # hosts/blocked_hosts exclude local delivery. PR3 Stage C still
        # runs remote dispatch even when local is skipped.
        local_targets = self._publisher_targets_local(eff_hosts, eff_blocked)
        now_ts = time.time()
        survivors: list = []

        if local_targets:
            # Step 4: local fan-out — find all local subs matching resolved
            # topic. find_all returns insertion order (LOCKED C).
            all_subs = await self.topic_registry.find_all(resolved_topic)
            local_subs = [s for s in all_subs if s.plugin_uuid in self.plugins_by_uuid]

            survivors = [
                s
                for s in local_subs
                if self._sub_accepts_local(s)
                and self._sub_accepts_author(s, publisher.plugin_name)
            ]

            if publisher.verbose_notifier:
                self._logger.debug(
                    "publish_event %s topic=%r matched %d local sub(s) "
                    "(of %d total subs)",
                    event_id,
                    resolved_topic,
                    len(survivors),
                    len(local_subs),
                )

            # Per-sub fan-out tasks. Each gets its own Request with
            # kind="publish_event", hosts="local" (C19), and
            # requester_id=sub.plugin_uuid (C18).
            for sub in survivors:
                await self._fanout_sub(
                    sub=sub,
                    publisher=publisher,
                    resolved_topic=resolved_topic,
                    payload=payload,
                    kind="publish_event",
                    timestamp=now_ts,
                    caller_chain=_caller_chain,
                    timeout=None,
                )
        else:
            if publisher.verbose_notifier:
                self._logger.debug(
                    "publish_event %s topic=%r: publisher hosts=%r "
                    "blocked_hosts=%r excludes local fan-out",
                    event_id,
                    resolved_topic,
                    eff_hosts,
                    eff_blocked,
                )

        # PR3 Stage C step 18 — remote dispatch (locked #16). Fire-and-
        # forget per-peer publish tasks for every advertised sub on
        # every reachable peer that survived per-peer + sub-level
        # filters. Best-effort; return count is local + remote.
        # Snapshot ``nm = self.network`` once (Commit 2b cycle 2 MED-B):
        # mid-block hot-reload would otherwise leak calls onto a
        # stopped NM. cycle 4 HIGH-1: the ``_deregister`` closure below
        # MUST capture ``nm`` via default-arg so done-callbacks fired
        # AFTER a rebuild swap continue mutating the OLD NM's
        # ``_inflight_publishes`` (drain is ongoing on it) instead of
        # corrupting the NEW NM's accounting.
        local_count = len(survivors)
        remote_count = 0
        nm = self.network
        if (
            getattr(self, "networking_enabled", False)
            and nm is not None
            and getattr(nm, "is_ready", False)
        ):
            try:
                from uuid import uuid4 as _uuid4

                request_uuid = _uuid4().hex
                per_peer = await nm._build_remote_dispatch(
                    topic=resolved_topic,
                    payload=payload,
                    author=publisher.plugin_name,
                    author_id=publisher.plugin_uuid,
                    author_host=self.hostname,
                    timestamp=now_ts,
                    request_uuid=request_uuid,
                    eff_hosts=eff_hosts,
                    eff_blocked_hosts=eff_blocked,
                )
                remote_count = sum(len(advs) for advs in per_peer.values())

                tasks = []
                for peer_hostname, advs in per_peer.items():
                    node = next(
                        (n for n in list(nm.nodes) if n.hostname == peer_hostname),
                        None,
                    )
                    if node is None:
                        continue
                    # locked #16: caller-acquires-_struct_lock-once;
                    # enabled recheck atomic with task creation.
                    async with nm._adverts_struct_lock:
                        if not node.enabled:
                            continue
                        t = asyncio.create_task(
                            nm.publish_event_remote(
                                node.IP,
                                resolved_topic,
                                payload,
                                publisher.plugin_name,
                                publisher.plugin_uuid,
                                self.hostname,
                                now_ts,
                                request_uuid,
                            )
                        )
                        nm._inflight_publishes.setdefault(peer_hostname, set()).add(t)
                    tasks.append(t)

                    # cycle 4 HIGH-1: capture ``nm`` via default-arg so
                    # the done-callback uses the OLD NM's accounting
                    # even if a hot-reload has swapped ``self.network``
                    # mid-flight. Reading ``self.network`` inside
                    # ``_drop`` would race with rebuild and corrupt
                    # the NEW NM's ``_inflight_publishes``.
                    def _deregister(_t, ph=peer_hostname, _nm=nm):
                        async def _drop():
                            async with _nm._adverts_struct_lock:
                                s = _nm._inflight_publishes.get(ph)
                                if s is not None:
                                    s.discard(_t)
                                    if not s:
                                        _nm._inflight_publishes.pop(ph, None)

                        try:
                            asyncio.create_task(_drop())
                        except RuntimeError:
                            pass

                    t.add_done_callback(_deregister)

                if tasks:
                    # POSS-W-A1-003 fix: register the outer gather
                    # wrapper through _spawn_tracked so a strong
                    # reference is kept in self.task_list (preventing
                    # GC mid-flight) and shutdown drain can wait on
                    # it. Individual per-peer ``t`` tasks remain
                    # tracked via nm._inflight_publishes; this wrapper
                    # only swallows their exceptions via
                    # return_exceptions=True.
                    self._spawn_tracked(
                        asyncio.gather(*tasks, return_exceptions=True),
                        name=f"publish_event_remote_fanout:{resolved_topic}",
                    )
            except Exception:
                self._logger.debug(
                    "publish_event remote dispatch failed", exc_info=True
                )

        # B-073 Step 8 emit: event published. ALWAYS emit even when
        # target_count=0 — useful for "publisher fired, nothing
        # listened" debugging.
        self._internal_emit(
            "_core/event/published",
            publisher=publisher.plugin_name,
            topic=resolved_topic,
            target_count=local_count + remote_count,
            ts=now_ts,
        )
        return local_count + remote_count

    async def _fanout_sub(
        self,
        *,
        sub: Subscription,
        publisher: Optional[Plugin],
        resolved_topic: str,
        payload: Any,
        kind: str,
        timestamp: float,
        timeout: Optional[float],
        caller_chain: Optional[tuple] = None,
        # PR3 Stage C — locked #3 + #15. When invoked from the
        # networking-side handler path, `publisher` is None and the
        # remote publisher metadata arrives via these kwargs.
        remote_publisher_name: Optional[str] = None,
        remote_publisher_uuid: Optional[str] = None,
        remote_publisher_host: Optional[str] = None,
        remote_verbose: bool = False,
    ) -> Optional[Request]:
        """Build a per-sub Request and spawn its dispatch task (publish
        path) or build + return without spawning (request path; caller
        awaits it).

        Stage B always returns the Request. publish_event ignores the
        return value (fire-and-forget). request_event awaits it.
        """
        # PR3 Stage C defense-in-depth (locked #15): reject any caller
        # path that hands us a remote_publisher_host claiming our own
        # hostname. The wire handler already gates this; defense-in-
        # depth covers tests + future direct callers that bypass
        # _handle_client.
        if remote_publisher_host is not None and remote_publisher_host == self.hostname:
            self._logger.warning(
                "fan-out gate: remote_publisher_host equals our hostname; rejecting"
            )
            return None

        # Resolve effective publisher metadata. Local path reads from
        # `publisher: Plugin`; remote path reads from kwargs.
        if publisher is not None:
            eff_author = publisher.plugin_name
            eff_author_id = publisher.plugin_uuid
            eff_author_host = self.hostname
        else:
            eff_author = remote_publisher_name or "remote"
            eff_author_id = remote_publisher_uuid or "remote"
            eff_author_host = remote_publisher_host or ""

        request = Request(
            author_host=eff_author_host,
            plugin=sub.target_plugin or sub.plugin_name,
            method=sub.target_access_name,
            args=payload,
            plugin_uuid=sub.target_plugin_uuid,
            target_hosts="local",  # C19
            blocked_hosts=None,
            author=eff_author,
            author_id=eff_author_id,
            timeout=timeout,
            request_id=None,
            event_loop=self.main_event_loop,
            kind=kind,
            topic=resolved_topic,
            # C4: declared_id (YAML key) for config subs, sub_uuid for
            # runtime. Use `is not None` instead of truthy `or` so an
            # empty-string declared_id (impossible from YAML loader, but
            # possible via direct topic_registry.subscribe(declared_id="")
            # calls) doesn't silently fall through to sub_uuid.
            origin_subscription_id=(
                sub.declared_id if sub.declared_id is not None else sub.sub_uuid
            ),
            timestamp=timestamp,
            requester_id=sub.plugin_uuid,  # C18
        )

        # C10: propagate the publisher's sync call chain through fan-out
        # so cross-pool cycle detection still works when a sync subscriber
        # handler eventually re-enters execute_sync / publish_event_sync /
        # etc. Stage A's _tracked_event wrapper reads request._call_chain.
        # Use the SAME flat-string format the execute path uses
        # (`f"{plugin}.{method}"`) — see _execute_sync_tracked at the
        # `chain + (target,)` site. Mismatched element shapes break the
        # `target in chain` membership test downstream and let real
        # cycles slip past detection.
        #
        # ``caller_chain`` is passed by sync entry points (publish_event_sync
        # etc.) which captured _sync_call_chain.chain on the WORKER thread
        # before scheduling onto the event loop. Threadlocal lookup here
        # would return () because the event loop thread never set it. If
        # not provided, fall back to threadlocal — covers the async-caller
        # path where _fanout_sub runs in the same task tree as the sync
        # wrapper that set the chain.
        if caller_chain is not None:
            existing_chain = caller_chain
        else:
            existing_chain = getattr(_sync_call_chain, "chain", ())
        target_for_chain = (
            f"{sub.target_plugin or sub.plugin_name}.{sub.target_access_name}"
        )
        request._call_chain = tuple(existing_chain) + (target_for_chain,)

        async with self.request_lock:
            self.requests[request.id] = request

        async def _run_and_collect():
            try:
                await self._process_request(request)
            finally:
                # B-073 Session 2 Step 3: done-callback eviction. Was
                # ``await request.set_collected()`` (Q12 fix); migrated
                # to direct sync pop. Idempotent — _process_request's
                # own finally already pops via the producer-side path.
                self.requests.pop(request.id, None)

        # Name uses `sub.target_plugin or sub.plugin_name` to mirror the
        # actual dispatch target (line ~3876 already applies that
        # fallback for the Request's `plugin` field).
        self._spawn_tracked(
            _run_and_collect(),
            name=f"sub:{sub.target_plugin or sub.plugin_name}.{sub.target_access_name}<-{resolved_topic}",
        )

        return request

    @log_errors
    def publish_event_sync(
        self,
        publisher: Plugin,
        event_id: str,
        payload: Any = None,
        topic_vars: Optional[Dict[str, str]] = None,
        hosts: Union[str, list, None] = None,
        blocked_hosts: Union[str, list, None] = None,
    ) -> int:
        """Sync variant of publish_event (C16). Schedules the async
        coroutine on main_event_loop via run_coroutine_threadsafe.
        Pre-start guard fires inside the Plugin wrapper (Q1).

        C10: capture _sync_call_chain.chain on the WORKER thread before
        scheduling onto the event loop. The coroutine running on the
        loop thread sees `_sync_call_chain.chain == ()` (different
        thread, different threadlocal), so the chain must be passed
        explicitly via _caller_chain to thread cycle detection through
        sync→fan-out→sync paths.
        """
        chain = getattr(_sync_call_chain, "chain", ())
        future = asyncio.run_coroutine_threadsafe(
            self.publish_event(
                publisher,
                event_id,
                payload,
                topic_vars,
                hosts,
                blocked_hosts,
                _caller_chain=chain,
            ),
            self.main_event_loop,
        )
        return future.result()

    @async_log_errors
    async def request_event(
        self,
        publisher: Plugin,
        event_id: str,
        payload: Any = None,
        topic_vars: Optional[Dict[str, str]] = None,
        hosts: Union[str, list, None] = None,
        blocked_hosts: Union[str, list, None] = None,
        timeout: Optional[float] = None,
        *,
        _caller_chain: Optional[tuple] = None,
    ) -> Any:
        """Request an event (1:1 ask).

        Per PR3 PLAN F. Tie-break: insertion order on local subs (LOCKED
        C). No local match → RequestException (Stage B is LOCAL-only;
        remote dispatch lands in Stage C).
        """
        # C1 ORDER OF OPERATIONS: lookup → enabled → payload → resolve.
        event_entry = self._lookup_event(publisher, event_id)

        # C2: disabled events raise on request_event (caller awaits a
        # result, can't silently return None). MUST precede topic_vars
        # validation.
        if not event_entry.get("enabled", True):
            raise RequestException(f"event {event_id!r} disabled (C2)")

        if payload is None:
            payload = {}

        resolved_topic, _ = self._resolve_topic_for_event(
            publisher, event_id, topic_vars, event_entry=event_entry
        )

        # Validate caller-supplied hosts/blocked_hosts (events: defaults
        # already normalized at YAML load). Catches malformed forms at
        # call time. default=None so caller-None falls through.
        if hosts is not None:
            hosts = _normalize_hosts(
                hosts,
                param_name="request_event hosts",
                default=None,
            )
        if blocked_hosts is not None:
            blocked_hosts = _normalize_hosts(
                blocked_hosts,
                param_name="request_event blocked_hosts",
                default=None,
            )

        # Publisher-level hosts gate: when hosts="remote" or excludes
        # local, skip the local-match phase entirely and go straight to
        # Stage C remote dispatch (locked #18 item 7). Previously raised
        # here, which prevented hosts="remote" callers from ever reaching
        # the remote candidate iteration block.
        eff_hosts = hosts if hosts is not None else event_entry.get("hosts")
        eff_blocked = (
            blocked_hosts
            if blocked_hosts is not None
            else event_entry.get("blocked_hosts")
        )
        _warn_redundant_host_combos(eff_hosts, eff_blocked, self._logger)
        local_targets = self._publisher_targets_local(eff_hosts, eff_blocked)

        # Capture timestamp once so all per-sub Requests built off this
        # call see consistent epoch seconds (consistency with
        # publish_event).
        now_ts = time.time()

        # Find first matching LOCAL sub (insertion order) only when the
        # publisher's hosts filter actually targets local. Apply the
        # same sub-level host/author filter as publish_event so subs
        # with hosts="remote" or blocked_authors filtering us out are
        # skipped (LOCKED H).
        if local_targets:
            all_subs = await self.topic_registry.find_all(resolved_topic)
            local_match = next(
                (
                    s
                    for s in all_subs
                    if s.plugin_uuid in self.plugins_by_uuid
                    and self._sub_accepts_local(s)
                    and self._sub_accepts_author(s, publisher.plugin_name)
                ),
                None,
            )
        else:
            local_match = None

        if local_match is None:
            # PR3 Stage C step 19 — remote dispatch fall-through (locked
            # #6 + #13). Iterate _inbound_global_order in C11 insertion
            # order, apply ALL filters, try each surviving candidate.
            # Snapshot ``nm = self.network`` once (Commit 2b cycle 2
            # MED-B): mid-block hot-reload would otherwise leak calls
            # onto a stopped NM. None falls through to the bottom
            # ``raise RequestException("no subscriber matches...")``.
            #
            # B-073 Step 8: ``candidates`` + ``request_uuid`` initialized
            # OUTSIDE the networking sub-block so (a) the emit fires
            # even on the networking-disabled path with target_count=0,
            # and (b) ``request_uuid`` is bound for the second
            # networking guard's dispatch loop even if observer-driven
            # state flips networking between the two guards.
            from uuid import uuid4 as _uuid4
            from .notifier import TopicRegistry as _TR

            candidates: list = []
            request_uuid = _uuid4().hex
            nm = self.network
            if (
                getattr(self, "networking_enabled", False)
                and nm is not None
                and getattr(nm, "is_ready", False)
            ):
                async with nm._adverts_struct_lock:
                    cands_raw = list(nm._inbound_global_order.items())

                for (peer_hostname, _sub_uuid), advert in cands_raw:
                    node = next(
                        (n for n in list(nm.nodes) if n.hostname == peer_hostname),
                        None,
                    )
                    if node is None:
                        continue
                    try:
                        if not (node.enabled and await node.is_alive()):
                            continue
                    except Exception:
                        continue
                    if not nm._hosts_match(eff_hosts, eff_blocked, peer_hostname):
                        continue
                    if not self._sub_accepts_remote_publisher(
                        advert, self.hostname, publisher.plugin_name
                    ):
                        continue
                    if not self._sub_accepts_author(advert, publisher.plugin_name):
                        continue
                    if not _TR._topic_matches(advert.topic_pattern, resolved_topic):
                        continue
                    candidates.append((peer_hostname, advert, node))

            # B-073 Step 8 emit: event_requested on no-local-match path.
            # target_count covers all 3 sub-paths (networking disabled
            # → 0; networking on but no candidates → 0; networking on
            # with candidates → N).
            self._internal_emit(
                "_core/event/requested",
                publisher=publisher.plugin_name,
                topic=resolved_topic,
                target_count=len(candidates),
                ts=now_ts,
            )

            # B-074 Step 10 verbose log: no-local-match branch.
            if publisher.verbose_notifier:
                self._logger.debug(
                    "request_event %s topic=%r no local match, "
                    "falling through to %d remote candidates",
                    event_id,
                    resolved_topic,
                    len(candidates),
                )

            if (
                getattr(self, "networking_enabled", False)
                and nm is not None
                and getattr(nm, "is_ready", False)
            ):
                last_exc: Optional[BaseException] = None
                for peer_hostname, advert, node in candidates:
                    try:
                        return await nm.request_event_remote(
                            node.IP,
                            resolved_topic,
                            payload,
                            publisher.plugin_name,
                            publisher.plugin_uuid,
                            self.hostname,
                            now_ts,
                            request_uuid,
                            timeout=timeout,
                        )
                    except (NetworkRequestException, NoLocalSubException) as exc:
                        last_exc = exc
                        continue  # locked #6 fall-through
                    except RequestException:
                        raise

                # All candidates exhausted (or none) — propagate.
                if last_exc is not None:
                    raise RequestException(
                        f"request_event {event_id!r}: no handler found / all "
                        f"unreachable (last: {last_exc})"
                    )

            raise RequestException(
                f"request_event {event_id!r}: no subscriber matches resolved "
                f"topic {resolved_topic!r}"
            )

        # B-073 Step 8 emit: event_requested on local-match path.
        self._internal_emit(
            "_core/event/requested",
            publisher=publisher.plugin_name,
            topic=resolved_topic,
            target_count=1,
            ts=now_ts,
        )

        # B-074 Step 10 verbose log: local-match branch.
        if publisher.verbose_notifier:
            self._logger.debug(
                "request_event %s topic=%r matched local sub uuid=%s, " "dispatching",
                event_id,
                resolved_topic,
                local_match.sub_uuid,
            )

        request = await self._fanout_sub(
            sub=local_match,
            publisher=publisher,
            resolved_topic=resolved_topic,
            payload=payload,
            kind="request_event",
            timestamp=now_ts,
            timeout=timeout,
            caller_chain=_caller_chain,
        )

        try:
            result, error, _ = await request.wait_for_result_async()
            if error:
                raise RequestException(result)
            return result
        finally:
            # B-073 Session 2 Step 3: done-callback eviction. Was
            # ``await request.set_collected()``; migrated to direct sync
            # pop. Defensive — _process_request's producer-side finally
            # and _fanout_sub._run_and_collect's finally both also pop
            # the same Request id. All three pops are idempotent under
            # ``pop(key, None)``. Triple-pop is harmless.
            self.requests.pop(request.id, None)

    @log_errors
    def request_event_sync(
        self,
        publisher: Plugin,
        event_id: str,
        payload: Any = None,
        topic_vars: Optional[Dict[str, str]] = None,
        hosts: Union[str, list, None] = None,
        blocked_hosts: Union[str, list, None] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Sync variant of request_event (C16). C10: capture caller's
        _sync_call_chain on the WORKER thread before scheduling."""
        chain = getattr(_sync_call_chain, "chain", ())
        future = asyncio.run_coroutine_threadsafe(
            self.request_event(
                publisher,
                event_id,
                payload,
                topic_vars,
                hosts,
                blocked_hosts,
                timeout,
                _caller_chain=chain,
            ),
            self.main_event_loop,
        )
        return future.result()

    @async_gen_log_errors
    async def request_event_stream(
        self,
        publisher: Plugin,
        event_id: str,
        payload: Any = None,
        topic_vars: Optional[Dict[str, str]] = None,
        hosts: Union[str, list, None] = None,
        blocked_hosts: Union[str, list, None] = None,
        timeout: Optional[float] = None,
        *,
        _caller_chain: Optional[tuple] = None,
    ) -> Any:
        """Streaming variant of request_event. First yield is wrapped
        in Event metadata (LOCKED I); subsequent yields raw."""
        # C1 ORDER OF OPERATIONS: lookup → enabled → payload → resolve.
        event_entry = self._lookup_event(publisher, event_id)
        if not event_entry.get("enabled", True):
            raise RequestException(f"event {event_id!r} disabled (C2)")
        if payload is None:
            payload = {}
        resolved_topic, _ = self._resolve_topic_for_event(
            publisher, event_id, topic_vars, event_entry=event_entry
        )

        # Validate caller-supplied hosts/blocked_hosts (parity with
        # publish_event/request_event; events: defaults pre-normalized
        # at YAML load).
        if hosts is not None:
            hosts = _normalize_hosts(
                hosts,
                param_name="request_event_stream hosts",
                default=None,
            )
        if blocked_hosts is not None:
            blocked_hosts = _normalize_hosts(
                blocked_hosts,
                param_name="request_event_stream blocked_hosts",
                default=None,
            )

        # Publisher-level hosts gate (same as request_event): skip local
        # match entirely when publisher's hosts filter excludes local;
        # fall through directly to Stage C remote dispatch (locked #18
        # item 8).
        eff_hosts = hosts if hosts is not None else event_entry.get("hosts")
        eff_blocked = (
            blocked_hosts
            if blocked_hosts is not None
            else event_entry.get("blocked_hosts")
        )
        _warn_redundant_host_combos(eff_hosts, eff_blocked, self._logger)
        local_targets = self._publisher_targets_local(eff_hosts, eff_blocked)

        # Capture timestamp once (consistency with publish_event /
        # request_event).
        now_ts = time.time()

        # Apply the same sub-level filter as publish_event /
        # request_event so subs with hosts="remote" or blocked_authors
        # filtering us out are skipped (LOCKED H). Only run local-match
        # when publisher actually targets local.
        if local_targets:
            all_subs = await self.topic_registry.find_all(resolved_topic)
            local_match = next(
                (
                    s
                    for s in all_subs
                    if s.plugin_uuid in self.plugins_by_uuid
                    and self._sub_accepts_local(s)
                    and self._sub_accepts_author(s, publisher.plugin_name)
                ),
                None,
            )
        else:
            local_match = None
        if local_match is None:
            # PR3 Stage C step 20 — remote dispatch fall-through (locked
            # #6 + #13). Pre-first-chunk fall-through ONLY; mid-stream
            # NetworkRequestException terminates without fall-through to
            # preserve the Event-first invariant.
            # Snapshot ``nm = self.network`` once (Commit 2b cycle 2
            # MED-B): mid-block hot-reload would otherwise leak calls
            # onto a stopped NM. None falls through to the bottom
            # ``raise RequestException("no subscriber matches...")``.
            nm = self.network
            if (
                getattr(self, "networking_enabled", False)
                and nm is not None
                and getattr(nm, "is_ready", False)
            ):
                from uuid import uuid4 as _uuid4
                from .notifier import TopicRegistry as _TR

                request_uuid = _uuid4().hex

                async with nm._adverts_struct_lock:
                    cands_raw = list(nm._inbound_global_order.items())

                candidates = []
                for (peer_hostname, _sub_uuid), advert in cands_raw:
                    node = next(
                        (n for n in list(nm.nodes) if n.hostname == peer_hostname),
                        None,
                    )
                    if node is None:
                        continue
                    try:
                        if not (node.enabled and await node.is_alive()):
                            continue
                    except Exception:
                        continue
                    if not nm._hosts_match(eff_hosts, eff_blocked, peer_hostname):
                        continue
                    if not self._sub_accepts_remote_publisher(
                        advert, self.hostname, publisher.plugin_name
                    ):
                        continue
                    if not self._sub_accepts_author(advert, publisher.plugin_name):
                        continue
                    if not _TR._topic_matches(advert.topic_pattern, resolved_topic):
                        continue
                    candidates.append((peer_hostname, advert, node))

                last_exc: Optional[BaseException] = None
                for peer_hostname, advert, node in candidates:
                    agen = nm.request_event_stream_remote(
                        node.IP,
                        resolved_topic,
                        payload,
                        publisher.plugin_name,
                        publisher.plugin_uuid,
                        self.hostname,
                        now_ts,
                        request_uuid,
                        timeout=timeout,
                    )
                    # Tee first chunk in an isolated try/except so that
                    # ONLY pre-first-chunk failures fall through (locked
                    # #6 strict). Mid-stream errors propagate verbatim.
                    try:
                        first = await agen.__anext__()
                    except StopAsyncIteration:
                        # Empty stream — degenerate but legal. Treat as
                        # successful with zero items.
                        return
                    except (NetworkRequestException, NoLocalSubException) as exc:
                        last_exc = exc
                        continue
                    except RequestException:
                        raise

                    # First chunk yielded — committed to this peer; no
                    # fall-through past this point.
                    yield first
                    async for chunk in agen:
                        yield chunk
                    return

                if last_exc is not None:
                    raise RequestException(
                        f"request_event_stream {event_id!r}: no handler "
                        f"found / all unreachable (last: {last_exc})"
                    )

            raise RequestException(
                f"request_event_stream {event_id!r}: no subscriber matches "
                f"resolved topic {resolved_topic!r}"
            )

        # B-074 Step 10 verbose log: stream local-match opening.
        if publisher.verbose_notifier:
            self._logger.debug(
                "request_event_stream %s topic=%r matched local sub uuid=%s, "
                "opening stream",
                event_id,
                resolved_topic,
                local_match.sub_uuid,
            )

        # Route through find_endpoint so the C18 accessible_by_other_plugins
        # access check applies on the streaming path too. Pass
        # requester_id=local_match.plugin_uuid (the SUB OWNER's identity)
        # so cross-plugin subs to private endpoints are denied
        # consistently with the non-streaming request_event path.
        # NOTE: find_endpoint returns (None, None, None) on no-match
        # (NOT bare None), so check the unpacked plugin slot.
        target_plugin, endpoint, _node = await self.find_endpoint(
            access_name=local_match.target_access_name,
            hosts="local",
            plugin_uuid=local_match.target_plugin_uuid,
            requester_id=local_match.plugin_uuid,
            target_plugin=local_match.target_plugin,
        )
        if target_plugin is None or endpoint is None:
            raise RequestException(
                f"request_event_stream {event_id!r}: target endpoint "
                f"{local_match.target_access_name!r} not found on "
                f"{local_match.target_plugin!r} (or access denied per C18)"
            )

        # R1 HIGH-3 fix: Stage O readiness gate also applies on the
        # LOCAL request_event_stream path. Without this gate, fan-out
        # from a publisher to a subscriber that is mid-on_enable would
        # bypass _process_request_stream's gate entirely (this path
        # iterates the generator directly) and hit a not-yet-ready
        # handler. Same skip rules as _process_request_stream — remote
        # plugins (no readiness events) and self-calls (Q23 — avoid
        # gating against own _lifecycle_ready from inside on_enable).
        if (
            isinstance(target_plugin, Plugin)
            and target_plugin.plugin_uuid != publisher.plugin_uuid
        ):
            try:
                await self._wait_for_plugin_ready(target_plugin)
            except asyncio.TimeoutError as e:
                ready_timeout = getattr(
                    self,
                    "plugin_ready_timeout",
                    DEFAULT_PLUGIN_READY_TIMEOUT,
                )
                raise RequestException(
                    f"request_event_stream {event_id!r}: target plugin "
                    f"{target_plugin.plugin_name!r} not ready within "
                    f"{ready_timeout}s"
                ) from e

        internal = endpoint.get("internal_name") or local_match.target_access_name
        func = getattr(target_plugin, internal, None)
        if func is None or not (
            inspect.isasyncgenfunction(func) or inspect.isgeneratorfunction(func)
        ):
            raise RequestException(
                f"request_event_stream {event_id!r}: handler is not a "
                f"generator function (use request_event instead)"
            )

        # Build the Event metadata for first-chunk wrapping.
        event_meta = Event(
            topic=resolved_topic,
            payload=payload,
            author=publisher.plugin_name,
            author_id=publisher.plugin_uuid,
            author_host=self.hostname,
            subscription_id=(
                local_match.declared_id
                if local_match.declared_id is not None
                else local_match.sub_uuid
            ),
            timestamp=now_ts,
        )

        # B-054 fix: route through GeneratorRequest + _spawn_tracked
        # so close()'s 30s drain catches the in-flight stream and
        # pop_plugin's pending-request walk can fail the Request when
        # the target plugin is unloaded mid-stream.
        #
        # CRITICAL — pass timeout=None to GeneratorRequest. The
        # timeout we received is enforced by the producer's own
        # _residual() (loop.time() monotonic deadline). If we also
        # passed it here, get_queue_stream (utils.py:1999/2013)
        # would enforce it independently with wall-clock time.time(),
        # producing a double-trigger race. The current inline code
        # had NO consumer-side get_queue_stream timeout, so timeout=
        # None here preserves single-source-of-truth semantics.
        request = GeneratorRequest(
            author_host=self.hostname,
            plugin=local_match.target_plugin or local_match.plugin_name,
            method=local_match.target_access_name,
            args=payload,
            plugin_uuid=local_match.target_plugin_uuid,
            target_hosts="local",
            blocked_hosts=None,
            author=publisher.plugin_name,
            author_id=publisher.plugin_uuid,
            timeout=None,  # B-054: producer enforces, see above
            request_id=None,
            event_loop=self.main_event_loop,
            kind="request_event_stream",
            topic=resolved_topic,
            origin_subscription_id=event_meta.subscription_id,
            timestamp=now_ts,
            requester_id=local_match.plugin_uuid,
        )
        async with self.request_lock:
            self.requests[request.id] = request

        producer_task = self._spawn_tracked(
            self._process_request_event_stream(
                request,
                target_plugin,
                endpoint,
                event_meta,
                timeout=timeout,
                caller_chain=_caller_chain,
                verbose_notifier=publisher.verbose_notifier,
            ),
            name=f"event_stream:{request.target_plugin}.{request.target_method}<-{resolved_topic}",
        )
        request._producer_task = producer_task

        # B-074 Step 10: stream-end tracking for verbose log L5.
        chunk_count = 0
        exit_reason = "normal"
        try:
            try:
                async for result, error, _ in request.get_queue_stream():
                    if error:
                        self._logger.warning(
                            f"Error in request_event_stream {event_id!r} "
                            f"(GenReq-ID: {request.id}): {result}. You can "
                            f"check the logs for this Req-ID."
                        )
                        exit_reason = "exception"
                        raise RequestException(result)
                    chunk_count += 1
                    yield result
            except GeneratorExit:
                # Consumer broke out of `async for chunk in ...:` early.
                exit_reason = "consumer_break"
                raise
            except BaseException:
                if exit_reason == "normal":
                    exit_reason = "exception"
                raise
        finally:
            # B-073 Step 8 emit: event streamed ended. Captures all 3
            # exit paths (natural exhaustion, consumer break, exception).
            self._internal_emit(
                "_core/event/streamed",
                publisher=publisher.plugin_name,
                topic=resolved_topic,
                phase="ended",
                ts=time.time(),
            )
            # B-074 Step 10 verbose log: stream ended.
            if publisher.verbose_notifier:
                self._logger.debug(
                    "request_event_stream %s topic=%r stream ended " "(chunks=%d, %s)",
                    event_id,
                    resolved_topic,
                    chunk_count,
                    exit_reason,
                )
            # Mark for cleanup. Cancels the producer task on early
            # break (B-002 pattern). Mirrors execute_stream's pattern.
            await request.set_collected()

    @gen_log_errors
    def request_event_stream_sync(
        self,
        publisher: Plugin,
        event_id: str,
        payload: Any = None,
        topic_vars: Optional[Dict[str, str]] = None,
        hosts: Union[str, list, None] = None,
        blocked_hosts: Union[str, list, None] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Sync variant of request_event_stream (C16). Iterates the
        async generator on main_event_loop and yields chunks back to
        the caller thread.

        C10: capture _sync_call_chain.chain on the WORKER thread before
        scheduling the async generator on the loop. The loop thread can't
        see this threadlocal; sync-gen handler invocations inside the
        stream re-set the chain on each next() call (see
        request_event_stream sync branch).
        """
        chain = getattr(_sync_call_chain, "chain", ())
        async_gen = self.request_event_stream(
            publisher,
            event_id,
            payload,
            topic_vars,
            hosts,
            blocked_hosts,
            timeout,
            _caller_chain=chain,
        )

        try:
            while True:
                try:
                    chunk = asyncio.run_coroutine_threadsafe(
                        async_gen.__anext__(), self.main_event_loop
                    ).result()
                except StopAsyncIteration:
                    break
                yield chunk
        finally:
            # Close the underlying async generator if the caller breaks
            # out of the for loop early (without exhausting it). Without
            # this aclose() the async gen's try/finally / async with
            # blocks never run, leaking resources.
            #
            # B-055 fix: bound the wait with a 5s timeout so a hanging
            # handler `finally`/`async with` cleanup can't block this
            # caller's worker thread forever. On expiry, cancel the
            # orphaned aclose task so it doesn't leak on the event loop;
            # CancelledError propagates into the handler's hung await
            # and unblocks the cleanup eventually.
            # concurrent.futures.TimeoutError is what
            # Future.result(timeout=...) raises on expiry (a separate
            # class from asyncio.TimeoutError, even though they alias to
            # builtins.TimeoutError on Python 3.11+).
            fut = asyncio.run_coroutine_threadsafe(
                async_gen.aclose(), self.main_event_loop
            )
            try:
                fut.result(timeout=5.0)
            except concurrent.futures.TimeoutError:
                fut.cancel()
                self._logger.warning(
                    "request_event_stream_sync: aclose() exceeded 5s — "
                    "underlying handler's finally/async-with cleanup may "
                    "be blocked; cancelled orphan task"
                )
            except Exception:
                pass

    async def subscribe_event(
        self,
        topic: str,
        plugin_name: str,
        plugin_uuid: str,
        target_access_name: str,
        target_plugin: Optional[str] = None,
        target_plugin_uuid: Optional[str] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        authors: Union[str, list, None] = None,
        blocked_authors: Union[str, list, None] = None,
    ) -> str:
        """Register a runtime subscription (NEW PR3 API). Returns sub_uuid.

        Pure-runtime path; declared_id stays None per LOCKED D.
        Adds the sub_uuid to the owning plugin's _sub_uuids list so the
        on_disable wrapper can include it in the unregister sweep.

        Topic + filter values are validated with the same rules YAML
        load applies (LOCKED L #2 + LOCKED IN — PUBLISHER hosts) so a
        runtime ``subscribe(\"sensor/abc*\", ...)`` (embedded `*`) or
        ``subscribe(\"\", ...)`` (empty) doesn't silently produce a
        permanently dead subscription.
        """
        topic = _validate_subscription_topic(
            topic, context=f"runtime subscribe ({plugin_name})"
        )
        # Defensive: target_access_name must be a non-empty identifier-style
        # string. The Plugin.subscribe wrapper already checks this for the
        # standard call path, but direct Plexus.subscribe_event calls
        # (test code, future internal callers) bypass the wrapper. Without
        # this guard, an empty string silently produces a permanently dead
        # subscription — find_endpoint(access_name="") returns
        # (None,None,None) every time with a confusing "endpoint not found"
        # error far from the bad subscribe call.
        if not isinstance(target_access_name, str) or not target_access_name.strip():
            raise ValueError(
                f"runtime subscribe ({plugin_name}): target_access_name "
                f"must be a non-empty string; got "
                f"{type(target_access_name).__name__}={target_access_name!r}"
            )
        hosts = _normalize_hosts(
            hosts,
            param_name=f"runtime subscribe ({plugin_name}).hosts",
            default="any",
        )
        blocked_hosts = _normalize_hosts(
            blocked_hosts,
            param_name=f"runtime subscribe ({plugin_name}).blocked_hosts",
            default=None,
        )
        authors = _normalize_hosts(
            authors,
            param_name=f"runtime subscribe ({plugin_name}).authors",
            default=None,
        )
        blocked_authors = _normalize_hosts(
            blocked_authors,
            param_name=f"runtime subscribe ({plugin_name}).blocked_authors",
            default=None,
        )

        sub_uuid = await self.topic_registry.subscribe(
            topic_pattern=topic,
            plugin_name=plugin_name,
            plugin_uuid=plugin_uuid,
            target_plugin=target_plugin or plugin_name,
            target_access_name=target_access_name,
            target_plugin_uuid=target_plugin_uuid,
            hosts=hosts,
            blocked_hosts=blocked_hosts,
            authors=authors,
            blocked_authors=blocked_authors,
            declared_id=None,
            enabled=True,
        )

        owner = self.plugins_by_uuid.get(plugin_uuid)
        if owner is not None and hasattr(owner, "_sub_uuids"):
            owner._sub_uuids.append(sub_uuid)

        # PR3 Stage C add-delta hook (locked #18 item 3). No-op when
        # networking is disabled or not yet ready.
        # Snapshot nm (Commit 2b cycle 2 MED-B): single-call site;
        # snapshotting matches the loop-site pattern for consistency
        # and tightens the guard-vs-call window in case of mid-block
        # hot-reload.
        nm = self.network
        if (
            getattr(self, "networking_enabled", False)
            and nm is not None
            and getattr(nm, "is_ready", False)
        ):
            sub = await self.topic_registry.get_subscription(sub_uuid)
            if sub is not None:
                try:
                    await nm.broadcast_local_sub_added(sub)
                except Exception:
                    self._logger.debug(
                        "subscribe_event: broadcast add-delta failed",
                        exc_info=True,
                    )

        return sub_uuid

    async def unsubscribe_event(self, sub_uuid: str) -> bool:
        """Remove a runtime subscription (NEW PR3 API).

        Returns True if found and removed. Cleans up the owning plugin's
        _sub_uuids list as a side-effect (best-effort lookup).
        """
        sub = await self.topic_registry.get_subscription(sub_uuid)

        # PR3 Stage C remove-delta hook (locked #18 item 4). Send BEFORE
        # the registry drop so the broadcast still has access to the
        # sub object and our peers see the remove cleanly.
        # Snapshot nm (Commit 2b cycle 2 MED-B): single-call site,
        # snapshotting for consistency with the loop-site pattern.
        nm = self.network
        if (
            sub is not None
            and getattr(self, "networking_enabled", False)
            and nm is not None
            and getattr(nm, "is_ready", False)
        ):
            try:
                await nm.broadcast_local_sub_removed(sub)
            except Exception:
                self._logger.debug(
                    "unsubscribe_event: broadcast remove-delta failed",
                    exc_info=True,
                )

        ok = await self.topic_registry.unsubscribe(sub_uuid)
        if ok and sub is not None:
            owner = self.plugins_by_uuid.get(sub.plugin_uuid)
            if owner is not None and hasattr(owner, "_sub_uuids"):
                try:
                    owner._sub_uuids.remove(sub_uuid)
                except ValueError:
                    pass
        return ok

    # ── Phase 2a: runtime sub/event enable-toggle API ─────────────────
    # Twin async methods for flipping enabled flags on existing subs or
    # events at runtime. Both emit a ``_core/<noun>/state_changed`` topic
    # so the TUI Events tab + future tooling can react. See
    # ``_private/tui_phase2_events_plan.md`` sections 4.2-4.4 for the
    # detailed design + the lock-ordering rationale for splitting
    # subscription toggle across notifier (atomic mutation under registry
    # lock) and Plexus (broadcast + emit outside the lock).

    async def set_subscription_enabled(self, sub_uuid: str, enabled: bool) -> bool:
        """Toggle a subscription's enabled flag at runtime.

        Mutation happens atomically inside ``topic_registry._lock`` via
        ``TopicRegistry.set_subscription_enabled`` (notifier.py) which
        returns ``(sub_or_None, changed)``. When networking is enabled
        and ready, this method then broadcasts an add-delta to peers
        on a True transition (peer starts advertising the sub) or a
        remove-delta on a False transition (peer stops). Broadcasts
        happen OUTSIDE the registry lock, per the framework's
        lock-ordering rule (mirrored from the
        ``subscribe_event``/``unsubscribe_event`` patterns at
        ``core.py:6595`` + ``6626`` — see the lock-ordering
        comment block at ``_get_lifecycle_lock`` for the
        "no-network-I/O-under-registry-lock" invariant).

        After mutation + broadcast attempt, emits
        ``_core/subscription/state_changed`` so the Subscriptions
        browser + Live-stream can react. Emit fires ONLY when the flag
        actually changed (idempotent no-op call returns True without
        emitting — observers cannot distinguish "no-op-True" from
        "toggled-True" via the boolean return alone, but the absence of
        an emit on no-op lets them tell).

        Emit depth (per ``_internal_emit``'s ``_EMIT_DEPTH`` context
        var, ``_MAX_EMIT_DEPTH=5``): a single toggle call adds depth=1
        for the single ``_core/subscription/state_changed`` emit. If an
        observer of that topic chains back into this method, the
        nested emit observes depth=2. Any future fan-out that extends
        this chain MUST stay clear of the depth-5 cap (see Section 12
        stop condition in ``_private/tui_phase2_events_plan.md``).

        Returns:
            True if ``sub_uuid`` was found in the registry — covers both
                the "toggled successfully" path and the "no-op (already
                at target value)" path.
            False if ``sub_uuid`` was not in the registry (pop_plugin
                race or invalid uuid).

        Broadcast failure is logged at DEBUG and NOT propagated; local
        state mutated successfully, peer eventual-consistency via
        heartbeat handles any peer-side drift. Mirrors existing
        ``subscribe_event`` / ``unsubscribe_event`` semantics.
        """
        sub, changed = await self.topic_registry.set_subscription_enabled(
            sub_uuid, enabled
        )
        if sub is None:
            return False
        if not changed:
            return True  # no-op — no broadcast, no emit
        self._logger.info(
            "Subscription %s enabled=%s",
            sub_uuid,
            enabled,
        )
        # Snapshot nm once. Mid-call hot-reload would otherwise leak the
        # broadcast onto a stopped NM; consistent with the pattern used
        # by subscribe_event / unsubscribe_event for the same reason.
        nm = self.network
        if (
            getattr(self, "networking_enabled", False)
            and nm is not None
            and getattr(nm, "is_ready", False)
        ):
            try:
                if enabled:
                    await nm.broadcast_local_sub_added(sub)
                else:
                    await nm.broadcast_local_sub_removed(sub)
            except Exception:
                self._logger.debug(
                    "set_subscription_enabled: broadcast failed",
                    exc_info=True,
                )
        self._internal_emit(
            "_core/subscription/state_changed",
            sub_uuid=sub_uuid,
            enabled=enabled,
            ts=time.time(),
        )
        return True

    async def set_event_enabled(
        self, plugin_name: str, event_id: str, enabled: bool
    ) -> bool:
        """Toggle an event's ``enabled`` flag at runtime. Local-only —
        events are not advertised to peers (publishers don't advertise;
        only subscribers do).

        Async despite no awaited I/O: required so the ``_internal_emit``
        call runs on the loop thread per the observer contract
        (sync observers must NOT be dispatched from non-loop threads —
        ``internal_observe`` docstring at line 764 of this file). TUI
        callers bridge via ``_run_on_main`` like for set_subscription_enabled.

        Emits ``_core/event/state_changed`` so the Events catalogue +
        Live-stream can react. Emit fires only on actual state change
        (idempotent no-op returns True without emitting).

        Idempotency caveat — UNDER CONCURRENT TOGGLE: the
        read-modify-write sequence ``entry.get("enabled") != bool(enabled)``
        → ``entry["enabled"] = bool(enabled)`` is NOT atomic. Two
        concurrent calls with the same target value can both observe
        "needs change" between each other's writes and both emit. For
        the intended TUI single-actor use case this race is
        unobservable; high-concurrency callers should serialize.

        TOCTOU note: a concurrent ``_pop_plugin_under_lock`` between
        ``self.plugins.get`` and the mutation orphans the events dict.
        The mutation succeeds on the orphan but is invisible to future
        dispatch (``publish_event`` / ``request_event`` won't find the
        entry — the plugin's events dict has been GC'd from the
        framework's perspective). The emit fires correctly to other
        observers, but the popped plugin's own observers were already
        cleared by ``_unobserve_plugin`` at pop time, so they won't see
        the emit either. Accepted because: (a) operator clicked toggle
        on an event they could see — pop is rare in normal use,
        (b) guarding with plugin_lock would over-serialize a debug-only
        path.

        Returns:
            True if the ``(plugin, event_id)`` pair exists at call time
                (covers toggled + no-op paths).
            False if either the plugin is not loaded or the event_id is
                not declared on it.
        """
        plugin = self.plugins.get(plugin_name)
        if plugin is None:
            return False
        events = getattr(plugin, "events", None) or {}
        entry = events.get(event_id)
        if entry is None:
            return False
        if bool(entry.get("enabled", True)) == bool(enabled):
            return True  # no-op — no emit
        entry["enabled"] = bool(enabled)
        self._logger.info(
            "Event %s/%s enabled=%s",
            plugin_name,
            event_id,
            bool(enabled),
        )
        self._internal_emit(
            "_core/event/state_changed",
            plugin_name=plugin_name,
            event_id=event_id,
            enabled=bool(enabled),
            ts=time.time(),
        )
        return True
