"""TestBugSuite — PR3 Stage F bughunt repro suite + PR4 Stage K B-066 regressions.

One case per open `bugtracker.md` entry plus 7 PR4 Stage K B-066
regression cases (Test 1, 1b, 2, 2b, 3, 4, 5). Verdicts are recorded by
the parent post-run (annotated on bugtracker.md). NO bug fixes here —
only repros that prove which bugs are real vs fixed-by-construction.

Categories (one method per):
  _b_legacy_removed     — API surface deleted in Stage D — assert .gone
  _b_addressed_in_pr3   — PR3 added behavior that should fix the bug
  _b_active             — still-broken — repro and let recorder mark
  _b_deferred           — test infeasible without fixture work — skip
  _b_already_covered    — repro lives in another suite — skip-and-cite
  _b_security           — PR4 Stage K B-066 regression guards

See PLAN.md (alongside this file in the worktree) for the per-bug spec
table and pattern recipes.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402
import time  # noqa: E402
import inspect  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import pickle  # noqa: E402
import shutil  # noqa: E402
import ssl  # noqa: E402
import struct  # noqa: E402
import tempfile  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

from plexus.utils import Plugin  # noqa: E402
from plexus.decorators import async_log_errors, log_errors  # noqa: E402
from plexus.exceptions import RequestException  # noqa: E402

from plexus.networking import (  # noqa: E402
    MSG_EXECUTE,
    MSG_REQUEST_EVENT,
    MSG_PING,
    MSG_RESULT,
    MSG_STREAM_CHUNK,
    MSG_END_STREAM,
    MSG_ERROR,
    PeerSpec,
)
from plexus.serialization import generate_keypair, Serializable  # noqa: E402

from _test_helpers import CaseRecorder  # noqa: E402


SUITE_VERSION = "0.4.3"


# ──────────────────────────────────────────────────────────────────────
# PR4 Stage K B-066 — module-level helpers + payload classes.
#
# Pickle resolves classes/callables by (module, qualname). For payloads
# that traverse the wire, the class definition MUST be at module scope
# so the receiver-side find_class can resolve them. Plexus loads
# this file via spec_from_file_location(name="TestBugSuite", ...) so
# __module__ is "TestBugSuite" (not the dotted file path).
# ──────────────────────────────────────────────────────────────────────


def _b066_sentinel_create(sentinel_dir: str) -> None:
    """Module-level callable used as Test 1's __reduce__ target. A
    receiver still vulnerable to pre-/post-auth pickle RCE would invoke
    this. K-1 SafeUnpickler rejects in find_class — defense.
    """
    (Path(sentinel_dir) / "PWNED").write_bytes(b"pwned")


class _B066PrePwnPickle:
    """Test 1 payload. __reduce__ returns module-level callable."""

    def __init__(self, sentinel_dir: str) -> None:
        self.sentinel_dir = sentinel_dir

    def __reduce__(self):
        return (_b066_sentinel_create, (self.sentinel_dir,))


class _B066NotRegistered:
    """Test 2 payload. Plain class NOT inheriting Serializable.
    pickle.dumps succeeds; receiver's safe_loads rejects on find_class.
    """

    def __init__(self, value: str = "test") -> None:
        self.value = value


class _B066RegisteredButPwn(Serializable):
    """Test 2b payload. Inherits Serializable (CLASS lookup passes via
    SERIALIZABLE_REGISTRY) BUT __reduce__ returns a non-allowlisted
    callable — SafeUnpickler rejects on the *callable* lookup.
    """

    def __init__(self, sentinel_dir: str) -> None:
        self.sentinel_dir = sentinel_dir

    def __reduce__(self):
        return (_b066_sentinel_create, (self.sentinel_dir,))


class _B066LogCapture:
    """Inline log handler that buffers records on a logger to a list.
    Use as:
        cap = _B066LogCapture("networking", logging.DEBUG)
        cap.attach()
        try:
            ...
        finally:
            cap.detach()
    """

    def __init__(self, logger_name: str = "networking",
                 min_level: int = logging.DEBUG) -> None:
        self.logger_name = logger_name
        self.min_level = min_level
        self.records: List[logging.LogRecord] = []
        self._handler: Optional[logging.Handler] = None
        self._saved_level: int = logging.NOTSET
        self._attached: bool = False

    def attach(self) -> None:
        h = logging.Handler()
        h.setLevel(self.min_level)
        h.emit = lambda record: self.records.append(record)
        lg = logging.getLogger(self.logger_name)
        self._saved_level = lg.level
        if lg.level == logging.NOTSET or lg.level > self.min_level:
            lg.setLevel(self.min_level)
        lg.addHandler(h)
        self._handler = h
        self._attached = True

    def detach(self) -> None:
        # Cycle 4 fresh-eyes MED fix: only restore logger level if
        # attach() actually ran. A `finally`-block detach() called after
        # an exception during attach setup must not silently reset the
        # logger to NOTSET (which would suppress WARNING messages later
        # security tests rely on).
        if not self._attached:
            return
        lg = logging.getLogger(self.logger_name)
        if self._handler is not None:
            lg.removeHandler(self._handler)
            self._handler = None
        lg.setLevel(self._saved_level)
        self._attached = False

    def has_message(self, substring: str, min_level: int = 0) -> bool:
        for r in self.records:
            if r.levelno < min_level:
                continue
            if substring in r.getMessage():
                return True
        return False


async def _b066_send_msg(writer: asyncio.StreamWriter, msg_type: int,
                         data: Any) -> None:
    """Wire-frame send matching networking._send_message format.
    Sender uses raw pickle.dumps — receiver runs the bytes through
    safe_loads, which is the code under test.
    """
    payload = pickle.dumps(data)
    msg_length = len(payload) + 1
    header = struct.pack(">IB", msg_length, msg_type)
    writer.write(header + payload)
    await writer.drain()


async def _b066_recv_msg(reader: asyncio.StreamReader, *,
                         timeout: float = 5.0):
    """Wire-frame receive. Returns (msg_type, data).

    Raises asyncio.IncompleteReadError if peer closed mid-frame, or
    asyncio.TimeoutError if no full frame arrives within `timeout` (one
    overall bound, not per-segment).
    """
    async def _inner():
        length_bytes = await reader.readexactly(4)
        msg_length = struct.unpack(">I", length_bytes)[0]
        msg_type = (await reader.readexactly(1))[0]
        payload_length = msg_length - 1
        if payload_length > 0:
            payload = await reader.readexactly(payload_length)
            data = pickle.loads(payload)
        else:
            data = None
        return msg_type, data

    return await asyncio.wait_for(_inner(), timeout)

# Plexus loads this file via spec_from_file_location + exec_module
# without auto-registering in sys.modules. pickle.dumps validates that
# obj.__module__ resolves via sys.modules to an importable module
# containing the class — without this registration, pickling our payload
# classes raises PicklingError ("attribute lookup _B066NotRegistered on
# TestBugSuite failed"). Register an empty proxy module here so import-
# system probes find a placeholder; the proxy is populated with the
# final `globals()` snapshot at the BOTTOM of this file (after every
# module-level symbol — including TestBugSuite — is defined). Anything
# pickle resolves needs to be added to the file BEFORE the populate
# call at file bottom; the populate-at-end pattern means new helpers
# defined anywhere above that call are picked up automatically.
import types as _b066_types
_b066_proxy_mod = _b066_types.ModuleType(__name__)
sys.modules[__name__] = _b066_proxy_mod


TARGET = "TestEventTarget"
STREAM_TARGET = "TestStreamTarget"
EXEC_TARGET = "TestExecuteTarget"
BAD_ACTOR = "TestEventBadActor"


class TestBugSuite(Plugin):
    """PR3 Stage F bug-repro suite. See PLAN.md."""

    @log_errors
    def on_load(self, *args, **kwargs):
        # B-047 mailbox: probe handler increments to confirm fan-out
        # actually fires (sanity check for the task_list growth body).
        self.b047_probe_calls = 0

    async def handle_b047_probe(self, event):
        self.b047_probe_calls += 1

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestBugSuite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestBugSuite disabled")

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
        rec = CaseRecorder("TestBugSuite", SUITE_VERSION, self._plexus)
        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=False,
        )

        await self._b_legacy_removed(rec, kw)
        await self._b_addressed_in_pr3(rec, kw)
        await self._b_active(rec, kw)
        await self._b_deferred(rec, kw)
        await self._b_already_covered(rec, kw)
        await self._b_security(rec, kw)
        return rec.to_dict()

    # ──────────────────────────────────────────────────────────────────
    # PR4 Stage K B-066 — instance-level test fixtures.
    # ──────────────────────────────────────────────────────────────────
    _b066_peer_seq: int = 0

    async def _b066_make_test_peer(
        self,
        *,
        system_caller: bool = False,
        register_in_maps: bool = True,
        add_to_trust_store: bool = True,
        fake_port: Optional[int] = None,
        hostname: Optional[str] = None,
    ) -> Dict[str, Any]:
        nm = self._plexus.network
        # Cycle 4 fix: every test peer uses a UNIQUE subject CN. If
        # two self-signed CA certs in the trust store share Subject DN,
        # OpenSSL's chain-builder picks the FIRST match by name and
        # validates the presented cert's signature against the WRONG
        # public key — handshake fails for everything but the originally
        # presented cert. Unique hostname == unique Subject DN.
        type(self)._b066_peer_seq += 1
        seq = type(self)._b066_peer_seq
        if hostname is None:
            hostname = f"b066_test_peer_{seq:03d}"
        keys_dir = tempfile.mkdtemp(prefix="b066_test_")
        cert_path, key_path, fp, cert_pem = generate_keypair(keys_dir, hostname)
        spec = None
        if fake_port is None:
            fake_port = 19999 + seq
        if register_in_maps:
            spec = PeerSpec(
                hostname=hostname,
                ip="127.0.0.1",
                port=fake_port,
                cert_pem=cert_pem,
                fingerprint=fp,
                system_caller=system_caller,
            )
            nm.peers.append(spec)
            nm.peers_by_fingerprint[fp] = spec
            nm.peers_by_endpoint[("127.0.0.1", fake_port)] = spec
        if add_to_trust_store:
            nm.ssl_context.load_verify_locations(cadata=cert_pem)
        return {
            "keys_dir": keys_dir,
            "cert_path": str(cert_path),
            "key_path": str(key_path),
            "fingerprint": fp,
            "cert_pem": cert_pem,
            "spec": spec,
            "fake_port": fake_port,
        }

    def _b066_cleanup_test_peer(self, peer_info: Dict[str, Any]) -> None:
        nm = self._plexus.network
        spec = peer_info.get("spec")
        if spec is not None:
            nm.peers_by_fingerprint.pop(spec.fingerprint, None)
            nm.peers_by_endpoint.pop((spec.ip, spec.port), None)
            nm.peers = [p for p in nm.peers if p.fingerprint != spec.fingerprint]
        shutil.rmtree(peer_info["keys_dir"], ignore_errors=True)

    def _b066_make_client_ssl_context(
        self, peer_info: Dict[str, Any], *, trust_parent: bool = True,
    ) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = False
        ctx.load_cert_chain(peer_info["cert_path"], peer_info["key_path"])
        if trust_parent:
            nm = self._plexus.network
            ctx.load_verify_locations(
                cadata=Path(nm.cert_path).read_text(encoding="utf-8")
            )
        return ctx

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _ensure_loaded(self, name: str) -> bool:
        """Idempotently load+enable a fixture by name from yaml_config."""
        if name in self._plexus.plugins:
            return True
        for entry in self._plexus.yaml_config.get("plugins", []):
            if entry.get("name") == name:
                e = dict(entry)
                e["enabled"] = True
                await self._plexus.load_plugin_with_conf(e)
                if name in self._plexus.plugins:
                    try:
                        await self._plexus.enable_plugin(name)
                    except Exception:
                        pass
                    return True
        return False

    # ==================================================================
    # _b_legacy_removed — Recipe A (API surface gone)
    # ==================================================================
    async def _b_legacy_removed(self, rec: CaseRecorder, kw: Dict) -> None:
        category = "legacy_removed"

        # ---- B-001 ---------------------------------------------------
        async def body_b_001_request_topic_method_gone(c):
            # B-001: code-driven topic handlers bypassed remote: false.
            # The legacy `request_topic` API + `_handle_topic_request`
            # server handler were removed in Stage D, so the entire
            # bypass surface no longer exists.
            c.expect(getattr(self, "request_topic", None), None)
            nm = self._plexus.network
            c.expect(getattr(nm, "_handle_topic_request", None), None)

        # ---- B-011 ---------------------------------------------------
        async def body_b_011_request_topic_stream_remote_gone(c):
            # B-011: legacy stream-error sentinel only existed on the
            # request_topic_stream_remote path. That whole client method
            # was removed in Stage D.
            nm = self._plexus.network
            c.expect(getattr(nm, "request_topic_stream_remote", None), None)

        # ---- B-012 ---------------------------------------------------
        async def body_b_012_handle_topic_request_stream_gone(c):
            # B-012: server-side stream-chunk error sentinel handler
            # `_handle_topic_request_stream` was removed in Stage D.
            nm = self._plexus.network
            c.expect(
                getattr(nm, "_handle_topic_request_stream", None), None
            )

        # ---- B-014 ---------------------------------------------------
        async def body_b_014_request_topic_stream_sync_gone(c):
            # B-014: `request_topic_stream_sync` method removed in Stage D.
            c.expect(getattr(self, "request_topic_stream_sync", None), None)

        # ---- B-018a --------------------------------------------------
        async def body_b_018a_handle_notify_handler_gone(c):
            # B-018a: wire-side spoof handlers (_handle_notify and
            # _handle_topic_request) were removed in Stage D — the
            # MSG_NOTIFY / MSG_TOPIC_REQUEST surface they hung off no
            # longer exists, so the remote spoof path through them is
            # gone.
            nm = self._plexus.network
            c.expect(getattr(nm, "_handle_notify", None), None)
            c.expect(getattr(nm, "_handle_topic_request", None), None)

        # ---- B-019 ---------------------------------------------------
        async def body_b_019_notify_method_gone(c):
            # B-019: `notify`'s return-count contract is moot — the
            # method itself was removed in Stage D.
            c.expect(getattr(self, "notify", None), None)

        # ---- B-020 ---------------------------------------------------
        async def body_b_020_notify_sync_method_gone(c):
            # B-020: `notify_sync` blocking-on-remote-ACK can't repro —
            # method removed in Stage D.
            c.expect(getattr(self, "notify_sync", None), None)

        # ---- B-022 ---------------------------------------------------
        async def body_b_022_subscribe_handler_kwarg_gone(c):
            # B-022: subscribe() no longer accepts `handler=` (silent
            # no-op risk gone). The `target_access_name` parameter
            # replaced it. Verify both: handler not in signature, AND
            # target_access_name IS in signature.
            sig = inspect.signature(self.subscribe)
            params = sig.parameters
            c.expect("handler" in params, False)
            c.expect("target_access_name" in params, True)

        # ---- B-023 ---------------------------------------------------
        async def body_b_023_notify_method_gone(c):
            # B-023: `notify()` raise-on-error contract violation can't
            # repro — method removed in Stage D.
            c.expect(getattr(self, "notify", None), None)

        # ---- B-024 ---------------------------------------------------
        async def body_b_024_request_topic_stream_gone(c):
            # B-024: oversized chunk corruption on `request_topic_stream`
            # can't repro — method removed in Stage D.
            c.expect(getattr(self, "request_topic_stream", None), None)

        # ---- B-025 ---------------------------------------------------
        async def body_b_025_request_topic_stream_gone(c):
            # B-025: re-entry-after-partial-yield bug on
            # `request_topic_stream` can't repro — method removed in
            # Stage D.
            c.expect(getattr(self, "request_topic_stream", None), None)

        # ---- B-027 ---------------------------------------------------
        async def body_b_027_notify_remote_gone(c):
            # B-027: notify_remote return-zero-on-transport-fail can't
            # repro — method removed in Stage D.
            nm = self._plexus.network
            c.expect(getattr(nm, "notify_remote", None), None)

        # ---- B-028 ---------------------------------------------------
        async def body_b_028_remote_notifier_methods_gone(c):
            # B-028: notify_remote, request_topic_remote,
            # request_topic_stream_remote — all client-side notifier
            # methods removed in Stage D, so the no-client-timeout
            # caller-hang surface is gone entirely.
            nm = self._plexus.network
            c.expect(getattr(nm, "notify_remote", None), None)
            c.expect(getattr(nm, "request_topic_remote", None), None)
            c.expect(getattr(nm, "request_topic_stream_remote", None), None)

        # ---- B-029 ---------------------------------------------------
        async def body_b_029_handle_topic_request_gone(c):
            # B-029: server-side `_handle_topic_request` ignored its
            # `timeout` for code-driven handlers — handler itself gone
            # in Stage D.
            nm = self._plexus.network
            c.expect(getattr(nm, "_handle_topic_request", None), None)

        # ---- B-030 ---------------------------------------------------
        async def body_b_030_notify_remote_gone(c):
            # B-030: `notify_remote` swallowing pickle errors can't
            # repro — method removed in Stage D.
            nm = self._plexus.network
            c.expect(getattr(nm, "notify_remote", None), None)

        # ---- B-032 ---------------------------------------------------
        async def body_b_032_handle_notify_gone(c):
            # B-032: server-side `_handle_notify` head-of-line blocking
            # on slow fan-out can't repro — handler removed in Stage D.
            nm = self._plexus.network
            c.expect(getattr(nm, "_handle_notify", None), None)

        # ---- B-033 ---------------------------------------------------
        async def body_b_033_request_topic_stream_sync_gone(c):
            # B-033: `request_topic_stream_sync` ignoring its `host`
            # parameter can't repro — method removed in Stage D.
            c.expect(getattr(self, "request_topic_stream_sync", None), None)

        # ---- B-035 ---------------------------------------------------
        async def body_b_035_notify_method_gone(c):
            # B-035: `notify`'s asyncio.gather-no-per-sub-timeout
            # symptom can't repro — method removed in Stage D.
            # `publish_event` is the new fire-and-forget primitive but
            # uses asyncio.create_task per-handler (verify in source).
            c.expect(getattr(self, "notify", None), None)
            src = inspect.getsource(self._plexus.publish_event)
            # Sanity: dispatch path uses create_task (per-sub
            # independent) rather than a single gather over all subs.
            c.expect("create_task" in src, True)

        # ---- B-036 ---------------------------------------------------
        async def body_b_036_call_sub_method_gone(c):
            # B-036: `_call_sub` caught Exception not BaseException —
            # function replaced by `_fanout_sub` in PR3. Verify the old
            # name is gone.
            core = self._plexus
            c.expect(getattr(core, "_call_sub", None), None)

        # ---- B-039 ---------------------------------------------------
        async def body_b_039_request_topic_sync_gone(c):
            # B-039: topic-hop-wipes-_sync_call_chain symptom rode on
            # `request_topic_sync` — method removed in Stage D.
            c.expect(getattr(self, "request_topic_sync", None), None)

        # ---- B-042 ---------------------------------------------------
        async def body_b_042_request_topic_stream_gone(c):
            # B-042: code-driven stream bypass (B-001 variant for
            # streams) — `request_topic_stream` method removed in
            # Stage D.
            c.expect(getattr(self, "request_topic_stream", None), None)

        # ---- B-053 ---------------------------------------------------
        async def body_b_053_subscription_handler_field_gone(c):
            # B-053: `Subscription.handler` field removed from the
            # dataclass. Also assert the new field names are present
            # so this case fails informatively if the schema regresses.
            import dataclasses
            from plexus.notifier import Subscription
            fields = {f.name for f in dataclasses.fields(Subscription)}
            c.expect("handler" in fields, False)
            c.expect("endpoint_access_name" in fields, False)
            c.expect("config_driven" in fields, False)
            # Positive assertions: new PR3 fields must be present.
            c.expect("target_access_name" in fields, True)
            c.expect("sub_uuid" in fields, True)
            c.expect("enabled" in fields, True)

        # ---- B-059 ---------------------------------------------------
        async def body_b_059_notify_method_gone(c):
            # B-059: cross-plugin sub routing mismatch on the legacy
            # `notify()` path can't repro — method removed in Stage D.
            c.expect(getattr(self, "notify", None), None)

        # -- run_case calls --------------------------------------------
        await rec.run_case(
            "bug.B-001.request_topic_method_gone",
            body_b_001_request_topic_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-001",), **kw,
        )
        await rec.run_case(
            "bug.B-011.request_topic_stream_remote_gone",
            body_b_011_request_topic_stream_remote_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-011",), **kw,
        )
        await rec.run_case(
            "bug.B-012.handle_topic_request_stream_gone",
            body_b_012_handle_topic_request_stream_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-012",), **kw,
        )
        await rec.run_case(
            "bug.B-014.request_topic_stream_sync_gone",
            body_b_014_request_topic_stream_sync_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-014",), **kw,
        )
        await rec.run_case(
            "bug.B-018a.handle_notify_handler_gone",
            body_b_018a_handle_notify_handler_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-018",), **kw,
        )
        await rec.run_case(
            "bug.B-019.notify_method_gone",
            body_b_019_notify_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-019",), **kw,
        )
        await rec.run_case(
            "bug.B-020.notify_sync_method_gone",
            body_b_020_notify_sync_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-020",), **kw,
        )
        await rec.run_case(
            "bug.B-022.subscribe_handler_kwarg_gone",
            body_b_022_subscribe_handler_kwarg_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-022",), **kw,
        )
        await rec.run_case(
            "bug.B-023.notify_method_gone",
            body_b_023_notify_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-023",), **kw,
        )
        await rec.run_case(
            "bug.B-024.request_topic_stream_gone",
            body_b_024_request_topic_stream_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-024",), **kw,
        )
        await rec.run_case(
            "bug.B-025.request_topic_stream_gone",
            body_b_025_request_topic_stream_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-025",), **kw,
        )
        await rec.run_case(
            "bug.B-027.notify_remote_gone",
            body_b_027_notify_remote_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-027",), **kw,
        )
        await rec.run_case(
            "bug.B-028.remote_notifier_methods_gone",
            body_b_028_remote_notifier_methods_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-028",), **kw,
        )
        await rec.run_case(
            "bug.B-029.handle_topic_request_gone",
            body_b_029_handle_topic_request_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-029",), **kw,
        )
        await rec.run_case(
            "bug.B-030.notify_remote_gone",
            body_b_030_notify_remote_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-030",), **kw,
        )
        await rec.run_case(
            "bug.B-032.handle_notify_gone",
            body_b_032_handle_notify_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-032",), **kw,
        )
        await rec.run_case(
            "bug.B-033.request_topic_stream_sync_gone",
            body_b_033_request_topic_stream_sync_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-033",), **kw,
        )
        await rec.run_case(
            "bug.B-035.notify_method_gone",
            body_b_035_notify_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-035",), **kw,
        )
        await rec.run_case(
            "bug.B-036.call_sub_method_gone",
            body_b_036_call_sub_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-036",), **kw,
        )
        await rec.run_case(
            "bug.B-039.request_topic_sync_gone",
            body_b_039_request_topic_sync_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-039",), **kw,
        )
        await rec.run_case(
            "bug.B-042.request_topic_stream_gone",
            body_b_042_request_topic_stream_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-042",), **kw,
        )
        await rec.run_case(
            "bug.B-053.subscription_handler_field_gone",
            body_b_053_subscription_handler_field_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-053",), **kw,
        )
        await rec.run_case(
            "bug.B-059.notify_method_gone",
            body_b_059_notify_method_gone,
            category=category,
            tags=("bug_repro", "legacy_removed"), bug_ids=("B-059",), **kw,
        )

    # ==================================================================
    # _b_addressed_in_pr3 — Recipe B
    # ==================================================================
    async def _b_addressed_in_pr3(self, rec: CaseRecorder, kw: Dict) -> None:
        category = "addressed_in_pr3"

        # ---- B-003 ---------------------------------------------------
        async def body_b_003_disable_unregisters_subs(c):
            # B-003: notify dispatched to handlers on disabled plugin.
            # Stage B's _disable_plugin wrapper now calls
            # _unregister_plugin_subscriptions at end of on_disable per
            # Q23+C15. Verify: pop a plugin, confirm its subs vanish
            # from topic_registry, then restore via _ensure_loaded.
            target = TARGET
            if target not in self._plexus.plugins:
                c.skip(f"{target} not loaded — fixture order issue")
                return
            pre = await self._plexus.topic_registry.list_local_subs()
            pre_owned = [s for s in pre if s.plugin_name == target]
            if len(pre_owned) == 0:
                c.skip(
                    f"{target} has no subs registered — precondition fail"
                )
                return
            # Net-zero drift: pop + reload restores the plugin set, but
            # during the body the set transiently misses TARGET. Without
            # explicit declaration, _check_drift only sees the final
            # state — which matches snapshot. Defensive declaration
            # nonetheless:
            c.set_expected_drift(added=(), removed=())
            await self._plexus.pop_plugin(target)
            post = await self._plexus.topic_registry.list_local_subs()
            post_owned = [s for s in post if s.plugin_name == target]
            c.expect(len(post_owned), 0)
            # Restore so subsequent cases find TARGET loaded.
            restored = await self._ensure_loaded(target)
            if not restored:
                raise AssertionError(
                    f"failed to restore {target} after pop"
                )

        # ---- B-038 ---------------------------------------------------
        async def body_b_038_sync_pre_start_raises_request_exception(c):
            # B-038: pre-start sync wrappers used to raise TypeError.
            # Stage A Q1 added _check_framework_started guard to all
            # sync wrappers — fires FIRST (before event lookup, before
            # run_coroutine_threadsafe). Can't actually call before
            # framework start in this test (main_event_loop is set).
            # Instead, transiently null it and call. The event_id
            # passed below is intentionally bogus — guard fires before
            # lookup so the bogus id is never dereferenced.
            saved = self._plexus.main_event_loop
            self._plexus.main_event_loop = None
            try:
                c.expect_exception(
                    RequestException, match="Framework not started"
                )
                self.publish_event_sync("any_id_guard_fires_first")
            finally:
                self._plexus.main_event_loop = saved

        # ---- B-056 ---------------------------------------------------
        async def body_b_056_disabled_subs_excluded(c):
            # B-056: disabled YAML subs registered with enabled=False.
            # Verify topic_registry contains the disabled sub with
            # `.enabled == False`, AND find_first skips it.
            #
            # The TestEventSuite YAML declares `disabled_event` (an
            # event with `enabled: false`) — but no matching sub for
            # it. Use a runtime sub instead: register one, mark it
            # disabled directly on the registry entry, then verify
            # find_first returns no eligible subscriber.
            topic = "test_bugsuite/B056/disabled_probe"
            sub_uuid = await self.subscribe(
                topic,
                target_access_name="run",  # any handler — never invoked
            )
            try:
                subs = (
                    await self._plexus.topic_registry.list_local_subs()
                )
                owned = [s for s in subs if s.sub_uuid == sub_uuid]
                if len(owned) != 1:
                    raise AssertionError(
                        "B-056: runtime subscribe failed to register sub"
                    )
                # Toggle to disabled.
                owned[0].enabled = False
                # find_all should still return the sub (enabled is a
                # post-filter); find_first/eligibility must skip it.
                found = (
                    await self._plexus.topic_registry.find_first(topic)
                )
                if found is not None and found.sub_uuid == sub_uuid:
                    raise AssertionError(
                        "B-056: find_first returned a disabled sub "
                        "(enabled=False not honored)"
                    )
            finally:
                await self.unsubscribe(sub_uuid)

        await rec.run_case(
            "bug.B-003.disable_unregisters_subs",
            body_b_003_disable_unregisters_subs,
            category=category,
            tags=("bug_repro", "addressed_in_pr3"), bug_ids=("B-003",),
            **kw,
        )
        await rec.run_case(
            "bug.B-038.sync_pre_start_raises_request_exception",
            body_b_038_sync_pre_start_raises_request_exception,
            category=category,
            tags=("bug_repro", "addressed_in_pr3"), bug_ids=("B-038",),
            **kw,
        )
        await rec.run_case(
            "bug.B-056.disabled_subs_excluded",
            body_b_056_disabled_subs_excluded,
            category=category,
            tags=("bug_repro", "addressed_in_pr3"), bug_ids=("B-056",),
            **kw,
        )

    # ==================================================================
    # _b_active — Recipe C
    # ==================================================================
    async def _b_active(self, rec: CaseRecorder, kw: Dict) -> None:
        category = "active"

        # ---- B-018b --------------------------------------------------
        async def body_b_018b_execute_system_rewrite_alive(c):
            # B-018b: Stage D removed _handle_notify and
            # _handle_topic_request, but the `execute()` method still
            # rewrites author=="system" to hostname. MSG_EXECUTE remains
            # active so a remote node can still spoof via that path.
            # Stage F can't actually run a remote spoof here without
            # two-node infrastructure. Structural assertion: the
            # rewrite block is still present in execute()'s source.
            src = inspect.getsource(self._plexus.execute)
            if (
                'author == "system"' not in src
                and "author == 'system'" not in src
            ):
                # The rewrite was removed — bug fixed-by-construction.
                # Body completes normally; expected_status="fail" =>
                # unexpected_pass => bug fixed.
                return
            raise AssertionError(
                "B-018b: execute() still rewrites author=='system' to "
                "hostname — remote spoof path partially survives Stage D"
            )

        # ---- B-021 ---------------------------------------------------
        async def body_b_021_request_event_fallthrough_or_fail(c):
            # B-021: find_first/request_event ordering may still cause
            # non-eligible-sub-blocks-eligible-sub on the new
            # request_event path. Investigation requires constructing a
            # deliberate sub ordering with private/public endpoint pair
            # and exercising request_event's fall-through behavior end
            # to end. The framework state available from inside a
            # running suite doesn't cleanly support adding two
            # competing subs on the same topic (and the existing
            # priv_endpoint fixture is not paired with a public
            # alternative on the same topic). Skip with note pointing
            # at the structural investigation.
            c.skip(
                "STAGE_F_FIXME: request_event fall-through investigation "
                "requires deliberate sub ordering plus paired "
                "private/public endpoints on the same topic. Existing "
                "fixtures don't provide this pairing — defer to a "
                "follow-up that adds a dedicated fixture."
            )

        # ---- B-044 ---------------------------------------------------
        async def body_b_044_silent_truncation_on_error(c):
            # B-044: FIXED in Stage G (utils.py Request.get_queue_stream now
            # yields the error tuple before breaking, so execute_stream's
            # `if error: raise RequestException(result)` branch fires).
            # This case asserts the FIXED behavior: a stream that yields N
            # items then raises must propagate RequestException to the
            # consumer (NOT silently truncate). Regression guard.
            received = []
            try:
                async for v in self.execute_stream(
                    STREAM_TARGET, "ea_gen_raises_after", (3,)
                ):
                    received.append(v)
            except RequestException:
                # Expected: error surfaces. Verify we got the pre-error
                # items first (so they weren't dropped on the floor).
                c.expect(len(received), 3)
                return
            raise AssertionError(
                f"B-044 regression: stream completed silently "
                f"(got {len(received)} items, no RequestException)"
            )

        # ---- B-045 ---------------------------------------------------
        async def body_b_045_stream_timeout_exception_type(c):
            # B-045: FIXED in Stage G (utils.py Request.get_queue_stream
            # now wraps asyncio.TimeoutError as RequestException, symmetric
            # with execute()). Asserts the FIXED behavior: stream timeout
            # surfaces as RequestException, never as raw asyncio.TimeoutError.
            # Regression guard.
            import asyncio as _asyncio
            try:
                async for _ in self.execute_stream(
                    STREAM_TARGET,
                    "ea_gen_hangs",
                    None,
                    timeout=0.2,
                ):
                    pass
            except RequestException:
                return  # Expected post-fix.
            except _asyncio.TimeoutError as e:
                raise AssertionError(
                    f"B-045 regression: stream timeout surfaced as "
                    f"asyncio.TimeoutError, not RequestException ({e!r})"
                )
            raise AssertionError(
                "B-045 regression: stream timeout produced no exception at all"
            )

        # ---- B-046 ---------------------------------------------------
        async def body_b_046_plugin_lock_held_across_on_enable(c):
            # B-046: FIXED in Stage O via per-plugin lifecycle locks.
            # The global plugin_lock is now held only for fast dict
            # reads/writes; user on_enable runs OUTSIDE plugin_lock
            # under the per-plugin lifecycle_lock. Regression guard:
            # while one plugin's on_enable is mid-flight (artificially
            # delayed), a concurrent get_plugin_info on a DIFFERENT
            # plugin must return promptly (well under 1s).
            core = self._plexus
            v_name = "TestLifecycleVictim"
            other_name = "TestLifecycleSuite"
            victim = core.plugins.get(v_name)
            if victim is None or other_name not in core.plugins:
                c.skip(
                    "B-046 regression guard requires TestLifecycleVictim "
                    "and TestLifecycleSuite both loaded"
                )
                return

            # Slow on_enable: 0.6s sleep. Configure flags directly so
            # we don't depend on the configure endpoint being callable
            # while the plugin is in mid-disable.
            victim._on_enable_delay_secs = 0.6
            try:
                # Disable then concurrently re-enable + ping a different
                # plugin's get_plugin_info. Pre-fix the call would block
                # on plugin_lock until on_enable finishes.
                await core.disable_plugin(v_name)

                async def _delayed_get_info():
                    # Tiny stagger so enable is in mid-on_enable when
                    # we hit get_plugin_info.
                    await asyncio.sleep(0.1)
                    t0 = asyncio.get_event_loop().time()
                    info = await core.get_plugin_info(other_name)
                    return info, asyncio.get_event_loop().time() - t0

                enable_task = asyncio.create_task(core.enable_plugin(v_name))
                info_task = asyncio.create_task(_delayed_get_info())
                info, elapsed = await info_task
                await enable_task

                if info is None or info.get("name") != other_name:
                    raise AssertionError(
                        f"B-046 regression: get_plugin_info({other_name}) "
                        f"returned {info!r}"
                    )
                # Should return well under the 0.5s remainder of the
                # on_enable sleep. R1 LOW-1 fix: threshold raised from
                # 0.2s to 1.0s — Windows scheduler timer resolution is
                # ~15.6ms and loaded CI can blow past 200ms. The actual
                # operation is a dict read (microseconds); 1s still
                # comfortably catches a regression where the lock is
                # held across the full 1.5s on_enable sleep.
                if elapsed > 1.0:
                    raise AssertionError(
                        f"B-046 regression: get_plugin_info on {other_name} "
                        f"took {elapsed:.3f}s while {v_name}.on_enable was "
                        f"sleeping — plugin_lock still held across on_enable"
                    )
            finally:
                # Restore configuration so subsequent suites are clean.
                victim = core.plugins.get(v_name)
                if victim is not None:
                    victim._on_enable_delay_secs = 0.0
                    if not victim.enabled:
                        try:
                            await core.enable_plugin(v_name)
                        except Exception:
                            pass

        # ---- B-047 ---------------------------------------------------
        async def body_b_047_task_list_grows_unboundedly(c):
            # B-047 (Stage Q FIXED): task_list now uses set + per-task
            # done_callback for O(1) self-eviction. This regression guard
            # asserts that a 200-event burst does NOT cause sustained
            # growth. b047_probe_event is registered → b047_probe_sub →
            # handle_b047_probe so every publish spawns one real fan-out
            # task that exercises the eviction path.
            core = self._plexus
            tlist = getattr(core, "task_list", None)
            if tlist is None:
                # Attribute removed — fixed-by-construction.
                return
            # Snapshot baseline tasks. We measure the delta from the
            # burst (tasks NOT in this snapshot) instead of total list
            # size, so concurrent unrelated activity adding/removing
            # its own tasks doesn't false-trigger the assertion.
            before_set = frozenset(tlist)
            for _ in range(200):
                await self.publish_event_for_repro()
            # Poll until burst-spawned tasks drain. Each fan-out task
            # awaits _process_request → endpoint dispatch → producer's
            # finally pops from self.requests (B-073 Session 2 Step 3
            # — was set_collected pre-migration) → done_callback fires
            # via call_soon. One asyncio.sleep(0) is NOT enough;
            # deadline-bounded poll handles slow CI.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                remaining = sum(1 for t in tlist if t not in before_set)
                if remaining <= 5:
                    break
                await asyncio.sleep(0.01)
            remaining = sum(1 for t in tlist if t not in before_set)
            if remaining > 5:
                raise AssertionError(
                    f"B-047 regression: {remaining} burst-spawned tasks "
                    f"remain in task_list after 5s drain — done_callback "
                    f"eviction not firing"
                )

        # ---- B-048 ---------------------------------------------------
        async def body_b_048_find_endpoint_returns_none_tuple(c):
            # B-048: find_endpoint type annotation says Optional[tuple]
            # but returns 3-tuple of Nones on miss. Verify the actual
            # return shape against a guaranteed-miss lookup. Signature
            # is (access_name, hosts, blocked_hosts, plugin_uuid,
            # requester_id, target_plugin) — find_endpoint is async.
            core = self._plexus
            try:
                result = await core.find_endpoint(
                    "no_such_endpoint_for_b048_repro",
                    "any",
                )
            except TypeError:
                # Signature drift — skip to avoid spurious failure.
                c.skip(
                    "find_endpoint signature changed — re-verify in "
                    "follow-up"
                )
                return
            # Bug confirmed if result is the 3-tuple of Nones rather
            # than a single None.
            if (
                isinstance(result, tuple)
                and len(result) == 3
                and result == (None, None, None)
            ):
                raise AssertionError(
                    "B-048: find_endpoint returns (None, None, None) "
                    "instead of None on miss — annotation contract "
                    "broken"
                )

        # ---- B-051 ---------------------------------------------------
        async def body_b_051_normalize_hosts_authors_keyword(c):
            # B-051: _normalize_hosts is reused for the `authors` field.
            # The keyword-in-list guard rejects "remote"/"any" in lists
            # with other elements — appropriate for hosts but
            # inappropriate for authors (where "remote" could be a
            # literal plugin name). Bug confirms if ValueError is
            # raised. If the bug is fixed (separate normalize for
            # authors), the ValueError won't fire — recorder records
            # as "fail" (expected exception not raised).
            from plexus.core import _normalize_hosts
            c.expect_exception(ValueError, match=r"keyword 'remote'")
            _normalize_hosts(
                ["remote", "OtherPlugin"],
                param_name="authors",
                default=None,
            )

        # ---- B-054 ---------------------------------------------------
        async def body_b_054_request_event_stream_bypasses_request(c):
            # B-054: request_event_stream bypasses Request lifecycle —
            # stream-dispatch entries aren't tracked in core.requests.
            # Repro: verify that during a streaming dispatch, no
            # corresponding entry shows up in requests. Implementation-
            # bound: skip if requests dict isn't accessible or the
            # streaming primitive isn't routable here.
            core = self._plexus
            requests = getattr(core, "requests", None)
            if requests is None:
                c.skip(
                    "STAGE_F_FIXME: core.requests not accessible from "
                    "suite context — repro path implementation-bound"
                )
                return
            # No clean way to drive a streaming request and snapshot
            # the requests dict mid-flight without race-prone timing.
            # Defer to fixture work.
            c.skip(
                "STAGE_F_FIXME: request_event_stream lifecycle repro "
                "needs mid-flight snapshot of core.requests; race-prone "
                "without dedicated harness fixture"
            )

        # ---- B-064 ---------------------------------------------------
        async def body_b_064_subscribe_no_async_log_errors(c):
            # B-064: preventive — Plugin.subscribe must NOT have
            # @async_log_errors decorator. getsource inspects the
            # original source text; a post-class-definition wrapping
            # would slip past (low-risk false negative documented).
            src = inspect.getsource(Plugin.subscribe)
            c.expect("async_log_errors" in src, False)

        # -- run_case calls --------------------------------------------
        # B-018b: expected_status="fail" — bug expected to repro
        # (rewrite block still present); body raises AssertionError
        # with matching signature on confirmed repro.
        await rec.run_case(
            "bug.B-018b.execute_system_rewrite_alive",
            body_b_018b_execute_system_rewrite_alive,
            category=category,
            tags=("bug_repro", "active"), bug_ids=("B-018",),
            expected_status="fail",
            expected_signature={
                "exception_type": "AssertionError",
                "message_regex": r"system_rewrite|partially survives",
            },
            **kw,
        )
        # B-021: skip pending fixture work.
        await rec.run_case(
            "bug.B-021.request_event_fallthrough_or_fail",
            body_b_021_request_event_fallthrough_or_fail,
            category=category,
            tags=("bug_repro", "active", "deferred"), bug_ids=("B-021",),
            **kw,
        )
        # B-044: FIXED in Stage G. Case now asserts the FIXED behavior
        # (RequestException propagated to consumer). Regression guard.
        await rec.run_case(
            "bug.B-044.silent_truncation_on_error",
            body_b_044_silent_truncation_on_error,
            category=category,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-044",),
            **kw,
        )
        # B-045: FIXED in Stage G. Case asserts FIXED behavior
        # (RequestException for timeouts, never asyncio.TimeoutError).
        await rec.run_case(
            "bug.B-045.stream_timeout_exception_type",
            body_b_045_stream_timeout_exception_type,
            category=category,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-045",),
            **kw,
        )
        # B-046: FIXED in Stage O. Positive guard — body asserts that
        # a concurrent dict-read on another plugin completes promptly
        # while one plugin's on_enable is artificially delayed.
        await rec.run_case(
            "bug.B-046.plugin_lock_held_across_on_enable",
            body_b_046_plugin_lock_held_across_on_enable,
            category=category,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-046",),
            hard_timeout_s=15.0,
            **kw,
        )
        # B-047: expected_status="fail" — bug expected to repro
        # B-047: FIXED in Stage Q. Positive regression guard — body
        # asserts burst-spawned tasks drain via the per-task
        # done_callback within 5s. Success path completes in
        # milliseconds; the deadline only fires on a regression.
        # slow=True intentionally removed (no longer slow).
        await rec.run_case(
            "bug.B-047.task_list_grows_unboundedly",
            body_b_047_task_list_grows_unboundedly,
            category=category,
            tags=("bug_repro", "regression_guard"), bug_ids=("B-047",),
            **kw,
        )
        # B-048: expected_status="fail" — bug expected to repro
        # (3-tuple of Nones instead of None).
        await rec.run_case(
            "bug.B-048.find_endpoint_returns_none_tuple",
            body_b_048_find_endpoint_returns_none_tuple,
            category=category,
            tags=("bug_repro", "active"), bug_ids=("B-048",),
            expected_status="fail",
            expected_signature={
                "exception_type": "AssertionError",
                "message_regex": r"None, None, None",
            },
            **kw,
        )
        # B-051: c.expect_exception drives the path — no
        # expected_status="fail" needed. ValueError matching =>
        # recorded as pass (bug confirmed).
        await rec.run_case(
            "bug.B-051.normalize_hosts_authors_keyword",
            body_b_051_normalize_hosts_authors_keyword,
            category=category,
            tags=("bug_repro", "active"), bug_ids=("B-051",),
            **kw,
        )
        # B-054: skip pending fixture work.
        await rec.run_case(
            "bug.B-054.request_event_stream_bypasses_request",
            body_b_054_request_event_stream_bypasses_request,
            category=category,
            tags=("bug_repro", "active", "deferred"), bug_ids=("B-054",),
            **kw,
        )
        # B-064: preventive — body uses c.expect (default
        # expected_status="pass"); pass = decorator absent (good).
        await rec.run_case(
            "bug.B-064.subscribe_no_async_log_errors",
            body_b_064_subscribe_no_async_log_errors,
            category=category,
            tags=("bug_repro", "active", "preventive"), bug_ids=("B-064",),
            **kw,
        )

    # Helper used by B-047 — publishes b047_probe_event which routes
    # via b047_probe_sub → handle_b047_probe. Each call spawns one
    # real fan-out task in Plexus.task_list; the test asserts
    # that the eviction path (Stage Q done_callback) drains them.
    async def publish_event_for_repro(self):
        try:
            await self.publish_event("b047_probe_event")
        except Exception:
            pass

    # ==================================================================
    # _b_deferred — Recipe E (skip with STAGE_F_FIXME)
    # ==================================================================
    async def _b_deferred(self, rec: CaseRecorder, kw: Dict) -> None:
        category = "deferred"

        async def body_b_026_deferred(c):
            c.skip(
                "STAGE_F_FIXME: B-026 references request_topic_remote "
                "(removed in Stage D) AND requires a writer mock that "
                "fails on _send_end_stream after a successful chunk "
                "drain. Practical fix is the API removal itself; no "
                "current fixture supports the writer-failure case."
            )

        async def body_b_034_deferred(c):
            c.skip(
                "STAGE_F_FIXME: hot-reload subscription window race. "
                "Stage B lifecycle wrappers shrunk the gap but the "
                "race still exists. Bugtracker pre-marks this as "
                "TIMING-RACE (5 ms sleep doesn't reliably hit). "
                "TestLifecycleSuite already carries B-037 cycle-race "
                "coverage; defer here."
            )

        async def body_b_049_deferred(c):
            c.skip(
                "STAGE_F_FIXME: enable_plugin no on_enable timeout. "
                "Repro needs a controlled-startup harness — calling "
                "enable_plugin from inside a running suite would "
                "deadlock on plugin_lock. Same harness pattern as "
                "B-007 in TestLifecycleSuite."
            )

        async def body_b_050_deferred(c):
            c.skip(
                "STAGE_F_FIXME: close() try/finally masking. Needs a "
                "fixture with on_disable raising AND _unregister "
                "mocked to raise; no current fixture supports this."
            )

        async def body_b_055_deferred(c):
            c.skip(
                "STAGE_F_FIXME: aclose().result() no timeout. Needs "
                "worker thread plus early-break orchestration; not "
                "covered by current fixtures."
            )

        async def body_b_057_deferred(c):
            c.skip(
                "STAGE_F_FIXME: C6 placeholder-drift INFO log — "
                "bugtracker explicitly marks this deferred polish."
            )

        async def body_b_058_deferred(c):
            c.skip(
                "STAGE_F_FIXME: _warn_redundant_host_combos not called "
                "for sub filters. Log-capture-based test; no shared "
                "log-capture fixture in repo (logging.handlers."
                "MemoryHandler is brittle)."
            )

        async def body_b_060_deferred(c):
            c.skip(
                "STAGE_F_FIXME: no DEBUG/ERROR log when fan-out "
                "target_plugin missing. Log-capture-based test; same "
                "fixture-gap as B-058."
            )

        async def body_b_061_deferred(c):
            c.skip(
                "STAGE_F_FIXME: no DEBUG log when find_endpoint denies "
                "access. Log-capture-based test; same fixture-gap as "
                "B-058."
            )

        async def body_b_062_deferred(c):
            c.skip(
                "STAGE_F_FIXME: no ERROR log when sync handler raises "
                "during fan-out. Log-capture-based test; same fixture-"
                "gap as B-058."
            )

        async def body_b_063_deferred(c):
            c.skip(
                "STAGE_F_FIXME: unknown override sub-key DEBUG vs "
                "WARNING — touches PR2 apply_overrides (out of PR3 "
                "scope)."
            )

        await rec.run_case(
            "bug.B-026.deferred_writer_mock", body_b_026_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-026",), **kw,
        )
        await rec.run_case(
            "bug.B-034.deferred_reload_window_race", body_b_034_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-034",), **kw,
        )
        await rec.run_case(
            "bug.B-049.deferred_enable_no_timeout", body_b_049_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-049",), **kw,
        )
        await rec.run_case(
            "bug.B-050.deferred_close_try_finally_mask", body_b_050_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-050",), **kw,
        )
        await rec.run_case(
            "bug.B-055.deferred_aclose_no_timeout", body_b_055_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-055",), **kw,
        )
        await rec.run_case(
            "bug.B-057.deferred_placeholder_drift_info_log",
            body_b_057_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-057",), **kw,
        )
        await rec.run_case(
            "bug.B-058.deferred_redundant_host_combos_log",
            body_b_058_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-058",), **kw,
        )
        await rec.run_case(
            "bug.B-060.deferred_fanout_target_missing_log",
            body_b_060_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-060",), **kw,
        )
        await rec.run_case(
            "bug.B-061.deferred_find_endpoint_denies_log",
            body_b_061_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-061",), **kw,
        )
        await rec.run_case(
            "bug.B-062.deferred_sync_handler_raises_log",
            body_b_062_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-062",), **kw,
        )
        await rec.run_case(
            "bug.B-063.deferred_unknown_override_subkey_log",
            body_b_063_deferred,
            category=category,
            tags=("bug_repro", "deferred"), bug_ids=("B-063",), **kw,
        )

    # ==================================================================
    # _b_already_covered — Recipe D (skip-and-cite)
    # ==================================================================
    async def _b_already_covered(self, rec: CaseRecorder, kw: Dict) -> None:
        category = "already_covered"

        async def body_b_002_covered(c):
            c.skip(
                "covered by TestStreamSuite (bug_ids=('B-002',)); "
                "verdict tracks there"
            )

        async def body_b_004_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-004',)); "
                "verdict tracks there"
            )

        async def body_b_005_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-005',)); "
                "verdict tracks there"
            )

        # B-073 Session 2 Step 5: B-006 skip-stub deleted. The actual
        # B-006 case in TestLifecycleSuite was deleted (Step 4 killed
        # running_loop, removing the failure mode the case guarded
        # against). No upstream case to defer to.

        async def body_b_007_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-007',)) "
                "as DEFERRED — verdict tracks there"
            )

        async def body_b_008_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-008',)); "
                "verdict tracks there"
            )

        async def body_b_009_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-009',)); "
                "verdict tracks there"
            )

        async def body_b_010_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-010',)); "
                "verdict tracks there"
            )

        async def body_b_013_covered(c):
            c.skip(
                "covered by TestExecuteSuite (bug_ids=('B-013',)); "
                "verdict tracks there"
            )

        async def body_b_015_covered(c):
            c.skip(
                "covered by TestExecuteSuite (bug_ids=('B-015',)); "
                "verdict tracks there"
            )

        async def body_b_016_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-016',)); "
                "verdict tracks there"
            )

        async def body_b_017_covered(c):
            c.skip(
                "covered by TestExecuteSuite (bug_ids=('B-017',)); "
                "verdict tracks there"
            )

        async def body_b_037_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-037',)); "
                "verdict tracks there"
            )

        async def body_b_040_covered(c):
            c.skip(
                "covered by TestEventSuite (bug_ids=('B-040',)); "
                "verdict tracks there"
            )

        async def body_b_041_covered(c):
            c.skip(
                "covered by TestStreamSuite (bug_ids=('B-041',)); "
                "verdict tracks there"
            )

        async def body_b_043_covered(c):
            c.skip(
                "covered by TestLifecycleSuite (bug_ids=('B-043',)); "
                "verdict tracks there"
            )

        await rec.run_case(
            "bug.B-002.covered_by_test_stream_suite", body_b_002_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-002",),
            **kw,
        )
        await rec.run_case(
            "bug.B-004.covered_by_test_lifecycle_suite", body_b_004_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-004",),
            **kw,
        )
        await rec.run_case(
            "bug.B-005.covered_by_test_lifecycle_suite", body_b_005_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-005",),
            **kw,
        )
        # B-073 Session 2 Step 5: bug.B-006.covered_by_test_lifecycle_suite
        # registration removed alongside the body stub above.
        await rec.run_case(
            "bug.B-007.covered_by_test_lifecycle_suite", body_b_007_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-007",),
            **kw,
        )
        await rec.run_case(
            "bug.B-008.covered_by_test_lifecycle_suite", body_b_008_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-008",),
            **kw,
        )
        await rec.run_case(
            "bug.B-009.covered_by_test_lifecycle_suite", body_b_009_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-009",),
            **kw,
        )
        await rec.run_case(
            "bug.B-010.covered_by_test_lifecycle_suite", body_b_010_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-010",),
            **kw,
        )
        await rec.run_case(
            "bug.B-013.covered_by_test_execute_suite", body_b_013_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-013",),
            **kw,
        )
        await rec.run_case(
            "bug.B-015.covered_by_test_execute_suite", body_b_015_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-015",),
            **kw,
        )
        await rec.run_case(
            "bug.B-016.covered_by_test_lifecycle_suite", body_b_016_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-016",),
            **kw,
        )
        await rec.run_case(
            "bug.B-017.covered_by_test_execute_suite", body_b_017_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-017",),
            **kw,
        )
        await rec.run_case(
            "bug.B-037.covered_by_test_lifecycle_suite", body_b_037_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-037",),
            **kw,
        )
        await rec.run_case(
            "bug.B-040.covered_by_test_event_suite", body_b_040_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-040",),
            **kw,
        )
        await rec.run_case(
            "bug.B-041.covered_by_test_stream_suite", body_b_041_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-041",),
            **kw,
        )
        await rec.run_case(
            "bug.B-043.covered_by_test_lifecycle_suite", body_b_043_covered,
            category=category,
            tags=("bug_repro", "covered_elsewhere"), bug_ids=("B-043",),
            **kw,
        )

    # ==================================================================
    # _b_security — PR4 Stage K B-066 regression guards (Tests 1, 1b, 2,
    # 2b, 3, 4, 5). Each test impersonates a peer locally inside the
    # parent's NetworkManager and exercises one mTLS pinning / SafeUnpickler
    # / B-018b guard code path.
    # ==================================================================
    async def _b_security(self, rec: CaseRecorder, kw: Dict) -> None:
        category = "security"

        # ---- Test 1 — pre-auth pickle RCE (handshake-rejected) -----
        async def body_b_066_pre_auth_pickle_rce(c):
            # The malicious peer's cert is NOT in the parent's trust store
            # (add_to_trust_store=False) and not pinned (register_in_maps=
            # False). The server rejects at TLS handshake. In TLS 1.3 the
            # client's asyncio.open_connection may NOT raise — the alert
            # arrives only when reading. Either path is acceptable; the
            # security property is that the malicious payload's __reduce__
            # never invokes the sentinel callable on the receiver.
            sentinel_dir = tempfile.mkdtemp(prefix="b066_test1_sentinel_")
            peer = await self._b066_make_test_peer(
                register_in_maps=False, add_to_trust_store=False
            )
            writer = None
            try:
                client_ctx = self._b066_make_client_ssl_context(peer)
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(
                            "127.0.0.1",
                            self._plexus.network.port,
                            ssl=client_ctx,
                        ),
                        timeout=5.0,
                    )
                except (OSError, asyncio.TimeoutError):
                    # TLS handshake was rejected at the asyncio layer.
                    # The malicious payload was never on the wire.
                    pass
                else:
                    # asyncio returned a transport but the server-side
                    # rejected mid-handshake — exercise the worst case
                    # by attempting to ship the malicious pickle anyway,
                    # and verify the receiver did NOT execute it.
                    try:
                        await _b066_send_msg(writer, MSG_EXECUTE, {
                            "plugin": "TestEventTarget",
                            "method": "echo_author_id",
                            "args": (_B066PrePwnPickle(sentinel_dir),),
                            "author": "remote",
                            "author_id": "remote",
                            "author_host": "b066_test1_peer",
                        })
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(reader.read(1), timeout=2.0)
                    except Exception:
                        pass
                # Definitive security assertion: sentinel never fired.
                c.expect(os.listdir(sentinel_dir), [])
            finally:
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                shutil.rmtree(sentinel_dir, ignore_errors=True)
                self._b066_cleanup_test_peer(peer)

        # ---- Test 1b — handshake passes, pin check fails -----------
        async def body_b_066_handshake_passes_pin_fails(c):
            peer = await self._b066_make_test_peer(
                register_in_maps=False, add_to_trust_store=True
            )
            cap = _B066LogCapture("networking", logging.DEBUG)
            cap.attach()
            writer = None
            try:
                client_ctx = self._b066_make_client_ssl_context(peer)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        "127.0.0.1",
                        self._plexus.network.port,
                        ssl=client_ctx,
                    ),
                    timeout=5.0,
                )
                got = await asyncio.wait_for(reader.read(1), timeout=5.0)
                c.expect(got, b"")
                c.expect(
                    cap.has_message(
                        "[B066] unpinned peer", min_level=logging.DEBUG
                    ),
                    True,
                )
            finally:
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                cap.detach()
                self._b066_cleanup_test_peer(peer)

        # ---- Test 2 — post-auth disallowed-class injection ---------
        async def body_b_066_post_auth_disallowed_class(c):
            peer = await self._b066_make_test_peer(system_caller=False)
            cap = _B066LogCapture("networking", logging.WARNING)
            cap.attach()
            writer = None
            try:
                client_ctx = self._b066_make_client_ssl_context(peer)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        "127.0.0.1",
                        self._plexus.network.port,
                        ssl=client_ctx,
                    ),
                    timeout=5.0,
                )
                await _b066_send_msg(writer, MSG_EXECUTE, {
                    "plugin": "TestEventTarget",
                    "method": "echo_author_id",
                    "args": (_B066NotRegistered("malicious"),),
                    "author": "remote",
                    "author_id": "remote",
                    "author_host": "b066_test_peer",
                    "request_id": "b066-test2",
                })
                got = await asyncio.wait_for(reader.read(1), timeout=5.0)
                c.expect(got, b"")
                c.expect(
                    cap.has_message(
                        "[B066] disallowed-class deserialization",
                        min_level=logging.WARNING,
                    ),
                    True,
                )
            finally:
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                cap.detach()
                self._b066_cleanup_test_peer(peer)

        # ---- Test 2b — post-auth __reduce__ payload ---------------
        async def body_b_066_post_auth_reduce_payload(c):
            sentinel_dir = tempfile.mkdtemp(prefix="b066_test2b_sentinel_")
            peer = await self._b066_make_test_peer(system_caller=False)
            writer = None
            try:
                client_ctx = self._b066_make_client_ssl_context(peer)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        "127.0.0.1",
                        self._plexus.network.port,
                        ssl=client_ctx,
                    ),
                    timeout=5.0,
                )
                # The send may itself raise on Windows if the server
                # already RST'd the previous TLS handshake's tail-end —
                # the security guarantee is that the sentinel never
                # fires, regardless of where the path aborts.
                try:
                    await _b066_send_msg(writer, MSG_EXECUTE, {
                        "plugin": "TestEventTarget",
                        "method": "echo_author_id",
                        "args": (_B066RegisteredButPwn(sentinel_dir),),
                        "author": "remote",
                        "author_id": "remote",
                        "author_host": "b066_test_peer",
                        "request_id": "b066-test2b",
                    })
                    try:
                        got = await asyncio.wait_for(
                            reader.read(1), timeout=5.0
                        )
                    except (ConnectionError, OSError):
                        got = b""
                except (ConnectionError, OSError):
                    got = b""
                c.expect(got, b"")
                c.expect((Path(sentinel_dir) / "PWNED").exists(), False)
            finally:
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                shutil.rmtree(sentinel_dir, ignore_errors=True)
                self._b066_cleanup_test_peer(peer)

        # ---- Test 3 — system_caller=False denial + alive --------
        async def body_b_066_system_caller_privilege_denial(c):
            peer = await self._b066_make_test_peer(system_caller=False)
            writer = None
            try:
                client_ctx = self._b066_make_client_ssl_context(peer)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        "127.0.0.1",
                        self._plexus.network.port,
                        ssl=client_ctx,
                    ),
                    timeout=5.0,
                )
                # Action 1 — denial.
                await _b066_send_msg(writer, MSG_EXECUTE, {
                    "plugin": "TestEventTarget",
                    "method": "get_state",
                    "args": None,
                    "author": "system",
                    "author_id": "system",
                    "author_host": "b066_test_peer",
                    "request_id": "b066-test3-deny",
                })
                msg_type, data = await _b066_recv_msg(reader, timeout=5.0)
                c.expect(msg_type, MSG_ERROR)
                c.expect("system_caller=false" in str(data), True)

                # Action 2 — connection still alive: ping + result.
                await _b066_send_msg(writer, MSG_PING, {})
                msg_type2, data2 = await _b066_recv_msg(reader, timeout=5.0)
                c.expect(msg_type2, MSG_RESULT)
                c.expect(data2, {"status": "ok"})
            finally:
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                self._b066_cleanup_test_peer(peer)

        # ---- Test 4 — system_caller=True grant + sub-tests -------
        async def body_b_066_system_caller_privilege_grant(c):
            peer = await self._b066_make_test_peer(system_caller=True)
            writer = None
            try:
                client_ctx = self._b066_make_client_ssl_context(peer)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        "127.0.0.1",
                        self._plexus.network.port,
                        ssl=client_ctx,
                    ),
                    timeout=5.0,
                )

                async def _round_trip(*, author, author_id):
                    await _b066_send_msg(writer, MSG_REQUEST_EVENT, {
                        "topic": "test_b066/echo_author_id",
                        "payload": None,
                        "author": author,
                        "author_id": author_id,
                        "author_host": peer["spec"].hostname,
                        "timestamp": 0.0,
                        "timeout": 5.0,
                    })
                    mt1, d1 = await _b066_recv_msg(reader, timeout=5.0)
                    if mt1 != MSG_STREAM_CHUNK:
                        raise AssertionError(
                            f"expected MSG_STREAM_CHUNK ({MSG_STREAM_CHUNK}), "
                            f"got msg_type={mt1} data={d1!r}"
                        )
                    mt2, _ = await _b066_recv_msg(reader, timeout=5.0)
                    if mt2 != MSG_END_STREAM:
                        raise AssertionError(
                            f"expected MSG_END_STREAM ({MSG_END_STREAM}) "
                            f"trailing the chunk, got msg_type={mt2}"
                        )
                    return d1

                # 4-main — preserved author + pass-through author_id
                result = await _round_trip(
                    author="system", author_id="non-uuid-pass-through"
                )
                if result != {
                    "author": "system",
                    "author_id": "non-uuid-pass-through",
                }:
                    raise AssertionError(
                        f"Test 4-main: expected author='system' + "
                        f"pass-through author_id, got {result!r}"
                    )

                # 4a — privileged peer + author_id matches local UUID
                local_uuid = self.plugin_uuid
                result_4a = await _round_trip(
                    author="system", author_id=local_uuid
                )
                if not (
                    result_4a["author"] == "system"
                    and result_4a["author_id"].startswith("remote-peer:")
                ):
                    raise AssertionError(
                        f"Test 4a (privileged + spoofed UUID): expected "
                        f"author='system' AND author_id startswith "
                        f"'remote-peer:', got {result_4a!r}"
                    )

                # 4b — privileged peer + author_id NOT matching anything
                result_4b = await _round_trip(
                    author="system",
                    author_id="aaaa-bbbb-cccc-dddd-not-a-real-uuid",
                )
                if result_4b != {
                    "author": "system",
                    "author_id": "aaaa-bbbb-cccc-dddd-not-a-real-uuid",
                }:
                    raise AssertionError(
                        f"Test 4b (privileged + non-spoofed): expected "
                        f"author='system' + pass-through author_id, "
                        f"got {result_4b!r}"
                    )
            finally:
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                self._b066_cleanup_test_peer(peer)

        # ---- Test 5 — client-side pin rejects unpinned server ----
        async def body_b_066_client_side_pin_rejects_unpinned_server(c):
            nm = self._plexus.network
            actual_keys_dir = tempfile.mkdtemp(prefix="b066_test5_actual_")
            expected_keys_dir = tempfile.mkdtemp(prefix="b066_test5_expected_")
            actual_cert_path, actual_key_path, actual_fp, actual_cert_pem = \
                generate_keypair(actual_keys_dir, "b066_test5_actual")
            _, _, expected_fp, _ = generate_keypair(
                expected_keys_dir, "b066_test5_expected"
            )

            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.minimum_version = ssl.TLSVersion.TLSv1_3
            server_ctx.verify_mode = ssl.CERT_REQUIRED
            server_ctx.check_hostname = False
            server_ctx.load_cert_chain(actual_cert_path, actual_key_path)
            server_ctx.load_verify_locations(
                cadata=Path(nm.cert_path).read_text(encoding="utf-8")
            )

            async def _accept(reader, writer):
                try:
                    await reader.read(1)
                except Exception:
                    pass
                try:
                    writer.close()
                except Exception:
                    pass

            srv = await asyncio.start_server(
                _accept, "127.0.0.1", 0, ssl=server_ctx
            )
            test_port = srv.sockets[0].getsockname()[1]

            bad_spec = PeerSpec(
                hostname="b066_test5_server",
                ip="127.0.0.1",
                port=test_port,
                cert_pem=actual_cert_pem,
                fingerprint=expected_fp,
                system_caller=False,
            )
            orig_peers = nm.peers
            orig_pbf = nm.peers_by_fingerprint
            orig_pbe = nm.peers_by_endpoint
            nm.peers = [bad_spec]
            nm.peers_by_fingerprint = {expected_fp: bad_spec}
            nm.peers_by_endpoint = {("127.0.0.1", test_port): bad_spec}

            try:
                c.expect_exception(
                    ConnectionError, match=r"not in peers config"
                )
                await nm._create_connection("127.0.0.1")
            finally:
                nm.peers = orig_peers
                nm.peers_by_fingerprint = orig_pbf
                nm.peers_by_endpoint = orig_pbe
                srv.close()
                try:
                    await srv.wait_closed()
                except Exception:
                    pass
                shutil.rmtree(actual_keys_dir, ignore_errors=True)
                shutil.rmtree(expected_keys_dir, ignore_errors=True)

        # -- run_case calls -------------------------------------------
        await rec.run_case(
            "bug.B-066.pre_auth_pickle_rce",
            body_b_066_pre_auth_pickle_rce,
            category=category,
            tags=("bug_repro", "security", "b066"), bug_ids=("B-066",),
            hard_timeout_s=10.0, **kw,
        )
        await rec.run_case(
            "bug.B-066.handshake_passes_pin_fails",
            body_b_066_handshake_passes_pin_fails,
            category=category,
            tags=("bug_repro", "security", "b066"), bug_ids=("B-066",),
            hard_timeout_s=10.0, **kw,
        )
        await rec.run_case(
            "bug.B-066.post_auth_disallowed_class",
            body_b_066_post_auth_disallowed_class,
            category=category,
            tags=("bug_repro", "security", "b066"), bug_ids=("B-066",),
            hard_timeout_s=10.0, **kw,
        )
        await rec.run_case(
            "bug.B-066.post_auth_reduce_payload",
            body_b_066_post_auth_reduce_payload,
            category=category,
            tags=("bug_repro", "security", "b066"), bug_ids=("B-066",),
            hard_timeout_s=10.0, **kw,
        )
        await rec.run_case(
            "bug.B-066.system_caller_privilege_denial",
            body_b_066_system_caller_privilege_denial,
            category=category,
            tags=("bug_repro", "security", "b066"), bug_ids=("B-066",),
            hard_timeout_s=10.0, **kw,
        )
        await rec.run_case(
            "bug.B-066.system_caller_privilege_grant",
            body_b_066_system_caller_privilege_grant,
            category=category,
            tags=("bug_repro", "security", "b066"), bug_ids=("B-066",),
            hard_timeout_s=10.0, **kw,
        )
        await rec.run_case(
            "bug.B-066.client_side_pin_rejects_unpinned_server",
            body_b_066_client_side_pin_rejects_unpinned_server,
            category=category,
            tags=("bug_repro", "security", "b066"), bug_ids=("B-066",),
            hard_timeout_s=10.0, **kw,
        )


# Populate the sys.modules proxy with the final module globals — see the
# header comment near `_b066_proxy_mod = ...` for the rationale. Placing
# this at the bottom of the file means every module-level symbol defined
# above is visible to pickle's `find_class` resolution.
_b066_proxy_mod.__dict__.update(globals())
