import os
import re
import socket
import sys

os.environ.setdefault("PYTHONUTF8", "1")  # UTF-8 mode: all open() default to utf-8
if hasattr(sys.stdout, "reconfigure"):  # Reconfigure console streams to UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Windows defaults to ProactorEventLoop, which is incompatible with psycopg3
# async and other libraries. Switch to SelectorEventLoop before any loop is created.
if sys.platform == "win32":
    import asyncio as _asyncio

    _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())

import contextlib
import functools
import importlib
import inspect
import asyncio
import time
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional, Callable, Union, Dict, List
import yaml

# Tracks the sync call chain on each threadpool worker thread.
# Used by execute_sync / _call_endpoint to detect circular sync calls
# that would deadlock the ThreadPoolExecutor.
_sync_call_chain = threading.local()

from exceptions import NetworkRequestException, RequestException
from networking_classes import Node, RemotePlugin
from utils import LogUtil, Request, Plugin, ConfigUtil, GeneratorRequest, Event
from decorators import (
    log_errors,
    handle_errors,
    async_log_errors,
    async_handle_errors,
    async_gen_log_errors,
    async_gen_handle_errors,
    gen_log_errors,
    gen_handle_errors,
)
from networking import NetworkManager, REMOTE_NO_RESULT
from notifier import TopicRegistry, Subscription, SyncDispatcher


# Reserved identifier names — disallowed as plugin names AND endpoint
# access_names because they are framework-reserved keywords used in
# config/system contexts. Future-proof: extend as new framework-reserved
# names are introduced.
_RESERVED_IDENTIFIER_NAMES = frozenset({"system", "general"})


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
_RESERVED_TEMPLATE_VARS = frozenset({"prefix", "plugin_name", "hostname", "plugin_uuid"})

# {var}-style placeholder regex. Matches {name} where name is identifier-style.
_TEMPLATE_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


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


