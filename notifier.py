"""
Topic-based pub/sub notification and request-by-topic routing system.

Provides two communication patterns for decoupling plugin-to-plugin calls:
  - Notify (fire-and-forget, one-to-many): publish to a topic, all subscribers
    are called concurrently, errors are logged but do not propagate.
  - Request-by-topic (one-to-one, with response): request a topic, the first
    matching handler is called and its result returned (same discovery logic as
    PluginCore.execute with host="any").

Topics use "/" as separator (e.g. "ai/chat", "sensor/bathroom/temperature").
Single-level wildcard "*" is supported: "sensor/*/temperature" matches
"sensor/bathroom/temperature" but not "sensor/bathroom/sub/temperature".
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import uuid4


@dataclass
class Subscription:
    """A single topic subscription."""

    id: str
    topic_pattern: str
    plugin_name: str
    plugin_uuid: str
    # Config-driven: endpoint access_name to call via PluginCore.execute
    endpoint_access_name: Optional[str] = None
    # Code-driven: direct callable reference
    handler: Optional[Callable] = None
    # Whether this came from plugin_config.yml or subscribe()
    config_driven: bool = False

    def __repr__(self):
        target = self.endpoint_access_name or (
            self.handler.__name__ if self.handler else "?"
        )
        return f"Sub({self.id[:8]}, {self.topic_pattern} -> {self.plugin_name}.{target})"


class TopicRegistry:
    """
    Manages topic subscriptions and matching.

    Thread-safe via asyncio.Lock (sync plugin methods run on the thread pool
    but subscribe/unsubscribe/match are always called from the event loop).
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self._logger = logger or logging.getLogger(__name__)
        self._lock = asyncio.Lock()

        # Exact topic -> list of subscriptions (fast path)
        self._exact: Dict[str, List[Subscription]] = {}
        # Wildcard pattern -> list of subscriptions
        self._wildcard: Dict[str, List[Subscription]] = {}
        # Subscription ID -> Subscription (for targeted unsubscribe)
        self._by_id: Dict[str, Subscription] = {}
        # Plugin UUID -> set of subscription IDs (for bulk cleanup)
        self._by_plugin: Dict[str, Set[str]] = {}

    def _is_wildcard(self, topic: str) -> bool:
        return "*" in topic

    def _topic_matches(self, pattern: str, topic: str) -> bool:
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

    async def subscribe(
        self,
        topic_pattern: str,
        plugin_name: str,
        plugin_uuid: str,
        endpoint_access_name: Optional[str] = None,
        handler: Optional[Callable] = None,
        config_driven: bool = False,
    ) -> str:
        """
        Register a subscription. Returns the subscription ID.

        Either endpoint_access_name (config-driven, resolved via PluginCore.execute)
        or handler (code-driven, called directly) must be provided.
        """
        sub_id = uuid4().hex
        sub = Subscription(
            id=sub_id,
            topic_pattern=topic_pattern,
            plugin_name=plugin_name,
            plugin_uuid=plugin_uuid,
            endpoint_access_name=endpoint_access_name,
            handler=handler,
            config_driven=config_driven,
        )

        async with self._lock:
            if self._is_wildcard(topic_pattern):
                self._wildcard.setdefault(topic_pattern, []).append(sub)
            else:
                self._exact.setdefault(topic_pattern, []).append(sub)
            self._by_id[sub_id] = sub
            self._by_plugin.setdefault(plugin_uuid, set()).add(sub_id)

        self._logger.debug(f"Subscribed: {sub}")
        return sub_id

    async def unsubscribe(self, sub_id: str) -> bool:
        """Remove a subscription by its ID. Returns True if found and removed."""
        async with self._lock:
            sub = self._by_id.pop(sub_id, None)
            if not sub:
                return False

            # Remove from exact or wildcard index
            if self._is_wildcard(sub.topic_pattern):
                subs = self._wildcard.get(sub.topic_pattern, [])
                subs[:] = [s for s in subs if s.id != sub_id]
                if not subs:
                    self._wildcard.pop(sub.topic_pattern, None)
            else:
                subs = self._exact.get(sub.topic_pattern, [])
                subs[:] = [s for s in subs if s.id != sub_id]
                if not subs:
                    self._exact.pop(sub.topic_pattern, None)

            # Remove from plugin index
            plugin_subs = self._by_plugin.get(sub.plugin_uuid)
            if plugin_subs:
                plugin_subs.discard(sub_id)
                if not plugin_subs:
                    self._by_plugin.pop(sub.plugin_uuid, None)

        self._logger.debug(f"Unsubscribed: {sub}")
        return True

    async def unsubscribe_plugin(self, plugin_uuid: str) -> int:
        """Remove all subscriptions for a plugin. Returns count removed."""
        async with self._lock:
            sub_ids = self._by_plugin.pop(plugin_uuid, set())
            count = 0
            for sub_id in sub_ids:
                sub = self._by_id.pop(sub_id, None)
                if not sub:
                    continue
                count += 1
                if self._is_wildcard(sub.topic_pattern):
                    subs = self._wildcard.get(sub.topic_pattern, [])
                    subs[:] = [s for s in subs if s.id != sub_id]
                    if not subs:
                        self._wildcard.pop(sub.topic_pattern, None)
                else:
                    subs = self._exact.get(sub.topic_pattern, [])
                    subs[:] = [s for s in subs if s.id != sub_id]
                    if not subs:
                        self._exact.pop(sub.topic_pattern, None)

        if count:
            self._logger.debug(
                f"Unsubscribed all ({count}) subscriptions for plugin {plugin_uuid}"
            )
        return count

    async def find_all(self, topic: str) -> List[Subscription]:
        """Find all subscriptions matching a topic (for notify)."""
        results = []
        async with self._lock:
            # Exact matches
            results.extend(self._exact.get(topic, []))
            # Wildcard matches
            for pattern, subs in self._wildcard.items():
                if self._topic_matches(pattern, topic):
                    results.extend(subs)
        return results

    async def find_first(self, topic: str) -> Optional[Subscription]:
        """
        Find the first matching subscription (for request-by-topic).

        Priority: config-driven before code-driven (within those, first registered).
        """
        all_subs = await self.find_all(topic)
        if not all_subs:
            return None
        # Config-driven first, then code-driven
        config = [s for s in all_subs if s.config_driven]
        if config:
            return config[0]
        return all_subs[0]

    async def get_subscription_count(self, topic: str) -> int:
        """Return the number of subscriptions matching a topic."""
        return len(await self.find_all(topic))

    async def get_all_topics(self) -> List[str]:
        """Return all registered topic patterns."""
        async with self._lock:
            return list(self._exact.keys()) + list(self._wildcard.keys())

    async def get_plugin_subscriptions(self, plugin_uuid: str) -> List[Subscription]:
        """Return all subscriptions for a given plugin."""
        async with self._lock:
            sub_ids = self._by_plugin.get(plugin_uuid, set())
            return [self._by_id[sid] for sid in sub_ids if sid in self._by_id]
