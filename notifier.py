"""
Topic-based pub/sub notification and request-by-topic routing system.

PR3 Stage B: TopicRegistry + Subscription dataclass refactored for the
publish_event/request_event API. Subscriptions reference a TARGET endpoint
(target_plugin + target_access_name) instead of carrying a raw handler
callable. Matching iterates a single insertion-ordered structure so YAML
declaration order alone determines tie-breaks (LOCKED C — no more
exact-then-wildcard split).

The OLD `notify`/`request_topic` API (PluginCore.notify, request_topic,
their sync + stream variants) still calls into this registry. Stage B
keeps that path alive — Stage D removes it. To bridge both APIs, the
Subscription dataclass keeps a small back-compat shim: legacy
``handler``/``endpoint_access_name``/``config_driven`` fields are
re-exposed as Python ``@property`` overlays on top of the new
``target_plugin``/``target_access_name``/``declared_id`` fields, so the
old PluginCore.notify path keeps reading them by attribute name.

Topics use "/" as separator (e.g. "ai/chat", "sensor/bathroom/temperature").
Single-level wildcard "*" is supported: "sensor/*/temperature" matches
"sensor/bathroom/temperature" but not "sensor/bathroom/sub/temperature".
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Union
from uuid import uuid4


class SyncDispatcher:
    """Dedicated executor for sync subscriber handlers (PR3 Q17 + C3 + C8).

    Thin wrapper around a ``ThreadPoolExecutor(max_workers=N,
    thread_name_prefix="sync-notifier")`` whose internal queue serves as
    the FIFO dispatch queue for sync handlers. Workers pick up handlers
    one at a time; with ``workers=1`` the user gets serialization (C9).
    Default ``N=4`` per Q17, configurable via
    ``general.sync_dispatcher_workers`` in main config.yml.

    Stage A: instantiated by PluginCore.__init__ and shut down by
    PluginCore.close() AFTER the existing 30s in-flight drain (C8).
    Callers (Stage B fan-out) submit handlers via
    ``loop.run_in_executor(dispatcher.executor, handler, event)`` —
    NOT submit + done_callback (per C3).
    """

    def __init__(
        self,
        workers: int = 4,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._workers = max(1, int(workers))
        self._logger = logger or logging.getLogger(__name__)
        self.executor = ThreadPoolExecutor(
            max_workers=self._workers,
            thread_name_prefix="sync-notifier",
        )
        self._logger.debug(
            "SyncDispatcher initialized with %d worker(s)", self._workers
        )

    def shutdown(self, wait: bool = False) -> None:
        """Shut the executor down. ``wait=False`` matches C8 — the
        graceful 30s drain happens upstream in PluginCore.close() before
        this method is called, so by the time we get here pending sync
        handlers have either finished or been told to wrap up.
        """
        self._logger.debug("SyncDispatcher.shutdown(wait=%s)", wait)
        self.executor.shutdown(wait=wait)


@dataclass
class Subscription:
    """A single topic subscription (PR3 Stage B — new shape).

    Per LOCKED IN PR3 NOTIFIER YAML + EVENT MODEL section D + the
    "TopicRegistry / Subscription dataclass field updates" notes:

      * ``sub_uuid`` — always uuid4 hex (Q6 β + uuid naming convention).
        Replaces the legacy ``id`` field as the canonical identity.
      * ``declared_id`` — YAML key for declared subs; ``None`` for
        runtime subs. Used as Event.subscription_id for declared subs
        per C4 (a).
      * ``topic_pattern`` — literal topic or ``*``-wildcard pattern.
      * ``plugin_name`` / ``plugin_uuid`` — sub OWNER (the plugin that
        declared/registered this subscription).
      * ``target_plugin`` / ``target_access_name`` — the endpoint the
        sub routes to (defaults: target_plugin = plugin_name, i.e.
        self-routing). Cross-plugin orchestrator subs (PR3 LOCKED G)
        use a different target_plugin from the owner.
      * ``target_plugin_uuid`` — optional runtime instance pin (C7).
      * ``hosts`` / ``blocked_hosts`` / ``authors`` / ``blocked_authors``
        — receiver-side filter chain.
      * ``enabled`` — Q13 opt-out flag, default True.

    Legacy fields (Stage B compat — Stage D removes them):
      * ``id`` — alias for sub_uuid; old code reads it directly.
      * ``handler`` — bare callable for code-driven subs registered via
        the legacy ``Plugin.subscribe(topic, handler=...)`` path.
      * ``endpoint_access_name`` — alias for target_access_name.
      * ``config_driven`` — derived from ``declared_id is not None``,
        but kept as a real field for the legacy path that reads it.

    The legacy fields are populated by the legacy ``subscribe(handler=...)``
    code path; new code uses ``target_plugin`` + ``target_access_name``
    and never reads the legacy fields.
    """

    # Identity
    sub_uuid: str
    declared_id: Optional[str] = None

    # Match
    topic_pattern: str = ""

    # Owner (the plugin that declared/registered the sub)
    plugin_name: str = ""
    plugin_uuid: str = ""

    # Target (where the framework dispatches when the topic fires)
    target_plugin: str = ""
    target_access_name: str = ""
    target_plugin_uuid: Optional[str] = None

    # Filter chain (per LOCKED A subscriptions: shape)
    hosts: Union[str, list, None] = "any"
    blocked_hosts: Union[str, list, None] = None
    authors: Union[str, list, None] = None
    blocked_authors: Union[str, list, None] = None

    # Opt-out flag (Q13). Disabled subs are skipped at registration.
    enabled: bool = True

    # ── LEGACY fields (Stage B only; Stage D removes) ──────────────
    # The old PluginCore.notify / request_topic path reads these
    # directly. New code SHOULD NOT use them.
    handler: Optional[Callable] = None
    endpoint_access_name: Optional[str] = None
    config_driven: bool = False

    def __repr__(self) -> str:
        target = f"{self.target_plugin}.{self.target_access_name}" if (
            self.target_plugin or self.target_access_name
        ) else (self.endpoint_access_name or "?")
        head = self.declared_id or self.sub_uuid[:8]
        return f"Sub({head}, {self.topic_pattern} -> {target})"

    @property
    def id(self) -> str:
        """Legacy alias for sub_uuid. Read-only so the property and
        a hypothetical dataclass field don't conflict."""
        return self.sub_uuid


