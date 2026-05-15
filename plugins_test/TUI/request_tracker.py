"""
Lightweight request statistics tracker.

Polls Plexus.requests periodically, diffs against previous snapshot,
and accumulates throughput, latency, error rate, and per-plugin stats.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Dict, List


class PluginStats:
    """Per-plugin accumulated stats."""

    def __init__(self):
        self.total: int = 0
        self.errors: int = 0
        self.timeouts: int = 0
        self.latencies: deque = deque(maxlen=100)

    @property
    def avg_latency(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else 0.0

    @property
    def error_rate(self) -> float:
        return (self.errors / self.total * 100) if self.total > 0 else 0.0


class ActiveRequest:
    """Snapshot of a single in-flight request."""

    __slots__ = ("request_id", "plugin", "method", "author", "elapsed", "has_error", "has_timeout", "is_finished")

    def __init__(self, request_id: str, plugin: str, method: str, elapsed: float,
                 author: str = "?", has_error: bool = False, has_timeout: bool = False,
                 is_finished: bool = False):
        self.request_id = request_id
        self.plugin = plugin
        self.method = method
        self.author = author
        self.elapsed = elapsed
        self.has_error = has_error
        self.has_timeout = has_timeout
        self.is_finished = is_finished


class RequestTracker:
    """Tracks request flow by polling core.requests dict."""

    def __init__(self, history_size: int = 300):
        self.total_requests: int = 0
        self.total_errors: int = 0
        self.total_timeouts: int = 0
        self.latencies: deque = deque(maxlen=history_size)
        # Each entry is (completed_count, elapsed_seconds_since_last_poll)
        self.throughput_history: deque = deque(maxlen=60)
        self.per_plugin: Dict[str, PluginStats] = {}
        self.active: List[ActiveRequest] = []
        # {rid: (first_seen_timestamp, plugin_name), "rid:err": (ts, plugin), ...}
        self._seen_ids: Dict[str, tuple] = {}
        self._last_poll: float = time.time()

    def poll(self, requests_dict: Dict[str, Any]) -> None:
        now = time.time()
        current_ids = set()
        self.active = []

        for req_id, req in requests_dict.items():
            rid = str(req_id)
            current_ids.add(rid)

            plugin = getattr(req, "target_plugin", "?")
            method = getattr(req, "target_method", "?")
            author = getattr(req, "author", "?")
            created = getattr(req, "created_at", now)
            finished = getattr(req, "finished_at", None)
            has_error = getattr(req, "error", False)
            has_timeout = getattr(req, "timeout", False)

            # Use finished_at for accurate elapsed when available
            if finished and created:
                elapsed = finished - created
            elif created:
                elapsed = now - created
            else:
                elapsed = 0.0

            self.active.append(ActiveRequest(
                request_id=rid[:8], plugin=plugin, method=method,
                elapsed=elapsed, author=author,
                has_error=has_error, has_timeout=has_timeout,
                is_finished=finished is not None,
            ))

            if rid not in self._seen_ids:
                self._seen_ids[rid] = (created if created else now, plugin, None)
                self.total_requests += 1
                self.per_plugin.setdefault(plugin, PluginStats()).total += 1

            # Update finished_at in seen entry when it becomes available
            if finished and rid in self._seen_ids:
                entry = self._seen_ids[rid]
                if len(entry) == 3 and entry[2] is None:
                    self._seen_ids[rid] = (entry[0], entry[1], finished)

            if has_error:
                marker = f"{rid}:err"
                if marker not in self._seen_ids:
                    self._seen_ids[marker] = (now, plugin)
                    self.total_errors += 1
                    self.per_plugin.setdefault(plugin, PluginStats()).errors += 1

            if has_timeout:
                marker = f"{rid}:timeout"
                if marker not in self._seen_ids:
                    self._seen_ids[marker] = (now, plugin)
                    self.total_timeouts += 1
                    self.per_plugin.setdefault(plugin, PluginStats()).timeouts += 1

        completed = 0
        gone = [rid for rid in self._seen_ids if ":" not in rid and rid not in current_ids]
        for rid in gone:
            entry = self._seen_ids.pop(rid)
            first_seen, plugin_name = entry[0], entry[1]
            finished = entry[2] if len(entry) > 2 else None
            # Use actual completion time when available, fall back to poll time
            latency = (finished - first_seen) if finished else (now - first_seen)
            self.latencies.append(latency)
            self.per_plugin.setdefault(plugin_name, PluginStats()).latencies.append(latency)
            completed += 1
            self._seen_ids.pop(f"{rid}:err", None)
            self._seen_ids.pop(f"{rid}:timeout", None)

        elapsed_since_last = now - self._last_poll
        self.throughput_history.append((completed, elapsed_since_last))
        self._last_poll = now
        self.active.sort(key=lambda r: r.elapsed, reverse=True)

        # Sweep stale entries older than 5 minutes to prevent unbounded growth.
        # Only sweep entries NOT currently active — avoids double-counting
        # long-running requests that reappear on next poll.
        max_age = 300.0
        stale = [
            k for k, entry in self._seen_ids.items()
            if (now - entry[0]) > max_age and k not in current_ids
            and (k.split(":")[0] not in current_ids)  # also keep markers for active requests
        ]
        for k in stale:
            del self._seen_ids[k]

    @property
    def avg_latency(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else 0.0

    @property
    def requests_per_minute(self) -> float:
        if not self.throughput_history:
            return 0.0
        total_completed = sum(c for c, _ in self.throughput_history)
        total_elapsed = sum(dt for _, dt in self.throughput_history)
        if total_elapsed <= 0:
            return 0.0
        return total_completed * (60.0 / total_elapsed)

    @property
    def error_rate(self) -> float:
        return (self.total_errors / self.total_requests * 100) if self.total_requests > 0 else 0.0

    def reset(self) -> None:
        self.total_requests = 0
        self.total_errors = 0
        self.total_timeouts = 0
        self.latencies.clear()
        self.throughput_history.clear()
        self.per_plugin.clear()
        self.active.clear()
        self._seen_ids.clear()
