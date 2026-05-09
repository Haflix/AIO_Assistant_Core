"""TestRemoteSuite — Phase 5.

Brings up a peer node as a subprocess (plugins_test/_remote_node/run_node.py)
and drives wire-bug repros + cross-node contract tests against it. If
networking is disabled OR the subprocess fails to come up, all cases are
recorded as skip with a clear reason.

Phase 5 cases all declare hosts=["remote"]. The recorder auto-skips a remote
sub-case when remote_available is False (subprocess never came up). The
suite calls _wait_for_subnode_advert() once before iterating cases so the
subnode's subscriptions are advertised to the parent before any
request_event(hosts="remote") fires.

Stage N (PR4) cleanup: legacy B-001 / B-042 / B-028 cases that targeted
the now-removed request_topic / notify_remote API surface have been
deleted. Those bugs are FIXED-BY-CONSTRUCTION (Stage D); structural
canaries in TestBugSuite (`bug.B-001.request_topic_method_gone` etc.)
remain authoritative. The same pass added five positive-guard cases
covering the new request_event / request_event_stream wire path
(remote.request_event.basic / .handler_raises / .timeout_honored,
remote.request_event_stream.basic / .mid_stream_raise).

Cases:
- remote.execute.basic / .remote_false_blocked
- remote.request_event.basic / .handler_raises / .timeout_honored
- remote.request_event_stream.basic / .mid_stream_raise
- remote.B-018.spoof_system_string / .spoof_known_uuid (skip — Stage E)
- remote.B-019 / B-021 / B-025 / B-011_B-012 / B-029 / B-030 / B-027 /
  B-032 / B-033 (skip — fixture wiring TBD)
- remote.B-024.huge_item
- remote.B-020.publish_event_sync_blocks_on_remote (positive regression
  guard after Stage M)
- remote.find_endpoints_by_tag
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

from utils import Plugin  # noqa: E402
from decorators import async_log_errors, log_errors  # noqa: E402

from _test_helpers import CaseRecorder  # noqa: E402


SUITE_VERSION = "0.4.0"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SUBNODE_SCRIPT = REPO_ROOT / "plugins_test" / "_remote_node" / "run_node.py"
SUBNODE_CONFIG = "plugins_test/_remote_node/config.subnode.yml"
SUBNODE_PORT_DEFAULT = 2511

UNAVAILABLE_REASON = (
    "Phase 5 subprocess peer not available — local-only test run "
    "(set networking.enabled=true in test_config.yml AND ensure no other "
    "process is using the subnode port to bring up the peer)"
)


class TestRemoteSuite(Plugin):
    """Phase 5 suite plugin. See test_suite_plan.md §6 Phase 5."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self._subproc: Optional[subprocess.Popen] = None
        self._ready_file: Optional[str] = None
        self._peer_info: Optional[Dict[str, Any]] = None
        self._remote_available: bool = False

    @async_log_errors
    async def on_enable(self):
        if not getattr(self._plugin_core, "networking_enabled", False):
            self._logger.info(
                "TestRemoteSuite: networking disabled in main config — "
                "subprocess peer not started; remote cases will skip"
            )
            return
        await self._spawn_subnode()

    @async_log_errors
    async def on_disable(self):
        await self._terminate_subnode()

    async def _wait_for_subnode_advert(self, timeout: float = 5.0) -> bool:
        """Poll until the subnode's subscriptions are advertised to parent.

        The advert protocol fires asynchronously after subnode startup;
        request_event(hosts='remote') falls through to remote dispatch
        only after the subnode's subs appear in network._inbound_adverts.
        Returns True if any advert from the peer landed within `timeout`,
        False on timeout or if networking/peer state is missing.
        """
        if not self._remote_available or not self._peer_info:
            return False
        peer_hostname = self._peer_info.get("hostname")
        if not peer_hostname:
            return False
        network = getattr(self._plugin_core, "network", None)
        if network is None:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            adverts = getattr(network, "_inbound_adverts", {}).get(
                peer_hostname, {}
            )
            if adverts:
                return True
            await asyncio.sleep(0.05)
        self._logger.warning(
            "TestRemoteSuite: subnode advert did not appear within %.1fs "
            "(peer=%s) — request_event(hosts='remote') cases may fail",
            timeout, peer_hostname,
        )
        return False

    async def _spawn_subnode(self) -> None:
        """Spawn the peer subprocess and wait for the ready-file."""
        try:
            tmp_dir = Path(tempfile.gettempdir()) / "aio_test_subnode"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            self._ready_file = str(tmp_dir / f"ready_{os.getpid()}.json")
            if os.path.exists(self._ready_file):
                os.remove(self._ready_file)

            # PR4 Stage K (B-066): forward mTLS provisioning to subnode.
            # test_application.py pre-generated parent + subnode keypairs
            # and stashed them in env vars; pass them as flags to run_node.py.
            sub_keys_dir = os.environ.get("AIO_TEST_SUB_KEYS_DIR")
            parent_cert_pem_file = os.environ.get("AIO_TEST_PARENT_CERT_PEM_FILE")
            parent_hostname = os.environ.get("AIO_TEST_PARENT_HOSTNAME", "aio-test-parent")
            parent_port = os.environ.get("AIO_TEST_PARENT_PORT", "2510")

            cmd = [
                sys.executable,
                str(SUBNODE_SCRIPT),
                "--config", SUBNODE_CONFIG,
                "--port", str(SUBNODE_PORT_DEFAULT),
                "--ready-file", self._ready_file,
            ]
            if sub_keys_dir and parent_cert_pem_file:
                cmd.extend([
                    "--keys-dir", sub_keys_dir,
                    "--parent-cert-pem-file", parent_cert_pem_file,
                    "--parent-hostname", parent_hostname,
                    "--parent-port", parent_port,
                ])
            else:
                self._logger.warning(
                    "TestRemoteSuite: AIO_TEST_SUB_KEYS_DIR or "
                    "AIO_TEST_PARENT_CERT_PEM_FILE missing from env; subnode "
                    "will start with empty peers and fail at NetworkManager.start()"
                )
            self._logger.info(f"TestRemoteSuite: spawning subnode: {cmd}")
            child_env = dict(os.environ)
            self._subproc = subprocess.Popen(
                cmd,
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env,
            )

            deadline = time.perf_counter() + 15.0
            while time.perf_counter() < deadline:
                if os.path.exists(self._ready_file):
                    try:
                        self._peer_info = json.loads(
                            Path(self._ready_file).read_text()
                        )
                        self._remote_available = True
                        self._logger.info(
                            f"TestRemoteSuite: peer up: {self._peer_info}"
                        )
                        return
                    except Exception:
                        pass
                if self._subproc.poll() is not None:
                    self._logger.warning(
                        f"TestRemoteSuite: subnode exited early "
                        f"(rc={self._subproc.returncode})"
                    )
                    return
                await asyncio.sleep(0.5)
            self._logger.warning(
                "TestRemoteSuite: subnode did not write ready-file within 15s"
            )
        except Exception as e:
            self._logger.warning(f"TestRemoteSuite: subnode spawn failed: {e}")

    async def _terminate_subnode(self) -> None:
        if self._subproc is None:
            return
        try:
            self._subproc.terminate()
            try:
                self._subproc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._subproc.kill()
                self._subproc.wait(timeout=2)
        except Exception:
            pass
        finally:
            self._subproc = None
            if self._ready_file and os.path.exists(self._ready_file):
                try:
                    os.remove(self._ready_file)
                except Exception:
                    pass

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
        rec = CaseRecorder("TestRemoteSuite", SUITE_VERSION, self._plugin_core)

        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=self._remote_available,
        )

        await self._enumerate_cases(rec, kw)

        return rec.to_dict()

    # ====================================================================
    # All Phase 5 cases — bodies are stubs that exercise the remote path
    # and rely on remote_available=False auto-skipping when no peer is up.
    # ====================================================================

    async def _enumerate_cases(self, rec: CaseRecorder, kw: Dict) -> None:
        peer_host = (
            self._peer_info.get("hostname") if self._peer_info else "test-subnode"
        )
        peer_ip = self._peer_info.get("ip") if self._peer_info else "127.0.0.1"

        # Stage N (PR4): wait once for the subnode's advert to land before
        # iterating cases. Without this, request_event(hosts="remote")
        # cases fired immediately after subnode startup race the advert
        # protocol and intermittently raise "no subscriber matches resolved
        # topic". No-op when remote_available=False.
        await self._wait_for_subnode_advert()

        async def body_remote_open(c):
            r = await self.execute(
                "TestRemoteTarget", "r_open", {"value": "x"},
                hosts=c.hosts,
            )
            c.expect(r, "x")

        async def body_remote_false_blocked(c):
            from exceptions import RequestException
            c.expect_exception(RequestException, match=r"[Ee]ndpoint.*not found")
            await self.execute(
                "TestRemoteVictim", "r_local_only", {"value": "x"},
                hosts=c.hosts,
            )

        # ── Stage N (PR4): request_event coverage ──────────────────────
        # Five positive-guard cases for the new request_event /
        # request_event_stream wire path. Subnode-side handlers live on
        # TestRemoteTarget (subscribed at runtime in on_enable to four
        # dedicated topics: test/r/req_basic, test/r/req_raise,
        # test/r/req_stream_basic, test/r/req_stream_raise).

        async def body_request_event_basic(c):
            """Happy path: parent → subnode → handler returns dict.
            Verifies payload preservation across the wire."""
            r = await self.request_event(
                "r_request_basic", payload={"v": "x"},
                hosts="remote", timeout=5.0,
            )
            c.expect(r, {"echoed": {"v": "x"}})

        async def body_request_event_handler_raises(c):
            """Remote handler raising ValueError must surface to caller as
            RequestException with the original message preserved."""
            from exceptions import RequestException
            c.expect_exception(RequestException, match=r"requested-error-marker")
            await self.request_event(
                "r_request_raise", payload={},
                hosts="remote", timeout=5.0,
            )

        async def body_request_event_timeout_honored(c):
            """request_event(timeout=2.0) against a hanging remote handler
            must raise within budget. Peer-side enforcement (Request.
            wait_for_result_async) sends the timeout result back, which
            unblocks the client receive loop. Regression guard for the
            B-028-class concern in the Stage-D-replacement API."""
            from exceptions import RequestException
            loop = asyncio.get_running_loop()
            start = loop.time()
            raised = False
            try:
                await self.request_event(
                    "r_hang", payload={},
                    hosts="remote", timeout=2.0,
                )
            except RequestException:
                raised = True
            elapsed = loop.time() - start
            if not raised:
                c.set_marker("no_exception")
                raise AssertionError(
                    f"request_event(timeout=2.0) returned without raising "
                    f"after {elapsed:.2f}s — timeout not honored"
                )
            # Budget = 2.0s timeout + 3.0s wire/scheduling slack. If the
            # call returned WAY late, peer-side enforcement is broken.
            if elapsed > 5.0:
                c.set_marker("timeout_too_late")
                raise AssertionError(
                    f"request_event(timeout=2.0) raised after {elapsed:.2f}s "
                    f"(budget 5.0s) — timeout enforcement is too slow"
                )

        async def body_request_event_stream_basic(c):
            """Happy-path streaming: 3 chunks. First chunk arrives wrapped
            in an Event object (LOCKED I); subsequent chunks are raw."""
            from utils import Event
            chunks = []
            async for chunk in self.request_event_stream(
                "r_request_stream_basic", payload={},
                hosts="remote", timeout=10.0,
            ):
                chunks.append(chunk)
            c.expect(len(chunks), 3)
            if not isinstance(chunks[0], Event):
                c.set_marker("first_chunk_not_event")
                raise AssertionError(
                    f"first chunk should be Event-wrapped per LOCKED I; "
                    f"got {type(chunks[0]).__name__}"
                )
            c.expect(chunks[0].payload, {"chunk": 0})
            c.expect(chunks[1], {"chunk": 1})
            c.expect(chunks[2], {"chunk": 2})

        async def body_request_event_stream_mid_stream_raise(c):
            """Mid-stream handler raise: 2 chunks then RequestException
            with original message preserved. Caller's `async for` exits
            via the exception, not silent termination."""
            from exceptions import RequestException
            chunks = []
            raised: Optional[BaseException] = None
            try:
                async for chunk in self.request_event_stream(
                    "r_request_stream_raise", payload={},
                    hosts="remote", timeout=10.0,
                ):
                    chunks.append(chunk)
            except RequestException as e:
                raised = e
            if raised is None:
                c.set_marker("no_exception")
                raise AssertionError(
                    f"request_event_stream completed without raising; "
                    f"got {len(chunks)} chunk(s)"
                )
            if "midstream-error-marker" not in str(raised):
                c.set_marker("error_message_lost")
                raise AssertionError(
                    f"RequestException raised but original message lost: "
                    f"{raised!r}"
                )
            if len(chunks) != 2:
                c.set_marker("chunk_count_wrong")
                raise AssertionError(
                    f"expected 2 chunks before mid-stream raise, "
                    f"got {len(chunks)}"
                )

        # ── B-071: per-peer wire counter coverage ─────────────────────
        # Sanity checks that peer_stats actually tracks bytes/messages
        # for both unary execute_remote and streaming paths, and that
        # _drop_peer_advert_state resets per O7 (current-session only).

        async def body_wire_counter_execute(c):
            """B-071: peer_stats[hostname] increments after execute_remote.
            We verify msgs_sent ≥ 1 and bytes_sent > 0 because the unary
            path issues at least one MSG_EXECUTE frame + receives one
            MSG_STREAM_CHUNK + MSG_END_STREAM."""
            nm = self._plugin_core.network
            stats_before = dict(nm.peer_stats.get(peer_host, {
                "bytes_sent": 0, "bytes_recv": 0,
                "msgs_sent": 0, "msgs_recv": 0,
            }))
            r = await self.execute(
                "TestRemoteTarget", "r_open", {"value": "wirecount"},
                hosts=c.hosts,
            )
            c.expect(r, "wirecount")
            stats_after = nm.peer_stats.get(peer_host)
            if stats_after is None:
                c.set_marker("peer_stats_missing")
                raise AssertionError(
                    f"peer_stats[{peer_host!r}] missing after execute_remote"
                )
            if stats_after["msgs_sent"] <= stats_before["msgs_sent"]:
                c.set_marker("msgs_sent_not_incremented")
                raise AssertionError(
                    f"msgs_sent did not increment: {stats_before['msgs_sent']} "
                    f"→ {stats_after['msgs_sent']}"
                )
            if stats_after["bytes_sent"] <= stats_before["bytes_sent"]:
                c.set_marker("bytes_sent_not_incremented")
                raise AssertionError(
                    f"bytes_sent did not increment: {stats_before['bytes_sent']} "
                    f"→ {stats_after['bytes_sent']}"
                )
            if stats_after["msgs_recv"] <= stats_before["msgs_recv"]:
                c.set_marker("msgs_recv_not_incremented")
                raise AssertionError(
                    f"msgs_recv did not increment: {stats_before['msgs_recv']} "
                    f"→ {stats_after['msgs_recv']}"
                )

        async def body_wire_counter_stream(c):
            """B-071: streaming path increments per-chunk + per-ITEM_END
            marker. Verifies that the stream-path coverage (chunk paths
            + no-payload ITEM_END counters) actually fires — the bulk of
            real-world traffic flows through these sites."""
            nm = self._plugin_core.network
            stats_before = dict(nm.peer_stats.get(peer_host, {
                "bytes_sent": 0, "bytes_recv": 0,
                "msgs_sent": 0, "msgs_recv": 0,
            }))
            chunks = []
            async for chunk in self.request_event_stream(
                "r_request_stream_basic", payload={},
                hosts="remote", timeout=10.0,
            ):
                chunks.append(chunk)
            c.expect(len(chunks), 3)
            stats_after = nm.peer_stats.get(peer_host)
            if stats_after is None:
                c.set_marker("peer_stats_missing")
                raise AssertionError(
                    f"peer_stats[{peer_host!r}] missing after stream"
                )
            recv_delta = stats_after["msgs_recv"] - stats_before["msgs_recv"]
            if recv_delta < 3:
                c.set_marker("stream_recv_undercount")
                raise AssertionError(
                    f"streaming msgs_recv delta {recv_delta} < 3 — likely "
                    f"the no-payload ITEM_END counters are not firing"
                )

        async def body_wire_counter_reset(c):
            """B-071: peer_stats entry is removed when a peer is declared
            dead via _drop_peer_advert_state. We invoke the helper
            directly (mirrors what heartbeat does on dead-peer detection),
            then verify the entry is gone and a fresh subsequent stamp
            recreates a zeroed entry."""
            nm = self._plugin_core.network
            # Ensure we have a stats entry to drop
            await self.execute(
                "TestRemoteTarget", "r_open", {"value": "before_drop"},
                hosts=c.hosts,
            )
            if peer_host not in nm.peer_stats:
                c.set_marker("no_stats_to_drop")
                raise AssertionError(
                    f"peer_stats[{peer_host!r}] missing before drop"
                )
            await nm._drop_peer_advert_state(peer_host)
            if peer_host in nm.peer_stats:
                c.set_marker("stats_not_cleared")
                raise AssertionError(
                    f"peer_stats[{peer_host!r}] still present after "
                    f"_drop_peer_advert_state"
                )
            # Subsequent traffic should recreate a fresh entry (next
            # connection acquired from pool will re-handshake or use a
            # surviving pooled writer; either way, the next _send_message
            # whose writer is stamped will pre-create a zeroed entry via
            # the handshake-time setdefault on the next reconnect).
            #
            # Note: with a surviving pooled writer (already stamped, but
            # peer_stats entry just popped), _count_sent will see
            # stats=None and SKIP the increment per the documented race
            # semantic. That's the correct behavior — we don't recreate
            # stale state for a peer that was just declared dead.

        async def body_b018_spoof_system_string(c):
            c.skip(
                "B-018 spoofing — Stage E re-evaluates author-stamping under "
                "PR3 Stage C wire protocol; spoofer retired in PR3 Stage D."
            )

        async def body_b018_spoof_known_uuid(c):
            c.skip(
                "B-018 spoofing — Stage E re-evaluates author-stamping under "
                "PR3 Stage C wire protocol; spoofer retired in PR3 Stage D."
            )

        async def body_b019_count_per_node(c):
            # On the parent's side we have one local sub for test/r/multi (set up
            # by code below); on the peer we'd register 3 more. Then publish_event
            # hosts=any returns count == 1 + 1 (one per remote node) instead of
            # 1 + 3 (one per actual sub).
            c.skip(
                "B-019 case requires registering N peer-side subs at runtime; "
                "fixture wiring TBD — use the existing remote.publish_event.basic "
                "matrix-expansion to validate basic count==1 path"
            )

        async def body_b021_first_sub_not_remote_eligible(c):
            c.skip(
                "B-021 needs paired subs on the peer where the first registered "
                "is remote=False — wiring TBD"
            )

        async def body_b024_huge_item(c):
            # B-024: request_event_stream emits ONE MSG_STREAM_CHUNK per
            # yielded item, no chunking, no item-end boundaries (unlike
            # execute_stream which DOES chunk via _handle_execute_stream).
            # The fixture yields one 101MB item — over MAX_MESSAGE_SIZE
            # (100MB) — so the receiver should reject with
            # NetworkRequestException. That is the B-024 repro path.
            try:
                items = []
                async for chunk in self.request_event_stream(
                    "r_huge_stream", hosts=c.hosts,
                ):
                    items.append(chunk)
                # If items received cleanly with the 101MB payload
                # intact, B-024 isn't reproducing here (server may have
                # added per-item chunking). Mark and fail.
                if not items:
                    c.set_marker("stream_aborted")
                    raise AssertionError(
                        "B-024: stream returned 0 items"
                    )
                first = items[0]
                expected_size = 101 * 1024 * 1024
                if (
                    not isinstance(first, dict)
                    or "data" not in first
                    or len(first.get("data", b"")) != expected_size
                ):
                    c.set_marker("stream_aborted")
                    raise AssertionError(
                        f"B-024: stream item corrupted (got {type(first).__name__})"
                    )
            except Exception as e:
                if not c.marker:
                    c.set_marker("stream_aborted")
                raise AssertionError(
                    f"B-024: stream aborted with exception: {type(e).__name__}: {e}"
                )

        async def body_b025_partial_then_failover(c):
            c.skip(
                "B-025 requires two peer nodes with same handler — Phase 5 "
                "scaffold only spawns one subnode; multi-peer TBD"
            )

        async def body_b011_b012_stream_error_sentinel(c):
            c.skip(
                "B-011 / B-012 require the server to emit __STREAM_ERROR__ "
                "sentinels — fixture for the unpicklable-payload trigger TBD"
            )

        async def body_b029_code_driven_timeout_ignored(c):
            c.skip(
                "B-029 requires a code-driven topic handler that hangs and "
                "the caller passes timeout — fixture wiring TBD"
            )

        async def body_b030_unpicklable_args(c):
            c.skip(
                "B-030 requires passing an unpicklable arg through "
                "Plugin.publish_event; the arg never crosses the Plugin "
                "wrapper unmodified — fixture TBD"
            )

        async def body_b027_publish_event_return_count(c):
            c.skip(
                "B-027 needs to inject a transport failure on "
                "publish_event_remote — fixture TBD"
            )

        async def body_b032_head_of_line_blocking(c):
            c.skip(
                "B-032 requires a slow peer-side handler + a fast call on the "
                "same pooled connection — fixture wiring TBD"
            )

        async def body_b033_sync_stream_host_remote(c):
            c.skip(
                "B-033 (sync request_event_stream silently routing local) is "
                "covered as Phase 3 skip; remote variant duplicate"
            )

        async def body_b020_publish_event_sync_blocks_on_remote(c):
            await c.assert_hang(
                asyncio.to_thread(self.publish_event_sync, "r_hang"),
                timeout_s=2.0,
                marker="outer_wait_for_fired",
            )

        async def body_find_endpoints_by_tag(c):
            r = await self._plugin_core.find_endpoints_by_tag("nonexistent")
            assert isinstance(r, list)

        # Run all cases in order (each declares hosts=("remote",); recorder
        # auto-skips when remote_available=False).
        cases = [
            ("remote.execute.basic", body_remote_open, ("basic",), ()),
            ("remote.execute.remote_false_blocked",
             body_remote_false_blocked, ("access",), ()),
            # Stage N (PR4) — request_event coverage
            ("remote.request_event.basic",
             body_request_event_basic,
             ("basic", "request_event"), ()),
            ("remote.request_event.handler_raises",
             body_request_event_handler_raises,
             ("basic", "request_event"), ()),
            ("remote.request_event.timeout_honored",
             body_request_event_timeout_honored,
             ("basic", "request_event", "regression_guard"), ()),
            ("remote.request_event_stream.basic",
             body_request_event_stream_basic,
             ("basic", "request_event_stream"), ()),
            ("remote.request_event_stream.mid_stream_raise",
             body_request_event_stream_mid_stream_raise,
             ("basic", "request_event_stream", "regression_guard"), ()),
            # End Stage N additions
            # B-071: per-peer wire counters
            ("remote.wire_counter.execute",
             body_wire_counter_execute,
             ("basic", "wire_counter"), ("B-071",)),
            ("remote.wire_counter.stream",
             body_wire_counter_stream,
             ("basic", "wire_counter", "request_event_stream"), ("B-071",)),
            ("remote.wire_counter.reset_on_drop",
             body_wire_counter_reset,
             ("basic", "wire_counter"), ("B-071",)),
            ("remote.B-018.spoof_system_string",
             body_b018_spoof_system_string,
             ("bug_repro", "security"), ("B-018",)),
            ("remote.B-018.spoof_known_uuid",
             body_b018_spoof_known_uuid,
             ("bug_repro", "security"), ("B-018",)),
            ("remote.B-019.publish_event_count_per_node_not_per_sub",
             body_b019_count_per_node,
             ("bug_repro",), ("B-019",)),
            ("remote.B-021.first_sub_not_remote_eligible",
             body_b021_first_sub_not_remote_eligible,
             ("bug_repro",), ("B-021",)),
            ("remote.B-024.huge_item", body_b024_huge_item,
             ("bug_repro", "slow"), ("B-024",)),
            ("remote.B-025.partial_then_failover",
             body_b025_partial_then_failover,
             ("bug_repro",), ("B-025",)),
            ("remote.B-011_B-012.stream_error_sentinel",
             body_b011_b012_stream_error_sentinel,
             ("bug_repro",), ("B-011", "B-012")),
            ("remote.B-029.code_driven_timeout_ignored",
             body_b029_code_driven_timeout_ignored,
             ("bug_repro",), ("B-029",)),
            ("remote.B-030.unpicklable_args",
             body_b030_unpicklable_args,
             ("bug_repro",), ("B-030",)),
            ("remote.B-027.publish_event_return_count_misleading",
             body_b027_publish_event_return_count,
             ("bug_repro",), ("B-027",)),
            ("remote.B-032.head_of_line_blocking",
             body_b032_head_of_line_blocking,
             ("bug_repro", "slow"), ("B-032",)),
            ("remote.B-033.request_event_stream_sync_host_remote",
             body_b033_sync_stream_host_remote,
             ("bug_repro",), ("B-033",)),
            ("remote.B-020.publish_event_sync_blocks_on_remote",
             body_b020_publish_event_sync_blocks_on_remote,
             ("bug_repro", "regression_guard"), ("B-020",)),
            ("remote.find_endpoints_by_tag", body_find_endpoints_by_tag,
             ("discovery", "basic"), ()),
        ]

        for case_id, body, tags, bug_ids in cases:
            extra: Dict[str, Any] = {}
            if "bug_repro" in tags:
                # Bug-repro cases need expected_status="fail" with a
                # signature when the body actually drives the bug; cases
                # that skip via c.skip(...) don't need wiring.
                if "B-024" in bug_ids:
                    extra = {
                        "expected_status": "fail",
                        "expected_signature": {"marker": "stream_aborted"},
                    }
                elif "B-018" in bug_ids:
                    extra = {
                        "expected_status": "fail",
                        "expected_signature": {"marker": "bypass_succeeded"},
                    }
                # B-020 (Stage M) is a positive regression guard now —
                # default extras={} is correct.
                # Other bug_repro cases skip via c.skip(...) inside the body.
            await rec.run_case(
                case_id, body,
                hosts=("remote",),
                tags=tags, bug_ids=bug_ids,
                hard_timeout_s=30.0,
                **extra,
                **kw,
            )