class TopicRegistry:
    """
    Manages topic subscriptions and matching.

    PR3 Stage B: subscriptions are stored in a SINGLE insertion-ordered
    dict (``self._subs`` keyed by sub_uuid; Python dicts preserve
    insertion order natively). ``find_all`` and ``find_first`` iterate
    this single dict so YAML declaration order alone determines
    matching order — fixing the LOCKED C tie-break (a wildcard sub
    declared FIRST in YAML must win over an exact-match sub declared
    second when both match).

    Auxiliary indexes (per-plugin, by-declared_id) provide fast cleanup
    + lookup but are NEVER used as iteration source for matching.

    Thread-safe via asyncio.Lock (sync plugin methods run on the thread
    pool but subscribe/unsubscribe/match are always called from the
    event loop).
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self._logger = logger or logging.getLogger(__name__)
        self._lock = asyncio.Lock()

        # Single insertion-ordered dict: sub_uuid -> Subscription. Python
        # dicts preserve insertion order natively (3.7+), so iteration is
        # YAML registration order (LOCKED C tie-break).
        self._subs: Dict[str, Subscription] = {}

        # Auxiliary index for plugin bulk-cleanup (pop_plugin / on_disable
        # wrapper). NOT an iteration source for matching.
        self._by_plugin: Dict[str, Set[str]] = {}

        # Optional secondary index — by (plugin_uuid, declared_id) — for
        # YAML-key lookups (override-time, debugging). NOT used for
        # matching.
        self._by_declared: Dict[tuple, str] = {}

    @property
    def _by_id(self) -> Dict[str, Subscription]:
        """Legacy alias for ``_subs``. Stage B keeps this around because
        TestNotifierSuite (still alive until Stage D) pokes into the
        registry directly. Stage D removes both the test and the alias.
        """
        return self._subs

    @staticmethod
    def _is_wildcard(topic: str) -> bool:
        return "*" in topic

    @staticmethod
    def _topic_matches(pattern: str, topic: str) -> bool:
        """
        Check if a wildcard pattern matches a topic.

        Uses segment-level matching: each "/" separated segment is compared
        individually. "*" matches exactly one segment.

        Examples:
            "ai/*"                  matches "ai/chat"           -> True
            "ai/*"                  matches "ai/chat/stream"    -> False
            "sensor/*/temperature"  matches "sensor/bath/temperature" -> True
        """
        pattern_parts = pattern.split("/")
        topic_parts = topic.split("/")

        if len(pattern_parts) != len(topic_parts):
            return False

        for p, t in zip(pattern_parts, topic_parts):
            if p == "*":
                continue
            if p != t:
                return False
        return True

    async def register(self, sub: Subscription) -> str:
        """
        Register a fully-built Subscription object. Returns sub_uuid.

        Used by both the Stage B YAML-driven on_enable wrapper (which
        builds Subscription with declared_id set) and the runtime
        Plugin.subscribe API (declared_id=None).
        """
        if not sub.sub_uuid:
            sub.sub_uuid = uuid4().hex

        async with self._lock:
            self._subs[sub.sub_uuid] = sub
            self._by_plugin.setdefault(sub.plugin_uuid, set()).add(sub.sub_uuid)
            if sub.declared_id is not None:
                self._by_declared[(sub.plugin_uuid, sub.declared_id)] = sub.sub_uuid

            # Q18: subscribe/unsubscribe events ALWAYS log at INFO regardless
            # of verbose_notifier toggle (toggle only affects dispatch
            # logging). Logged INSIDE the lock so concurrent
            # subscribe/unsubscribe sequences emit log lines in the same
            # order they mutate the registry — diagnostic ordering matches
            # state ordering.
            self._logger.info(f"Subscribed: {sub}")
        return sub.sub_uuid

    async def subscribe(
        self,
        topic_pattern: str,
        plugin_name: str,
        plugin_uuid: str,
        # Stage B: prefer target_plugin / target_access_name
        target_plugin: Optional[str] = None,
        target_access_name: Optional[str] = None,
        target_plugin_uuid: Optional[str] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        authors: Union[str, list, None] = None,
        blocked_authors: Union[str, list, None] = None,
        declared_id: Optional[str] = None,
        enabled: bool = True,
        # Legacy kwargs — Stage B back-compat. Stage D removes them.
        endpoint_access_name: Optional[str] = None,
        handler: Optional[Callable] = None,
        config_driven: bool = False,
    ) -> str:
        """Build and register a Subscription. Returns sub_uuid.

        Two calling conventions are supported during Stage B:
          1. NEW (Stage B+): pass ``target_plugin`` + ``target_access_name``.
          2. LEGACY (still alive in Stage B; removed in Stage D): pass
             ``endpoint_access_name`` (treated as target_access_name on
             the calling plugin) — also accepts ``handler=`` kwarg for
             code-driven subs that route through the OLD PluginCore.notify
             path. The OLD path inspects ``sub.handler`` via a
             ``@property`` shim and calls it directly.

        Code-driven legacy subs (``handler=`` only) are stored as a
        Subscription with ``target_plugin/target_access_name`` blank and
        a ``_legacy_handler`` slot patched on the dataclass instance —
        the legacy notify path uses ``sub.handler`` (the property)
        which dispatches to the slot. New code MUST NOT depend on this.
        """
        # Stage B compat: if caller used the old endpoint_access_name kwarg
        # without target_plugin, treat the sub as routed back to the owner.
        effective_target_plugin = target_plugin or plugin_name
        effective_target_access = target_access_name or endpoint_access_name or ""

        sub = Subscription(
            sub_uuid=uuid4().hex,
            declared_id=declared_id,
            topic_pattern=topic_pattern,
            plugin_name=plugin_name,
            plugin_uuid=plugin_uuid,
            target_plugin=effective_target_plugin,
            target_access_name=effective_target_access,
            target_plugin_uuid=target_plugin_uuid,
            hosts=hosts,
            blocked_hosts=blocked_hosts,
            authors=authors,
            blocked_authors=blocked_authors,
            enabled=enabled,
            # Legacy field population — Stage D removes:
            handler=handler,
            endpoint_access_name=(
                endpoint_access_name
                if endpoint_access_name is not None
                else (target_access_name or None)
            ),
            config_driven=config_driven or (declared_id is not None),
        )

        return await self.register(sub)

    async def unsubscribe(self, sub_uuid: str) -> bool:
        """Remove a subscription by sub_uuid. Returns True if found and removed."""
        async with self._lock:
            sub = self._subs.pop(sub_uuid, None)
            if not sub:
                return False

            plugin_subs = self._by_plugin.get(sub.plugin_uuid)
            if plugin_subs:
                plugin_subs.discard(sub_uuid)
                if not plugin_subs:
                    self._by_plugin.pop(sub.plugin_uuid, None)

            if sub.declared_id is not None:
                self._by_declared.pop((sub.plugin_uuid, sub.declared_id), None)

            # Q18: subscribe/unsubscribe always log at INFO. Logged
            # INSIDE the lock so concurrent subscribe/unsubscribe log
            # lines appear in the same order as the registry mutations.
            self._logger.info(f"Unsubscribed: {sub}")
        return True

    async def unsubscribe_plugin(self, plugin_uuid: str) -> int:
        """Remove all subscriptions for a plugin. Returns count removed."""
        async with self._lock:
            sub_uuids = self._by_plugin.pop(plugin_uuid, set())
            count = 0
            for sub_uuid in list(sub_uuids):
                sub = self._subs.pop(sub_uuid, None)
                if not sub:
                    continue
                count += 1
                if sub.declared_id is not None:
                    self._by_declared.pop((sub.plugin_uuid, sub.declared_id), None)

        if count:
            self._logger.debug(
                f"Unsubscribed all ({count}) subscriptions for plugin {plugin_uuid}"
            )
        return count

    async def find_all(self, topic: str) -> List[Subscription]:
        """Find all subscriptions matching a topic, in INSERTION ORDER.

        Iterates the single ordered store (LOCKED C tie-break). Disabled
        subs (``enabled is False``) are skipped — they stay in the
        registry for advertisement-protocol introspection but are
        never dispatched.
        """
        results: List[Subscription] = []
        async with self._lock:
            for sub in self._subs.values():
                if not sub.enabled:
                    continue
                if self._is_wildcard(sub.topic_pattern):
                    if self._topic_matches(sub.topic_pattern, topic):
                        results.append(sub)
                else:
                    if sub.topic_pattern == topic:
                        results.append(sub)
        return results

    async def find_first(self, topic: str) -> Optional[Subscription]:
        """
        Find the first matching subscription (for request-by-topic).

        PR3 Stage B (LOCKED C): single iteration over insertion-ordered
        store; first match wins. NO config-driven preference logic —
        YAML declaration order alone decides.
        """
        all_subs = await self.find_all(topic)
        return all_subs[0] if all_subs else None

    async def get_subscription_count(self, topic: str) -> int:
        """Return the number of subscriptions matching a topic."""
        return len(await self.find_all(topic))

    async def get_all_topics(self) -> List[str]:
        """Return all registered topic patterns (insertion order)."""
        async with self._lock:
            seen = []
            for sub in self._subs.values():
                if sub.topic_pattern not in seen:
                    seen.append(sub.topic_pattern)
            return seen

    async def get_plugin_subscriptions(self, plugin_uuid: str) -> List[Subscription]:
        """Return all subscriptions for a given plugin in INSERTION
        ORDER (LOCKED C). The internal ``_by_plugin`` index is a set
        which has no defined iteration order; we walk ``_subs`` (a
        Python 3.7+ insertion-ordered dict) and filter, ensuring
        introspection callers see a deterministic ordering matching
        find_all/find_first behavior.
        """
        async with self._lock:
            sub_uuids = self._by_plugin.get(plugin_uuid, set())
            return [
                sub for sid, sub in self._subs.items()
                if sid in sub_uuids
            ]

    async def get_subscription(self, sub_uuid: str) -> Optional[Subscription]:
        """Look up a subscription by sub_uuid."""
        async with self._lock:
            return self._subs.get(sub_uuid)
