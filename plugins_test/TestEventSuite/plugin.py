"""TestEventSuite — PR3 event-API smoke shell.

Stage D (this PR) ships smoke-level coverage only:
  * event.publish.basic — publish_event fires every matching subscription
  * event.request.basic — request_event returns the handler's value
  * event.metadata.basic — subscriber receives an Event with topic/payload/
    author/author_host populated

Stage E expands this suite to cover the full PLAN J case list (cross-plugin
access control C18, host filters, declared_id propagation, sync handlers,
streaming, etc.).
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

from utils import Plugin  # noqa: E402
from decorators import async_log_errors, log_errors  # noqa: E402

from _test_helpers import CaseRecorder  # noqa: E402


SUITE_VERSION = "0.1.0"


class TestEventSuite(Plugin):
    """PR3 event-API smoke shell. See PLAN J for the Stage-E expansion."""

    @log_errors
    def on_load(self, *args, **kwargs):
        # Per-case state mailboxes — populated by the subscriber endpoints,
        # asserted by the case bodies. Reset at the start of each case body
        # so cases are independent.
        self.received_publish_payload: Any = None
        self.last_event_meta: Optional[Dict[str, Any]] = None

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestEventSuite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestEventSuite disabled")

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
        rec = CaseRecorder("TestEventSuite", SUITE_VERSION, self._plugin_core)

        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=False,
        )

        await self._basic_publish(rec, kw)
        await self._basic_request(rec, kw)
        await self._basic_metadata(rec, kw)

        return rec.to_dict()

    # ────────────────────────────────────────────────────────────────────
    # Subscriber endpoints (self-target — matched by the suite's own subs)
    # ────────────────────────────────────────────────────────────────────

    async def handle_smoke_publish(self, event):
        self.received_publish_payload = event.payload
        self.last_event_meta = {
            "topic": event.topic,
            "payload": event.payload,
            "author": event.author,
            "author_host": event.author_host,
        }

    async def handle_smoke_request(self, event):
        # Echo handler — returns a dict the request_event caller awaits on.
        q = (event.payload or {}).get("q") if isinstance(event.payload, dict) else None
        return {"echo": q}

    # ────────────────────────────────────────────────────────────────────
    # Cases
    # ────────────────────────────────────────────────────────────────────

    async def _basic_publish(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body(c):
            self.received_publish_payload = None
            self.last_event_meta = None
            count = await self.publish_event(
                "smoke_publish", payload={"x": 1},
            )
            # Brief settle — fan-out tasks created by publish_event run on
            # separate tasks; allow them to complete before assertion.
            await asyncio.sleep(0.05)
            c.expect(count, 1)
            c.expect(self.received_publish_payload, {"x": 1})

        await rec.run_case(
            "event.publish.basic", body,
            tags=("basic",), **kw,
        )

    async def _basic_request(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body(c):
            r = await self.request_event(
                "smoke_request", payload={"q": "hello"}, timeout=2.0,
            )
            c.expect(r, {"echo": "hello"})

        await rec.run_case(
            "event.request.basic", body,
            tags=("basic",), **kw,
        )

    async def _basic_metadata(self, rec: CaseRecorder, kw: Dict) -> None:
        async def body(c):
            self.received_publish_payload = None
            self.last_event_meta = None
            await self.publish_event(
                "smoke_publish", payload={"meta": "probe"},
            )
            await asyncio.sleep(0.05)
            meta = self.last_event_meta
            if meta is None:
                raise AssertionError(
                    "metadata case: subscriber did not record event metadata"
                )
            c.expect(meta["topic"], "test_event/smoke/publish")
            c.expect(meta["payload"], {"meta": "probe"})
            c.expect(meta["author"], self.plugin_name)
            if not meta.get("author_host"):
                raise AssertionError(
                    f"metadata case: author_host empty (got {meta!r})"
                )

        await rec.run_case(
            "event.metadata.basic", body,
            tags=("basic",), **kw,
        )