class PluginCore:
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
        self.task_list = []

        self.main_event_loop = None
        self.plugins = {}
        self.plugins_by_uuid = {}
        self.plugin_lock = asyncio.Lock()
        # Dedicated thread pool for sync plugin endpoints — isolated from
        # Python's default executor to prevent deadlock under load.
        # See README "Sync vs Async Plugins: Thread Pool Deadlock Risk".
        self._plugin_executor = ThreadPoolExecutor(
            max_workers=32,
            thread_name_prefix="plugin",
        )
        self._init_tasks = []
        self._running_loop_task = None
        self.network = None
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
        """Ensure initialization tasks are started and await their completion."""
        # Ensure event loop and maintenance task
        if self.main_event_loop is None:
            self.main_event_loop = asyncio.get_running_loop()
            if self.yaml_config.get("general", {}).get("asyncio_debug", False):
                self.main_event_loop.set_debug(True)
                self.main_event_loop.slow_callback_duration = 0.5
        if self._running_loop_task is None:
            self._running_loop_task = asyncio.create_task(self.running_loop())

        # If no init tasks yet, create them (backward-compat with older call sites)
        if not self._init_tasks:
            self._init_tasks.append(asyncio.create_task(self.load_plugins()))

            if getattr(self, "networking_enabled", False):
                if self.network is None:
                    self.network = NetworkManager(
                        self,
                        self._logger.getChild("networking"),
                        node_ips=self.yaml_config.get("networking").get("node_ips", []),
                        discover_nodes=self.yaml_config.get("networking").get(
                            "discover_nodes", False
                        ),
                        direct_discoverable=self.networking_direct_discoverable,
                        auto_discoverable=self.networking_auto_discoverable,
                        port=self.networking_port,
                        secret=getattr(self, "networking_secret", None),
                        cert_file=getattr(self, "networking_cert_file", None),
                        key_file=getattr(self, "networking_key_file", None),
                        pool_size=getattr(self, "networking_pool_size", 5),
                    )
                self._init_tasks.append(asyncio.create_task(self.network.start()))

        if self._init_tasks:
            await asyncio.gather(*self._init_tasks)

        # NOTE: Wait for enabled?

    async def start(self):
        """Initialize background tasks, load plugins, and start networking."""
        self.main_event_loop = asyncio.get_running_loop()
        if self._running_loop_task is None:
            self._running_loop_task = asyncio.create_task(self.running_loop())

        self._init_tasks = [asyncio.create_task(self.load_plugins())]

        if getattr(self, "networking_enabled", False):
            self.network = NetworkManager(
                self,
                self._logger.getChild("networking"),
                node_ips=self.yaml_config.get("networking").get("node_ips", []),
                discover_nodes=self.yaml_config.get("networking").get(
                    "discover_nodes", False
                ),
                direct_discoverable=self.networking_direct_discoverable,
                auto_discoverable=self.networking_auto_discoverable,
                port=self.networking_port,
                secret=getattr(self, "networking_secret", None),
                cert_file=getattr(self, "networking_cert_file", None),
                key_file=getattr(self, "networking_key_file", None),
                pool_size=getattr(self, "networking_pool_size", 5),
            )
            self._init_tasks.append(asyncio.create_task(self.network.start()))

        await self.wait_until_ready()

    async def close(self):
        """Gracefully shutdown: drain requests, disable plugins in reverse order, stop networking."""
        # 1. Stop maintenance loop (no more cleanup cycles)
        self._logger.info("Shutdown: stopping maintenance loop...")
        if self._running_loop_task is not None:
            self._running_loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._running_loop_task
            self._running_loop_task = None

        # 2. Wait for all in-flight request tasks to finish (up to 30s)
        pending = [t for t in self.task_list if not t.done()]
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
        self.task_list = []

        # 2.5. Shutdown the SyncDispatcher (PR3 Stage A, Q17 + C8).
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

        # 3. Disable plugins in REVERSE config order
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
                async with self.plugin_lock:
                    if asyncio.iscoroutinefunction(plugin.on_disable):
                        await asyncio.wait_for(plugin.on_disable(), timeout=30)
                    else:
                        await asyncio.wait_for(
                            self.main_event_loop.run_in_executor(
                                self._plugin_executor, plugin.on_disable
                            ),
                            timeout=30,
                        )
                    plugin.enabled = False
                self._logger.info("Shutdown: %s disabled", name)
            except asyncio.TimeoutError:
                self._logger.warning(
                    "Shutdown: %s on_disable timed out after 30s", name
                )
                plugin.enabled = False
            except Exception as e:
                self._logger.error("Shutdown: %s on_disable failed: %s", name, e)
                plugin.enabled = False

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

    @log_errors
    def load_config_yaml(self, config_path: str):
        self._logger.info(f"Loading config from config_path: {config_path}")
        self.yaml_config: dict = ConfigUtil.load_config(config_path)
        self._logger.info(self.yaml_config)

        ConfigUtil.check_config_integrity(self.yaml_config, self._logger)

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

    async def async_load_config_yaml(self, config_path: str):
        """Async wrapper around load_config_yaml for use outside __init__."""
        self.load_config_yaml(config_path)

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
        """Start all plugin loops."""
        tasks = []
        task_plugins = []
        for plugin in self.plugins.values():
            if not plugin.enabled:
                tasks.append(self._enable_plugin(plugin.plugin_name))
                task_plugins.append(plugin)

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for plugin, result in zip(task_plugins, results):
                if isinstance(result, Exception):
                    self._logger.warning(
                        f'Error occured while enabling plugin with name "{plugin.plugin_name}": {type(result).__name__}: {result}'
                    )
                # task = asyncio.create_task(self._enable_plugin(plugin.plugin_name))
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
            await self.pop_plugin(name)
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
        _KNOWN_PLUGIN_ENTRY_KEYS = frozenset({
            "name", "enabled", "path", "overrides",
            # legacy `arguments:` already handled above with a tailored
            # message; include here so we don't double-warn.
            "arguments",
        })
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
                    _validate_identifier_name(
                        ep_key, context="endpoint access_name"
                    )
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

        # Instantiate with merged arguments
        plugin = plugin_class(
            self._logger.getChild(name),
            self,
            arguments=merged_args,
        )

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
        if prefix_val is None or (isinstance(prefix_val, str) and not prefix_val.strip()):
            prefix_val = name
        if not isinstance(prefix_val, str):
            await warn_config(
                f"prefix must be a string; got {type(prefix_val).__name__}; "
                f"falling back to plugin_name"
            )
            prefix_val = name
        plugin.prefix = prefix_val

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

            entry_dict = {
                "topic": stripped_topic,
                "hosts": entry.get("hosts"),
                "blocked_hosts": entry.get("blocked_hosts"),
                "enabled": (
                    bool(entry["enabled"]) if "enabled" in entry else True
                ),
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
            if target_access is None or not isinstance(target_access, str) or not target_access.strip():
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

            entry_dict = {
                "topic": stripped_topic,
                "target_access_name": target_access,
                "target_plugin": entry.get("target_plugin", name),
                "target_plugin_uuid": entry.get("target_plugin_uuid"),
                "hosts": entry.get("hosts", "any"),
                "blocked_hosts": entry.get("blocked_hosts"),
                "authors": entry.get("authors"),
                "blocked_authors": entry.get("blocked_authors"),
                "enabled": (
                    bool(entry["enabled"]) if "enabled" in entry else True
                ),
            }
            plugin_subs[declared_id] = entry_dict
        plugin.subscriptions = plugin_subs

        async with self.plugin_lock:
            self.plugins[name] = plugin
            # Maintain uuid index if available
            plugin_uuid = getattr(plugin, "plugin_uuid", None)
            if plugin_uuid:
                self.plugins_by_uuid[plugin_uuid] = plugin

        # PR3 Stage B: legacy `topic:` field auto-registration is now
        # done in _register_yaml_subscriptions (called from
        # _enable_plugin) so that disable -> re-enable re-registers
        # the subs. Stage D removes the legacy `topic:` field path
        # entirely.

        self._logger.info(
            f"Successfully loaded plugin: {name} (Version: {plugin.version}, Path: {path})"
        )

    @async_log_errors
    async def pop_plugin(self, plugin_name: str) -> None:
        self._logger.info(f"Popping plugin: {plugin_name}")
        try:
            if plugin_name in list(self.plugins.keys()):
                # Resolve any pending requests targeting this plugin
                async with self.request_lock:
                    for req in self.requests.values():
                        if req.target_plugin == plugin_name and not req._future.done():
                            await req.set_result(
                                f"Plugin {plugin_name} was unloaded while request was pending",
                                error=True,
                            )

                if self.plugins[plugin_name].enabled:
                    await self._disable_plugin(plugin_name)
                async with self.plugin_lock:
                    plugin = self.plugins.pop(plugin_name)
                    # Remove from uuid index
                    plugin_uuid = getattr(plugin, "plugin_uuid", None)
                    if plugin_uuid and plugin_uuid in self.plugins_by_uuid:
                        self.plugins_by_uuid.pop(plugin_uuid, None)
                    # Remove all topic subscriptions for this plugin
                    if plugin_uuid:
                        await self.topic_registry.unsubscribe_plugin(plugin_uuid)
                        LogUtil.clear_logger_levels_owned_by(plugin_name, plugin_uuid)
            else:
                self._logger.warning(f'Plugin with name "{plugin_name}" doesnt exist')
        except Exception as error:
            raise Exception(f'Error while popping plugin "{plugin_name}": {error}')

    @async_log_errors
    async def purge_plugins(self):
        self._logger.info("Purging plugins")
        try:
            for plugin_name in list(self.plugins.keys()):
                await self._disable_plugin(plugin_name)
            async with self.plugin_lock:
                for plugin_name, plugin in self.plugins.items():
                    plugin_uuid = getattr(plugin, "plugin_uuid", None)
                    if plugin_uuid:
                        await self.topic_registry.unsubscribe_plugin(plugin_uuid)
                        LogUtil.clear_logger_levels_owned_by(plugin_name, plugin_uuid)
                self.plugins.clear()
                self.plugins_by_uuid.clear()
            self._logger.info("Purged all plugins")
        except Exception as error:
            raise Exception(f"Error while purging plugins: {error}")

    @async_log_errors
    async def purge_plugins_except(self, excluded_names: List[str]):
        """Purge all plugins except those in the excluded_names list."""
        self._logger.info(f"Purging plugins except: {excluded_names}")
        try:
            plugins_to_purge = [
                name for name in list(self.plugins.keys()) if name not in excluded_names
            ]
            for plugin_name in plugins_to_purge:
                await self._disable_plugin(plugin_name)
            async with self.plugin_lock:
                for plugin_name in plugins_to_purge:
                    plugin = self.plugins.pop(plugin_name, None)
                    if plugin:
                        plugin_uuid = getattr(plugin, "plugin_uuid", None)
                        if plugin_uuid:
                            if plugin_uuid in self.plugins_by_uuid:
                                self.plugins_by_uuid.pop(plugin_uuid, None)
                            await self.topic_registry.unsubscribe_plugin(plugin_uuid)
                            LogUtil.clear_logger_levels_owned_by(
                                plugin_name, plugin_uuid
                            )
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
        """Gracefully shutdown the system by closing PluginCore."""
        self._logger.info("Initiating graceful shutdown...")
        await self.close()
        # Note: Stopping the event loop should be handled by the main application

    @async_handle_errors(None)
    async def _enable_plugin(self, plugin_name: str):
        """Method to enable a plugin.

        PR3 Stage B (Q23 + C15): YAML-declared subscriptions register
        BEFORE user.on_enable runs. So plugin code starts with subs
        already live; events arriving during on_enable are dispatched
        to handlers (which exist by definition — methods on the plugin
        class).
        """
        async with self.plugin_lock:
            plugin = self.plugins[plugin_name]
            if not plugin.enabled:
                # Register YAML subscriptions FIRST. Disabled subs (Q13)
                # skipped at registration; their declared_id stays in
                # plugin.subscriptions so override-time toggling works.
                await self._register_yaml_subscriptions(plugin)

                # Flip plugin.enabled=True BEFORE user.on_enable per Q23
                # + Q11: handlers must be callable (find_endpoint requires
                # plugin.enabled) so self-publish-from-on_enable works.
                # Roll back enabled + unregister subs on raise.
                plugin.enabled = True
                try:
                    if asyncio.iscoroutinefunction(plugin.on_enable):
                        await plugin.on_enable()
                    else:
                        await self.main_event_loop.run_in_executor(
                            self._plugin_executor, plugin.on_enable
                        )
                except Exception:
                    plugin.enabled = False
                    await self._unregister_plugin_subscriptions(plugin)
                    raise

    @async_log_errors
    async def _disable_plugin(self, plugin_name: str):
        """Disable a plugin by calling its on_disable and setting enabled=False.

        PR3 Stage B (C15): YAML + runtime subs are unregistered AFTER
        user.on_disable returns. User code can publish/receive events
        during shutdown teardown.
        """
        async with self.plugin_lock:
            plugin = self.plugins[plugin_name]
            if plugin.enabled:
                try:
                    if asyncio.iscoroutinefunction(plugin.on_disable):
                        await plugin.on_disable()
                    else:
                        await self.main_event_loop.run_in_executor(
                            self._plugin_executor, plugin.on_disable
                        )
                finally:
                    # Unregister all subs (YAML + runtime) regardless of
                    # whether on_disable raised. Symmetric with the
                    # rollback in _enable_plugin.
                    await self._unregister_plugin_subscriptions(plugin)
                    plugin.enabled = False

    async def _register_yaml_subscriptions(self, plugin: Plugin) -> None:
        """Register every YAML-declared subscription for ``plugin`` per
        Q23 + C15 + LOCKED A subscriptions: shape.

        Stage B also re-applies the legacy ``topic:`` field
        auto-registration here (Stage D removes that path) — moving
        from load-time to on_enable-time so disable -> re-enable
        cycles re-register the subs naturally.

        Disabled subs (``enabled: false``) get a Subscription with
        ``enabled=False`` so they live in the registry (visible to
        introspection / future advertisement) but are skipped by
        find_all/find_first matching.
        """
        # PR3 subscriptions: section.
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

        # Legacy `topic:` field on endpoints — Stage B keeps alive,
        # Stage D removes.
        endpoints_dict = getattr(plugin, "endpoints", {}) or {}
        if isinstance(endpoints_dict, dict):
            for ep_key, endpoint in endpoints_dict.items():
                if not isinstance(endpoint, dict):
                    continue
                topic = endpoint.get("topic")
                if topic and isinstance(topic, str) and topic.strip():
                    sub_uuid = await self.topic_registry.subscribe(
                        topic_pattern=topic.strip(),
                        plugin_name=plugin.plugin_name,
                        plugin_uuid=plugin.plugin_uuid,
                        endpoint_access_name=ep_key,
                        config_driven=True,
                    )
                    plugin._sub_uuids.append(sub_uuid)

    async def _unregister_plugin_subscriptions(self, plugin: Plugin) -> None:
        """Unregister every sub (YAML + runtime) for ``plugin`` at
        on_disable end (C15). Uses unsubscribe_plugin which removes by
        plugin_uuid (covers BOTH YAML subs registered via the wrapper
        AND runtime subs registered via Plugin.subscribe — both share
        plugin_uuid). plugin._sub_uuids is cleared as a side-effect.
        """
        plugin_uuid = getattr(plugin, "plugin_uuid", None)
        if plugin_uuid:
            await self.topic_registry.unsubscribe_plugin(plugin_uuid)
        plugin._sub_uuids = []

    @async_handle_errors(None)
    async def _reload_plugin(self, plugin_name: str):
        """Reload a plugin by disabling, removing, re-loading from config, and re-enabling."""
        # Capture whether it was enabled before reload
        previously_enabled = False
        if plugin_name in self.plugins:
            previously_enabled = self.plugins[plugin_name].enabled

        await self.pop_plugin(plugin_name)

        entry = next(
            (
                p
                for p in self.yaml_config.get("plugins", [])
                if p.get("name") == plugin_name
            ),
            None,
        )
        if not entry:
            raise Exception(f"Plugin '{plugin_name}' not found in config for reload")

        await self.load_plugin_with_conf(entry)
        if previously_enabled:
            await self._enable_plugin(plugin_name)

    @contextlib.asynccontextmanager
    async def request_context_async(self, request: Request):
        """Async context manager to handle requests."""
        try:
            result, error, timed_out = await request.wait_for_result_async()
            if error:
                raise Exception(f"Request {request.id} failed: {request.result}")
            yield result
        finally:
            await request.set_collected()

    @contextlib.contextmanager
    def request_context_sync(self, request: Request):
        """Sync context manager to handle requests."""
        try:
            result = request.get_result_sync()
            if request.error:
                raise Exception(f"Request failed: {request.result}")
            yield result
        finally:
            asyncio.run_coroutine_threadsafe(
                request.set_collected(), self.main_event_loop
            )

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
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Request:
        """Create a new request asynchronously."""

        if author_host == None:
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

        task = asyncio.create_task(self._process_request(request))
        self.task_list.append(task)

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
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
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
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> GeneratorRequest:
        """Create a new request asynchronously."""

        if author_host == None:
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

        task = asyncio.create_task(self._process_request_stream(request))
        self.task_list.append(task)

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
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
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

        if self.networking_enabled:
            for node in self.network.nodes:
                node: Node
                if node.enabled:
                    result = await self.network.node_get_tagged_endpoints(node.IP, tag)
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
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        plugin_uuid: Optional[str] = None,
        requester_id: Optional[str] = None,
        target_plugin: Optional[str] = None,
    ) -> Optional[tuple[Union[Plugin, RemotePlugin], dict, Optional[Node]]]:
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
            Tuple of (plugin, endpoint_dict, node) or None if not found
            For local: (Plugin, endpoint_dict, None)
            For remote: (RemotePlugin, endpoint_dict, Node)
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
        if getattr(self, "networking_enabled", False) and _other_than_local():

            for node in self.network.nodes:
                if not (node.enabled and await node.is_alive()):
                    continue

                if not _matches_remote_node(node.hostname) or _is_remote_node_blocked(
                    node.hostname
                ):
                    continue

                # Check remote node for endpoint
                result = await self.network.node_has_endpoint(
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
        """Process a request by invoking the target plugin method."""
        try:
            plugin_name = request.target_plugin
            function_name = request.target_method

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
                result = await self.network.execute_remote(
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
        """Process a request by invoking the target plugin method."""
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

            host_label = (
                f"(local) {self.hostname}"
                if isinstance(plugin, Plugin)
                else f"{node.IP}#{node.hostname}"
            )
            self._logger.debug(
                f"Found {plugin_name} (ID: {plugin.plugin_uuid}) for Request with ID {request.id} on host {host_label}"
            )

            if isinstance(plugin, RemotePlugin):
                async for result in self.network.execute_remote_stream(
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
                    elif request.args == None:
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
                    elif request.args == None:
                        generator = func()
                    else:
                        generator = func(request.args)

                    sentinel = object()
                    while True:
                        result = await asyncio.to_thread(next, generator, sentinel)
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

    @async_handle_errors(None)
    async def _set_request_result(
        self, request: Request, result: Any, error: bool = False
    ) -> None:
        """Set the result of a request."""
        if isinstance(result, asyncio.Future):
            result = await result
        await request.set_result(result, error)

    @async_handle_errors(None)
    async def _set_gen_request_result(
        self, request: GeneratorRequest, result: Any = None, error: bool = False
    ) -> None:
        """Set the result of a request."""

        await request.set_result(result, error)

    async def running_loop(self):
        """Maintenance loop that cleans up tasks and requests."""
        while True:
            self.task_list = [t for t in self.task_list if not t.done()]
            await self.cleanup_requests()
            await asyncio.sleep(10)

    @async_log_errors
    async def cleanup_requests(self):
        """Remove collected requests older than 10 seconds.

        Uses created_at as the fallback when finished_at is unset — a few exit
        paths in Request.wait_for_result_async finalize state without setting
        finished_at (timeout/exception branches), and the previous filter
        (`finished_at is None` → keep forever) was leaking those forever.
        Falling back to created_at means even un-finalized-but-collected
        requests get reaped 10 s after creation.
        """
        _timer = time.time() - 10
        async with self.request_lock:
            self.requests = {
                rid: req
                for rid, req in self.requests.items()
                if not req.collected or (req.finished_at or req.created_at) > _timer
            }

    def _validate_host_args(self, hosts, blocked_hosts):
        """Normalize hosts/blocked_hosts and warn on redundant combos.

        Returns (hosts, blocked_hosts) ready to pass to find_endpoint.
        Raises ValueError on structural input errors. Idempotent — safe to
        call on already-normalized values.
        """
        hosts = _normalize_hosts(hosts, param_name="hosts", default="local")
        blocked_hosts = _normalize_hosts(
            blocked_hosts, param_name="blocked_hosts", default=None,
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
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
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
            # Mark for cleanup. Runs on normal return, RequestException, AND
            # CancelledError — without this, a cancelled caller would leave the
            # Request lingering in self.requests forever.
            await request.set_collected()

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
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
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
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
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
            await request.set_collected()

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
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
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
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
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

    async def _resolve_subscription(self, sub: Subscription) -> tuple:
        """
        Resolve a Subscription to (plugin_name, access_name) for use with execute().

        For config-driven subs, the endpoint access_name is already known.
        For code-driven subs, a temporary endpoint is not needed — we call the
        handler directly via _call_endpoint.
        """
        return sub.plugin_name, sub.endpoint_access_name, sub.handler

    @async_log_errors
    async def notify(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        author: str = "system",
        author_id: str = "system",
    ) -> int:
        """
        Fire-and-forget publish to a topic. All matching subscribers are called
        concurrently; errors are logged but do not propagate.

        Returns the number of subscribers that were called.

        .. deprecated::
            The notifier subsystem is being redesigned. List-form
            hosts/blocked_hosts is rejected on this legacy path; full list
            support arrives with the rework. See notes.txt.
        """
        warnings.warn(
            "PluginCore.notify() uses the legacy notifier subsystem which is "
            "being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)
        if isinstance(hosts, list) or isinstance(blocked_hosts, list):
            raise ValueError(
                "Legacy notifier methods do not yet support list-form hosts/"
                "blocked_hosts. Use a string keyword ('any'/'local'/'remote') "
                "or a single hostname. Full list support arrives with the "
                "notifier rework."
            )

        subs = await self.topic_registry.find_all(topic)
        if not subs:
            self._logger.debug(f"Notify '{topic}': no subscribers")
            return 0

        called = 0

        async def _call_sub(sub: Subscription):
            nonlocal called
            try:
                plugin_name, access_name, handler = await self._resolve_subscription(
                    sub
                )

                if handler is not None:
                    # Code-driven: call handler directly
                    await self._call_endpoint(handler, args)
                elif access_name:
                    # Config-driven: route through execute()
                    await self.execute(
                        plugin_name,
                        access_name,
                        args,
                        plugin_uuid=sub.plugin_uuid,
                        hosts="local",
                        author=author,
                        author_id=author_id,
                    )
                called += 1
            except Exception as e:
                self._logger.warning(
                    f"Notify '{topic}': subscriber {sub} raised {type(e).__name__}: {e}"
                )

        # Local subscribers
        local_subs = [s for s in subs if s.plugin_uuid in self.plugins_by_uuid]
        local_targeted = hosts in ("any", "local", self.hostname)
        local_blocked = (
            blocked_hosts in ("any", "local", self.hostname)
            if blocked_hosts
            else False
        )
        if local_targeted and not local_blocked and local_subs:
            await asyncio.gather(*[_call_sub(s) for s in local_subs])

        # Remote notify (broadcast to all nodes)
        remote_needed = hosts in ("any", "remote") or (
            hosts not in ("local", self.hostname)
        )
        if remote_needed and getattr(self, "networking_enabled", False):
            for node in self.network.nodes:
                if not (node.enabled and await node.is_alive()):
                    continue
                if hosts not in ("any", "remote") and node.hostname != hosts:
                    continue
                if blocked_hosts is not None and (
                    blocked_hosts in ("any", "remote")
                    or blocked_hosts == node.hostname
                ):
                    continue
                try:
                    await self.network.notify_remote(
                        node.IP,
                        topic,
                        args,
                        author,
                        author_id,
                    )
                    called += 1  # Count remote dispatch as one call
                except Exception as e:
                    self._logger.warning(
                        f"Notify '{topic}': remote node {node.hostname} failed: {e}"
                    )

        return called

    @log_errors
    def notify_sync(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        author: str = "system",
        author_id: str = "system",
    ) -> int:
        """Synchronous variant of notify().

        .. deprecated::
            See notes.txt — the notifier subsystem is being redesigned.
        """
        warnings.warn(
            "PluginCore.notify_sync() uses the legacy notifier subsystem "
            "which is being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        future = asyncio.run_coroutine_threadsafe(
            self.notify(topic, args, hosts, blocked_hosts, author, author_id),
            self.main_event_loop,
        )
        return future.result()

    @async_log_errors
    async def request_topic(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        author: str = "system",
        author_id: str = "system",
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Request-by-topic: find the first matching handler and return its result.
        Same discovery logic as execute() with hosts="any" (local first).

        .. deprecated::
            See notes.txt — the notifier subsystem is being redesigned.
        """
        warnings.warn(
            "PluginCore.request_topic() uses the legacy notifier subsystem "
            "which is being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)
        if isinstance(hosts, list) or isinstance(blocked_hosts, list):
            raise ValueError(
                "Legacy notifier methods do not yet support list-form hosts/"
                "blocked_hosts. Use a string keyword ('any'/'local'/'remote') "
                "or a single hostname. Full list support arrives with the "
                "notifier rework."
            )

        # Try local subscription first (only if hosts includes local and not blocked)
        sub = None
        local_targeted = hosts in ("any", "local", self.hostname)
        local_blocked = (
            blocked_hosts in ("any", "local", self.hostname)
            if blocked_hosts
            else False
        )
        if local_targeted and not local_blocked:
            sub = await self.topic_registry.find_first(topic)

        if sub is not None:
            plugin_name, access_name, handler = await self._resolve_subscription(sub)

            if handler is not None:
                return await self._call_endpoint(handler, args)

            return await self.execute(
                plugin_name,
                access_name,
                args,
                plugin_uuid=sub.plugin_uuid,
                hosts="local",
                author=author,
                author_id=author_id,
                timeout=timeout,
            )

        # No local match (or hosts excludes local) — check remote
        if getattr(self, "networking_enabled", False) and hosts not in (
            "local",
            self.hostname,
        ):
            for node in self.network.nodes:
                if not (node.enabled and await node.is_alive()):
                    continue
                if hosts not in ("any", "remote") and node.hostname != hosts:
                    continue
                if blocked_hosts is not None and (
                    blocked_hosts in ("any", "remote")
                    or blocked_hosts == node.hostname
                ):
                    continue
                try:
                    result = await self.network.request_topic_remote(
                        node.IP,
                        topic,
                        args,
                        author,
                        author_id,
                        timeout,
                    )
                    if result is not REMOTE_NO_RESULT:
                        return result
                except Exception as e:
                    self._logger.warning(
                        f"request_topic '{topic}': remote {node.hostname} failed: {e}"
                    )

        raise RequestException(f"No handler found for topic '{topic}'")

    @log_errors
    def request_topic_sync(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        author: str = "system",
        author_id: str = "system",
        timeout: Optional[float] = None,
    ) -> Any:
        """Synchronous variant of request_topic().

        .. deprecated::
            See notes.txt — the notifier subsystem is being redesigned.
        """
        warnings.warn(
            "PluginCore.request_topic_sync() uses the legacy notifier subsystem "
            "which is being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        chain = getattr(_sync_call_chain, "chain", ())
        target = f"topic:{topic}"
        if target in chain:
            raise RequestException(
                f"Circular sync call: {' -> '.join(chain)} -> {target}"
            )

        future = asyncio.run_coroutine_threadsafe(
            self.request_topic(
                topic, args, hosts, blocked_hosts, author, author_id, timeout
            ),
            self.main_event_loop,
        )
        return future.result()

    @async_gen_log_errors
    async def request_topic_stream(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        author: str = "system",
        author_id: str = "system",
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Request-by-topic with streaming: find the first matching handler
        and yield its results.

        .. deprecated::
            See notes.txt — the notifier subsystem is being redesigned.
        """
        warnings.warn(
            "PluginCore.request_topic_stream() uses the legacy notifier "
            "subsystem which is being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)
        if isinstance(hosts, list) or isinstance(blocked_hosts, list):
            raise ValueError(
                "Legacy notifier methods do not yet support list-form hosts/"
                "blocked_hosts. Use a string keyword ('any'/'local'/'remote') "
                "or a single hostname. Full list support arrives with the "
                "notifier rework."
            )

        sub = None
        local_targeted = hosts in ("any", "local", self.hostname)
        local_blocked = (
            blocked_hosts in ("any", "local", self.hostname)
            if blocked_hosts
            else False
        )
        if local_targeted and not local_blocked:
            sub = await self.topic_registry.find_first(topic)

        if sub is None:
            if getattr(self, "networking_enabled", False) and hosts not in (
                "local",
                self.hostname,
            ):
                for node in self.network.nodes:
                    if not (node.enabled and await node.is_alive()):
                        continue
                    if hosts not in ("any", "remote") and node.hostname != hosts:
                        continue
                    if blocked_hosts is not None and (
                        blocked_hosts in ("any", "remote")
                        or blocked_hosts == node.hostname
                    ):
                        continue
                    try:
                        async for chunk in self.network.request_topic_stream_remote(
                            node.IP,
                            topic,
                            args,
                            author,
                            author_id,
                            timeout,
                        ):
                            yield chunk
                        return
                    except Exception as e:
                        self._logger.warning(
                            f"request_topic_stream '{topic}': remote {node.hostname} failed: {e}"
                        )
            raise RequestException(f"No handler found for topic '{topic}'")

        plugin_name, access_name, handler = await self._resolve_subscription(sub)

        if handler is not None:
            # Code-driven handler — must be an async generator
            if inspect.isasyncgenfunction(handler):
                if isinstance(args, tuple):
                    async for chunk in handler(*args):
                        yield chunk
                elif isinstance(args, dict):
                    async for chunk in handler(**args):
                        yield chunk
                elif args is None:
                    async for chunk in handler():
                        yield chunk
                else:
                    async for chunk in handler(args):
                        yield chunk
            elif inspect.isgeneratorfunction(handler):
                if isinstance(args, tuple):
                    gen = handler(*args)
                elif isinstance(args, dict):
                    gen = handler(**args)
                elif args is None:
                    gen = handler()
                else:
                    gen = handler(args)
                sentinel = object()
                while True:
                    chunk = await asyncio.to_thread(next, gen, sentinel)
                    if chunk is sentinel:
                        break
                    yield chunk
            else:
                raise RequestException(
                    f"Handler for topic '{topic}' is not a generator function"
                )
        else:
            async for chunk in self.execute_stream(
                plugin_name,
                access_name,
                args,
                plugin_uuid=sub.plugin_uuid,
                hosts="local",
                author=author,
                author_id=author_id,
                timeout=timeout,
            ):
                yield chunk

    @gen_log_errors
    def request_topic_stream_sync(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        author: str = "system",
        author_id: str = "system",
        timeout: Optional[float] = None,
    ) -> Any:
        """Synchronous streaming variant of request_topic().

        .. deprecated::
            See notes.txt — the notifier subsystem is being redesigned.
        """
        warnings.warn(
            "PluginCore.request_topic_stream_sync() uses the legacy notifier "
            "subsystem which is being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)
        if isinstance(hosts, list) or isinstance(blocked_hosts, list):
            raise ValueError(
                "Legacy notifier methods do not yet support list-form hosts/"
                "blocked_hosts. Use a string keyword ('any'/'local'/'remote') "
                "or a single hostname. Full list support arrives with the "
                "notifier rework."
            )

        sub = asyncio.run_coroutine_threadsafe(
            self.topic_registry.find_first(topic),
            self.main_event_loop,
        ).result()

        if sub is None:
            raise RequestException(f"No handler found for topic '{topic}'")

        plugin_name, access_name, handler = asyncio.run_coroutine_threadsafe(
            self._resolve_subscription(sub),
            self.main_event_loop,
        ).result()

        if handler is not None:
            if inspect.isgeneratorfunction(handler):
                if isinstance(args, tuple):
                    yield from handler(*args)
                elif isinstance(args, dict):
                    yield from handler(**args)
                elif args is None:
                    yield from handler()
                else:
                    yield from handler(args)
            else:
                raise RequestException(
                    f"Handler for topic '{topic}' is not a sync generator"
                )
        else:
            for chunk in self.execute_stream_sync(
                plugin_name,
                access_name,
                args,
                plugin_uuid=sub.plugin_uuid,
                hosts="local",
                author=author,
                author_id=author_id,
                timeout=timeout,
            ):
                yield chunk

    async def subscribe(
        self,
        topic: str,
        plugin_name: str,
        plugin_uuid: str,
        endpoint_access_name: Optional[str] = None,
        handler: Optional[Callable] = None,
        config_driven: bool = False,
    ) -> str:
        """Register a topic subscription. Returns subscription ID.

        .. deprecated::
            The notifier subsystem is being redesigned. ``handler=`` will be
            removed and subscriptions will require an endpoint access_name
            target. See notes.txt.
        """
        warnings.warn(
            "PluginCore.subscribe() uses the legacy notifier subsystem which "
            "is being redesigned (handler= will be removed). See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.topic_registry.subscribe(
            topic_pattern=topic,
            plugin_name=plugin_name,
            plugin_uuid=plugin_uuid,
            endpoint_access_name=endpoint_access_name,
            handler=handler,
            config_driven=config_driven,
        )

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a topic subscription by ID.

        .. deprecated::
            See notes.txt — the notifier subsystem is being redesigned.
        """
        warnings.warn(
            "PluginCore.unsubscribe() uses the legacy notifier subsystem "
            "which is being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
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
                return "local" in val or self.hostname in val
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
                raise ValueError(
                    f"topic_vars[{k!r}] is empty string (LOCKED L #5)"
                )
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
                event_id, topic_template, tv,
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
                event_id, sorted(extra), topic_template,
            )

        # Substitute. Use the same regex helper to keep behavior consistent.
        def _sub(match: re.Match) -> str:
            name = match.group(1)
            if name in tv:
                return tv[name]
            # Should be unreachable given the missing-key check above —
            # defensive guard.
            raise ValueError(
                f"event {event_id!r} unresolved placeholder {{{name}}}"
            )

        resolved = _TEMPLATE_VAR_RE.sub(_sub, topic_template)

        # Post-resolution checks (Q15 reject empty + Q16 strip slashes).
        stripped_topic = resolved.strip("/")
        if not stripped_topic.strip():
            raise ValueError(
                f"event {event_id!r} resolved topic empty (Q15)"
            )

        # Re-validate post-resolution (no embedded * mid-segment, no
        # empty middle segments). Wildcards forbidden in events. Per
        # LOCKED L's "ORDER OF OPERATIONS" + "FILTER LOOKUP" step 6.
        try:
            stripped_topic = _validate_topic_static(
                stripped_topic,
                context=f"event {event_id!r} resolved topic",
                allow_wildcards=False,
            )
        except ValueError:
            raise

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
    ) -> int:
        """Publish an event (1:N fire-and-forget).

        Per PR3 PLAN F + LOCKED L FILTER LOOKUP. Returns the count of
        local subs the publisher targeted (post-filter). Stage B is
        LOCAL-only; remote dispatch lands in Stage C.

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
        eff_hosts = hosts if hosts is not None else event_entry.get("hosts")
        eff_blocked = (
            blocked_hosts
            if blocked_hosts is not None
            else event_entry.get("blocked_hosts")
        )
        # Stage C will read eff_hosts/eff_blocked for the peer-level
        # filter (PR3 PLAN F step 5a). Stage B uses them ONLY to gate
        # whether local fan-out happens at all (e.g. hosts="remote"
        # means peer-only, no local delivery).

        # Publisher-level gate: skip local fan-out if publisher's
        # hosts/blocked_hosts exclude local delivery.
        if not self._publisher_targets_local(eff_hosts, eff_blocked):
            if publisher.verbose_notifier:
                self._logger.debug(
                    "publish_event %s topic=%r: publisher hosts=%r "
                    "blocked_hosts=%r excludes local fan-out",
                    event_id, resolved_topic, eff_hosts, eff_blocked,
                )
            return 0

        # Step 4: local fan-out — find all local subs matching resolved
        # topic. find_all returns insertion order (LOCKED C).
        all_subs = await self.topic_registry.find_all(resolved_topic)
        local_subs = [
            s for s in all_subs if s.plugin_uuid in self.plugins_by_uuid
        ]

        survivors = [
            s for s in local_subs
            if self._sub_accepts_local(s)
            and self._sub_accepts_author(s, publisher.plugin_name)
        ]

        if publisher.verbose_notifier:
            self._logger.debug(
                "publish_event %s topic=%r matched %d local sub(s) "
                "(of %d total subs)",
                event_id, resolved_topic, len(survivors), len(local_subs),
            )

        # Per-sub fan-out tasks. Each gets its own Request with
        # kind="publish_event", hosts="local" (C19), and
        # requester_id=sub.plugin_uuid (C18).
        now_ts = time.time()
        for sub in survivors:
            await self._fanout_sub(
                sub=sub,
                publisher=publisher,
                resolved_topic=resolved_topic,
                payload=payload,
                kind="publish_event",
                timestamp=now_ts,
                timeout=None,
            )

        return len(survivors)

    async def _fanout_sub(
        self,
        *,
        sub: Subscription,
        publisher: Plugin,
        resolved_topic: str,
        payload: Any,
        kind: str,
        timestamp: float,
        timeout: Optional[float],
    ) -> Optional[Request]:
        """Build a per-sub Request and spawn its dispatch task (publish
        path) or build + return without spawning (request path; caller
        awaits it).

        Stage B always returns the Request. publish_event ignores the
        return value (fire-and-forget). request_event awaits it.
        """
        author_host = self.hostname
        request = Request(
            author_host=author_host,
            plugin=sub.target_plugin or sub.plugin_name,
            method=sub.target_access_name,
            args=payload,
            plugin_uuid=sub.target_plugin_uuid,
            target_hosts="local",  # C19
            blocked_hosts=None,
            author=publisher.plugin_name,
            author_id=publisher.plugin_uuid,
            timeout=timeout,
            request_id=None,
            event_loop=self.main_event_loop,
            kind=kind,
            topic=resolved_topic,
            origin_subscription_id=sub.declared_id or sub.sub_uuid,
            timestamp=timestamp,
            requester_id=sub.plugin_uuid,  # C18
        )

        # C10: propagate the publisher's sync call chain through fan-out
        # so cross-pool cycle detection still works when a sync subscriber
        # handler eventually re-enters execute_sync / publish_event_sync /
        # etc. Stage A's _tracked_event wrapper reads request._call_chain.
        existing_chain = getattr(_sync_call_chain, "chain", ())
        request._call_chain = tuple(existing_chain) + (
            (publisher.plugin_uuid, kind),
        )

        async with self.request_lock:
            self.requests[request.id] = request

        async def _run_and_collect():
            try:
                await self._process_request(request)
            finally:
                # Q12 fix: mark for cleanup so cleanup_requests doesn't
                # leak fan-out Requests.
                await request.set_collected()

        task = asyncio.create_task(_run_and_collect())
        self.task_list.append(task)

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
        Pre-start guard fires inside the Plugin wrapper (Q1)."""
        future = asyncio.run_coroutine_threadsafe(
            self.publish_event(
                publisher, event_id, payload, topic_vars, hosts, blocked_hosts
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
            raise RequestException(
                f"event {event_id!r} disabled (C2)"
            )

        if payload is None:
            payload = {}

        resolved_topic, _ = self._resolve_topic_for_event(
            publisher, event_id, topic_vars, event_entry=event_entry
        )

        # Publisher-level hosts gate: if hosts="remote" or excludes
        # local, request_event has no local route and Stage B is
        # LOCAL-only — raise rather than silently fail (caller awaits
        # a result).
        eff_hosts = hosts if hosts is not None else event_entry.get("hosts")
        eff_blocked = (
            blocked_hosts
            if blocked_hosts is not None
            else event_entry.get("blocked_hosts")
        )
        if not self._publisher_targets_local(eff_hosts, eff_blocked):
            raise RequestException(
                f"request_event {event_id!r}: publisher hosts={eff_hosts!r} "
                f"excludes local; remote dispatch lands in Stage C"
            )

        # Capture timestamp once so all per-sub Requests built off this
        # call see consistent epoch seconds (consistency with
        # publish_event).
        now_ts = time.time()

        # Find first matching LOCAL sub (insertion order). Apply the
        # same sub-level host/author filter as publish_event so subs
        # with hosts="remote" or blocked_authors filtering us out are
        # skipped (LOCKED H).
        all_subs = await self.topic_registry.find_all(resolved_topic)
        local_match = next(
            (
                s for s in all_subs
                if s.plugin_uuid in self.plugins_by_uuid
                and self._sub_accepts_local(s)
                and self._sub_accepts_author(s, publisher.plugin_name)
            ),
            None,
        )

        if local_match is None:
            raise RequestException(
                f"request_event {event_id!r}: no subscriber matches resolved "
                f"topic {resolved_topic!r}"
            )

        request = await self._fanout_sub(
            sub=local_match,
            publisher=publisher,
            resolved_topic=resolved_topic,
            payload=payload,
            kind="request_event",
            timestamp=now_ts,
            timeout=timeout,
        )

        result, error, _ = await request.wait_for_result_async()
        if error:
            raise RequestException(result)
        return result

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
        """Sync variant of request_event (C16)."""
        future = asyncio.run_coroutine_threadsafe(
            self.request_event(
                publisher, event_id, payload, topic_vars, hosts,
                blocked_hosts, timeout,
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
    ) -> Any:
        """Streaming variant of request_event. First yield is wrapped
        in Event metadata (LOCKED I); subsequent yields raw."""
        # C1 ORDER OF OPERATIONS: lookup → enabled → payload → resolve.
        event_entry = self._lookup_event(publisher, event_id)
        if not event_entry.get("enabled", True):
            raise RequestException(
                f"event {event_id!r} disabled (C2)"
            )
        if payload is None:
            payload = {}
        resolved_topic, _ = self._resolve_topic_for_event(
            publisher, event_id, topic_vars, event_entry=event_entry
        )

        # Publisher-level hosts gate (same as request_event).
        eff_hosts = hosts if hosts is not None else event_entry.get("hosts")
        eff_blocked = (
            blocked_hosts
            if blocked_hosts is not None
            else event_entry.get("blocked_hosts")
        )
        if not self._publisher_targets_local(eff_hosts, eff_blocked):
            raise RequestException(
                f"request_event_stream {event_id!r}: publisher hosts="
                f"{eff_hosts!r} excludes local; remote dispatch lands in "
                f"Stage C"
            )

        # Capture timestamp once (consistency with publish_event /
        # request_event).
        now_ts = time.time()

        # Apply the same sub-level filter as publish_event /
        # request_event so subs with hosts="remote" or blocked_authors
        # filtering us out are skipped (LOCKED H).
        all_subs = await self.topic_registry.find_all(resolved_topic)
        local_match = next(
            (
                s for s in all_subs
                if s.plugin_uuid in self.plugins_by_uuid
                and self._sub_accepts_local(s)
                and self._sub_accepts_author(s, publisher.plugin_name)
            ),
            None,
        )
        if local_match is None:
            raise RequestException(
                f"request_event_stream {event_id!r}: no subscriber matches "
                f"resolved topic {resolved_topic!r}"
            )

        # Route through find_endpoint so the C18 accessible_by_other_plugins
        # access check applies on the streaming path too. Pass
        # requester_id=local_match.plugin_uuid (the SUB OWNER's identity)
        # so cross-plugin subs to private endpoints are denied
        # consistently with the non-streaming request_event path.
        found = await self.find_endpoint(
            access_name=local_match.target_access_name,
            hosts="local",
            plugin_uuid=local_match.target_plugin_uuid,
            requester_id=local_match.plugin_uuid,
            target_plugin=local_match.target_plugin,
        )
        if found is None:
            raise RequestException(
                f"request_event_stream {event_id!r}: target endpoint "
                f"{local_match.target_access_name!r} not found on "
                f"{local_match.target_plugin!r} (or access denied per C18)"
            )
        target_plugin, endpoint, _node = found
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
            subscription_id=local_match.declared_id or local_match.sub_uuid,
            timestamp=now_ts,
        )

        first = True
        # The handler is a plain endpoint generator — pass the Event as
        # single positional argument, matching the non-streaming
        # subscriber-handler convention (LOCKED I).
        if inspect.isasyncgenfunction(func):
            async for chunk in func(event_meta):
                if first:
                    first = False
                    # Yield Event-shaped wrapper: event with payload =
                    # first chunk.
                    wrapped = Event(
                        topic=resolved_topic,
                        payload=chunk,
                        author=publisher.plugin_name,
                        author_id=publisher.plugin_uuid,
                        author_host=self.hostname,
                        subscription_id=event_meta.subscription_id,
                        timestamp=event_meta.timestamp,
                    )
                    yield wrapped
                else:
                    yield chunk
        else:
            sentinel = object()
            gen = func(event_meta)
            loop = asyncio.get_running_loop()
            while True:
                # Sync handler iteration runs on the SyncDispatcher pool
                # per Q17 + C3 (NOT asyncio.to_thread, which uses the
                # default executor and breaks the executor isolation
                # invariant).
                chunk = await loop.run_in_executor(
                    self.sync_dispatcher.executor, next, gen, sentinel
                )
                if chunk is sentinel:
                    break
                if first:
                    first = False
                    wrapped = Event(
                        topic=resolved_topic,
                        payload=chunk,
                        author=publisher.plugin_name,
                        author_id=publisher.plugin_uuid,
                        author_host=self.hostname,
                        subscription_id=event_meta.subscription_id,
                        timestamp=event_meta.timestamp,
                    )
                    yield wrapped
                else:
                    yield chunk

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
        the caller thread."""
        async_gen = self.request_event_stream(
            publisher, event_id, payload, topic_vars, hosts, blocked_hosts,
            timeout,
        )

        while True:
            try:
                chunk = asyncio.run_coroutine_threadsafe(
                    async_gen.__anext__(), self.main_event_loop
                ).result()
            except StopAsyncIteration:
                break
            yield chunk

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
        """
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

        return sub_uuid

    async def unsubscribe_event(self, sub_uuid: str) -> bool:
        """Remove a runtime subscription (NEW PR3 API).

        Returns True if found and removed. Cleans up the owning plugin's
        _sub_uuids list as a side-effect (best-effort lookup).
        """
        sub = await self.topic_registry.get_subscription(sub_uuid)
        ok = await self.topic_registry.unsubscribe(sub_uuid)
        if ok and sub is not None:
            owner = self.plugins_by_uuid.get(sub.plugin_uuid)
            if owner is not None and hasattr(owner, "_sub_uuids"):
                try:
                    owner._sub_uuids.remove(sub_uuid)
                except ValueError:
                    pass
        return ok
