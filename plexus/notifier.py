"""
Topic-based pub/sub notification and request-by-topic routing system.

TopicRegistry + Subscription dataclass back the publish_event /
request_event API. Subscriptions reference a TARGET endpoint
(``target_plugin`` + ``target_access_name``) instead of carrying a raw
handler callable. Matching iterates a single insertion-ordered structure
so YAML declaration order alone determines tie-breaks — there is no
exact-then-wildcard split.

Topics use "/" as separator (e.g. "ai/chat", "sensor/bathroom/temperature").
Single-level wildcard "*" is supported: "sensor/*/temperature" matches
"sensor/bathroom/temperature" but not "sensor/bathroom/sub/temperature".
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple, Union
from uuid import uuid4


class SyncDispatcher:
    """Dedicated executor for sync subscriber handlers (PR3 Q17 + C3 + C8).

    Thin wrapper around a ``ThreadPoolExecutor(max_workers=N,
    thread_name_prefix="sync-notifier")`` whose internal queue serves as
    the FIFO dispatch queue for sync handlers. Workers pick up handlers
    one at a time; with ``workers=1`` the user gets serialization (C9).
    Default ``N=4`` per Q17, configurable via
    ``general.sync_dispatcher_workers`` in main config.yml.

    Stage A: instantiated by Plexus.__init__ and shut down by
    Plexus.close() AFTER the existing 30s in-flight drain (C8).
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
        graceful 30s drain happens upstream in Plexus.close() before
        this method is called, so by the time we get here pending sync
        handlers have either finished or been told to wrap up.
        """
        self._logger.debug("SyncDispatcher.shutdown(wait=%s)", wait)
        self.executor.shutdown(wait=wait)


@dataclass
class Subscription:
    """A single topic subscription.

    Per LOCKED IN PR3 NOTIFIER YAML + EVENT MODEL section D + the
    "TopicRegistry / Subscription dataclass field updates" notes:

      * ``sub_uuid`` — always uuid4 hex (Q6 β + uuid naming convention).
        Canonical identity.
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

    def __repr__(self) -> str:
        target = f"{self.target_plugin}.{self.target_access_name}"
        head = self.declared_id or self.sub_uuid[:8]
        return f"Sub({head}, {self.topic_pattern} -> {target})"


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
        """Build and register a Subscription. Returns sub_uuid.

        ``target_plugin`` defaults to ``plugin_name`` (self-routing).
        ``target_access_name`` is required (resolves to the endpoint that
        receives the dispatched event).
        """
        effective_target_plugin = target_plugin or plugin_name

        sub = Subscription(
            sub_uuid=uuid4().hex,
            declared_id=declared_id,
            topic_pattern=topic_pattern,
            plugin_name=plugin_name,
            plugin_uuid=plugin_uuid,
            target_plugin=effective_target_plugin,
            target_access_name=target_access_name or "",
            target_plugin_uuid=target_plugin_uuid,
            hosts=hosts,
            blocked_hosts=blocked_hosts,
            authors=authors,
            blocked_authors=blocked_authors,
            enabled=enabled,
        )

        return await self.register(sub)

    async def unsubscribe(self, sub_uuid: str) -> bool:
        """Remove a subscription by sub_uuid. Returns True if found and removed."""
        async with self._lock:
            sub = self._subs.pop(sub_uuid, None)
            if sub is None:
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
                if sub is None:
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

    async def set_subscription_enabled(
        self, sub_uuid: str, enabled: bool
    ) -> Tuple[Optional[Subscription], bool]:
        """Toggle a subscription's enabled flag atomically inside the
        registry lock. Returns ``(sub_or_None, changed)``:

        * ``sub=None`` when ``sub_uuid`` is unknown (popped or never existed)
        * ``changed=False`` when current state already matched (no-op)
        * ``changed=True`` when the flag was flipped

        Caller is responsible for any post-mutation broadcast / emit; this
        method does NOT release the lock to call network code (per the
        framework's lock-ordering rule that disallows network I/O inside
        registry locks — see ``_get_lifecycle_lock`` in Plexus).

        The returned ``Subscription`` reference is the live registry entry;
        callers that need to broadcast to peers can read its fields after
        the lock has released — the dataclass is mutable, but field reads
        are atomic at the Python attribute level, so a concurrent
        unsubscribe between this return and the broadcast call leaves the
        reference valid (peer just learns about an enabled state for a
        sub that no longer exists locally; eventual consistency via
        peer heartbeat resolves it).
        """
        # Coerce to bool so callers passing truthy/falsy non-bool values
        # (e.g. 1 from a JSON deserializer) don't silently corrupt
        # ``Subscription.enabled`` to a non-bool type. find_all's
        # ``if not sub.enabled`` check tolerates truthy/falsy values, but
        # downstream readers (advert protocol, TUI rendering) expect bool.
        coerced = bool(enabled)
        async with self._lock:
            sub = self._subs.get(sub_uuid)
            if sub is None:
                return None, False
            if sub.enabled == coerced:
                return sub, False
            sub.enabled = coerced
            return sub, True

    async def list_local_subs(self) -> List[Subscription]:
        """Snapshot all local subs in insertion order. Used by Stage C
        advert protocol for outbound snapshot build (locked #14)."""
        async with self._lock:
            return list(self._subs.values())
