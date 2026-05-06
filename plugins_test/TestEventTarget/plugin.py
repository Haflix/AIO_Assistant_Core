"""TestEventTarget — passive subscriber fixture for PR3 Stage E.

Used by TestEventSuite to verify cross-plugin subscription routing,
access-control gates, sync-handler dispatch, streaming handlers, and
load-time topic templating ({prefix}, {plugin_name}, {hostname}). All
handlers append to instance mailboxes; tests inspect via get_state.

Stays Tier 1 — no business logic, only mailbox-style state recording.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import threading  # noqa: E402
from typing import Any, Dict, List  # noqa: E402

from utils import Plugin  # noqa: E402
from decorators import async_log_errors, log_errors  # noqa: E402


class TestEventTarget(Plugin):
    """Passive subscriber for cross-plugin event tests."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self.received: Dict[str, List[Dict[str, Any]]] = {}
        self.last_thread_name: Dict[str, str] = {}
        self.priv_call_count: int = 0
        self.requester_id_seen: List[str] = []

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestEventTarget enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestEventTarget disabled")

    # ------------------------------------------------------------------
    # Mailbox management
    # ------------------------------------------------------------------

    async def reset_state(self) -> None:
        """Reset all mailbox state. Tests call before each case body."""
        self.received = {}
        self.last_thread_name = {}
        self.priv_call_count = 0
        self.requester_id_seen = []

    async def get_state(self) -> Dict[str, Any]:
        """Return a snapshot of internal state."""
        return {
            "received": dict(self.received),
            "last_thread_name": dict(self.last_thread_name),
            "priv_call_count": self.priv_call_count,
            "requester_id_seen": list(self.requester_id_seen),
        }

    def _record(self, slot: str, event) -> None:
        bucket = self.received.setdefault(slot, [])
        bucket.append({
            "topic": event.topic,
            "payload": event.payload,
            "author": event.author,
            "author_id": event.author_id,
            "author_host": event.author_host,
            "subscription_id": event.subscription_id,
            "timestamp": event.timestamp,
        })

    # ------------------------------------------------------------------
    # Async handlers
    # ------------------------------------------------------------------

    async def handle_cross(self, event):
        self._record("cross", event)

    async def handle_disabled(self, event):
        self._record("disabled", event)

    async def priv_endpoint(self, event):
        self.priv_call_count += 1
        self._record("priv", event)

    async def handle_requester_probe(self, event):
        # Tests use get_state to verify the dispatch reached this endpoint
        # at all. Author identity is captured via _record; the lookup of
        # which uuid was used by find_endpoint (requester_id != author_id
        # for cross-plugin subs) lives on the framework side.
        self.requester_id_seen.append(event.author_id)
        self._record("requester_probe", event)

    async def handle_exact_match(self, event):
        self._record("exact_match", event)

    async def handle_wildcard_match(self, event):
        self._record("wildcard_match", event)

    async def handle_no_match(self, event):
        self._record("no_match", event)

    async def handle_stream(self, event):
        # Async generator handler used by request_event_stream tests.
        # First yield gets wrapped server-side (LOCKED #2); subsequent
        # yields ship raw.
        for i in range(3):
            yield {"chunk": i, "topic": event.topic}

    async def handle_hostname_topic(self, event):
        self._record("hostname_topic", event)

    async def handle_prefix_topic(self, event):
        self._record("prefix_topic", event)

    async def handle_plugin_name_topic(self, event):
        self._record("plugin_name_topic", event)

    async def handle_user_topic(self, event):
        self._record("user_topic", event)

    async def handle_mixed_topic(self, event):
        self._record("mixed_topic", event)

    async def echo_author_id(self, event):
        return {"author": event.author, "author_id": event.author_id}

    # ------------------------------------------------------------------
    # Sync handler — runs on the SyncDispatcher executor (def, not async)
    # ------------------------------------------------------------------

    def handle_sync(self, event):
        self.last_thread_name["sync"] = threading.current_thread().name
        self._record("sync", event)
        return {"thread_name": threading.current_thread().name}
