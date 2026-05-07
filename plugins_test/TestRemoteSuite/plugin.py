"""TestRemoteSuite — Phase 5.

Brings up a peer node as a subprocess (plugins_test/_remote_node/run_node.py)
and drives wire-bug repros against it. If networking is disabled OR the
subprocess fails to come up, all cases are recorded as skip with a clear
reason.

KNOWN FIXTURE GAPS (2026-04-28, after first end-to-end run with networking on):
- TestRemoteVictim has plugin-level remote=False on purpose (it's the bug
  target for B-001 / B-042 — code-driven sub on a remote=False plugin).
  But its readback endpoints (get_bypass_count / reset_bypass) inherit
  remote=False, so the parent CANNOT call them via execute(hosts="remote").
  Catch-22: testing the bypass requires reading a counter that's only
  reachable via the bypass we're testing. Fix path: split into two plugins
  — Victim with remote=False holds the topic sub; a separate remote=True
  plugin exposes the readback. Several B-001 / B-042 / B-018 cases
  currently fail with "Endpoint reset_bypass not found" until that split
  lands.
- B-028.no_client_timeout case calls execute(timeout=2.0) — but execute_remote
  DOES have request-level timeout. B-028 is specifically about
  publish_event_remote / request_event_remote NOT having client-side
  timeout. The case body needs to call those APIs directly to repro.
- access_false_blocked case expects RequestException for a remote=True +
  accessible_by_other_plugins=False endpoint. find_endpoint only checks
  accessible_by_other_plugins for LOCAL cross-plugin calls; remote callers
  pass through plugin.remote + endpoint.remote. The test expectation is
  wrong; either redesign or remove the case.
- B-020.publish_event_sync_blocks_on_remote subnode has no sub on
  test/r/hang topic, so publish_event_sync doesn't block waiting for any
  remote handler. Need a hanging sub on the subnode to repro the bug.

Phase 5.1 cleanup: redesign these fixtures to repro the bugs they claim.

Phase 5 cases all declare hosts=["remote"]. The recorder auto-skips a remote
sub-case when remote_available is False. The suite passes that flag based on
subprocess startup success.

Cases (~22):
- remote.execute.remote_false_blocked / .access_false_blocked
- remote.publish_event.remote_false_blocked_for_config
- remote.B-001.code_driven_bypass
- remote.B-042.code_driven_stream_bypass
- remote.B-018.spoof_system_string / .spoof_known_uuid (skipped — Stage E)
- remote.B-019.publish_event_count_per_node_not_per_sub
- remote.B-021.first_sub_not_remote_eligible
- remote.B-024.huge_item / .B-025.partial_then_failover
- remote.B-011.stream_error_sentinel_via_item_end
- remote.B-012.stream_error_sentinel_via_end_stream
- remote.B-028.no_client_timeout
- remote.B-029.code_driven_timeout_ignored
- remote.B-030.unpicklable_args
- remote.B-027.publish_event_return_count_misleading
- remote.B-032.head_of_line_blocking
- remote.B-033.request_event_stream_sync_host_remote
- remote.B-020.publish_event_sync_blocks_on_remote
- remote.find_endpoints_by_tag
- edge: tag.no_matches / tag.mixed_local_remote / pool.exhaustion / discovery.race
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


SUITE_VERSION = "0.2.0"

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

        async def body_access_false_blocked(c):
            from exceptions import RequestException
            c.expect_exception(RequestException, match=r"[Ee]ndpoint.*not found")
            await self.execute(
                "TestRemoteTarget", "r_remote_only", {"value": "x"},
                hosts=c.hosts,
            )

        async def body_publish_event_remote_false_blocked_for_config(c):
            await self.execute("TestRemoteVictim", "reset_bypass", hosts=c.hosts)
            await self.publish_event(
                "r_local", payload={"data": "x"}, hosts=c.hosts,
            )
            cnt = await self.execute(
                "TestRemoteVictim", "get_bypass_count", hosts=c.hosts,
            )
            c.expect(cnt, 0)

        async def body_b001_code_driven_bypass(c):
            await self.execute("TestRemoteVictim", "reset_bypass", hosts=c.hosts)
            await self.publish_event(
                "r_code", payload={"data": "bypass"}, hosts=c.hosts,
            )
            cnt = await self.execute(
                "TestRemoteVictim", "get_bypass_count", hosts=c.hosts,
            )
            if cnt > 0:
                c.set_marker("bypass_succeeded")
                raise AssertionError(
                    f"B-001: code-driven sub on remote=False plugin fired "
                    f"({cnt} times) via remote publish_event"
                )

        async def body_b042_code_driven_stream_bypass(c):
            await self.execute("TestRemoteVictim", "reset_bypass", hosts=c.hosts)
            try:
                async for _ in self.request_event_stream(
                    "r_code_stream", hosts=c.hosts,
                ):
                    pass
            except Exception:
                pass
            cnt = await self.execute(
                "TestRemoteVictim", "get_stream_bypass_count", hosts=c.hosts,
            )
            if cnt > 0:
                c.set_marker("bypass_succeeded")
                raise AssertionError(
                    f"B-042: code-driven async-gen sub on remote=False "
                    f"plugin yielded {cnt} items via remote stream"
                )

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

        async def body_b028_no_client_timeout(c):
            await c.assert_hang(
                self.execute(
                    "TestRemoteTarget", "r_hang",
                    hosts=c.hosts, timeout=2.0,
                ),
                timeout_s=4.0,
                marker="outer_wait_for_fired",
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
            ("remote.execute.access_false_blocked",
             body_access_false_blocked, ("access",), ()),
            ("remote.publish_event.remote_false_blocked_for_config",
             body_publish_event_remote_false_blocked_for_config,
             ("access",), ()),
            ("remote.B-001.code_driven_bypass",
             body_b001_code_driven_bypass,
             ("bug_repro", "security"), ("B-001",)),
            ("remote.B-042.code_driven_stream_bypass",
             body_b042_code_driven_stream_bypass,
             ("bug_repro", "security"), ("B-042",)),
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
            ("remote.B-028.no_client_timeout",
             body_b028_no_client_timeout,
             ("bug_repro",), ("B-028",)),
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
             ("bug_repro",), ("B-020",)),
            ("remote.find_endpoints_by_tag", body_find_endpoints_by_tag,
             ("discovery", "basic"), ()),
        ]

        for case_id, body, tags, bug_ids in cases:
            extra: Dict[str, Any] = {}
            if "bug_repro" in tags:
                # Each bug-repro case needs an expected_status="fail" with
                # a signature; we pin marker "bypass_succeeded" or
                # "stream_aborted" or "outer_wait_for_fired" as appropriate.
                # When skipped (no peer) the recorder records skip without
                # checking signature.
                if "B-024" in bug_ids:
                    extra = {
                        "expected_status": "fail",
                        "expected_signature": {"marker": "stream_aborted"},
                    }
                elif "B-028" in bug_ids:
                    extra = {
                        "expected_status": "fail",
                        "expected_signature": {"marker": "outer_wait_for_fired"},
                    }
                elif "B-020" in bug_ids:
                    # Stage M (PR4): B-020 verified FIXED-BY-CONSTRUCTION.
                    # Stage D removed notify_sync; replacement
                    # publish_event_sync has different contract.
                    pass  # extra stays {} — case passes as positive regression guard
                elif "B-001" in bug_ids or "B-042" in bug_ids or "B-018" in bug_ids:
                    extra = {
                        "expected_status": "fail",
                        "expected_signature": {"marker": "bypass_succeeded"},
                    }
                # Other bug_repro cases skip via c.skip(...) inside the body
                # so they don't need expected_status="fail" wiring.
            await rec.run_case(
                case_id, body,
                hosts=("remote",),
                tags=tags, bug_ids=bug_ids,
                hard_timeout_s=30.0,
                **extra,
                **kw,
            )
