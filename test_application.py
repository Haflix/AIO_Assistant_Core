"""Standalone runner for the AIO Assistant Core test framework.

Loads test_config.yml (only test-framework plugins enabled), drives
TestRunner.run_all, prints a compact summary, dumps full JSON to disk,
then shuts down. Exit code = 0 if no failed/errored cases AND no
unexpected_passes.

PR4 Stage K (B-066): pre-generates parent + subnode mTLS keypairs at
startup, patches the parent's networking config with the subnode peer
entry BEFORE NetworkManager is constructed, and forwards the subnode
keys + parent cert PEM path to TestRemoteSuite via environment
variables (consumed in TestRemoteSuite._spawn_subnode and passed as
flags to run_node.py).
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from PluginCore import PluginCore
from serialization import generate_keypair


CONFIG_PATH = "test_config.yml"
DUMP_PATH = "_private/test_outputs/phase_1_baseline.json"
RUNNER_PLUGIN = "TestRunner"

# Subnode endpoint default (kept in sync with TestRemoteSuite.SUBNODE_PORT_DEFAULT
# and the smoke harness). Test_application.py provisions a subnode peer entry
# in the parent's networking.peers under this address.
SUBNODE_PORT = 2511
SUBNODE_HOSTNAME = "test-subnode"
PARENT_HOSTNAME = "aio-test-parent"


def _print_summary(report: Dict[str, Any]) -> None:
    s = report.get("summary", {})
    print()
    print("=" * 72)
    print(
        f"Test framework {report.get('framework_version', '?')}  "
        f"({report.get('duration_ms', 0):.0f} ms)"
    )
    print("=" * 72)
    print(
        f"Suites: {s.get('suites_passed', 0)} passed, "
        f"{s.get('suites_failed', 0)} failed"
    )
    print(
        f"Cases:  {s.get('passed', 0)} passed | "
        f"{s.get('failed', 0)} failed | "
        f"{s.get('errored', 0)} errored | "
        f"{s.get('skipped', 0)} skipped | "
        f"{s.get('unexpected_passes', 0)} unexpected_pass | "
        f"{s.get('total', 0)} total"
    )
    print()

    for suite in report.get("suites", []):
        name = suite.get("suite", "?")
        ver = suite.get("version", "?")
        passed = suite.get("passed", 0)
        failed = suite.get("failed", 0)
        errored = suite.get("errored", 0)
        skipped = suite.get("skipped", 0)
        ups = suite.get("unexpected_passes", 0)
        total = suite.get("total", 0)
        dur = suite.get("duration_ms", 0)
        print(
            f"  [{name} v{ver}] "
            f"{passed}P / {failed}F / {errored}E / {skipped}S / {ups}U "
            f"of {total} ({dur:.0f} ms)"
        )

        # Print non-pass cases for visibility
        non_pass: List[Dict[str, Any]] = [
            c for c in suite.get("cases", []) if c.get("status") != "pass"
        ]
        for c in non_pass:
            cid = c.get("id", "?")
            status = c.get("status", "?")
            detail = c.get("detail", "")
            tag = ""
            if c.get("bug_ids"):
                tag = f" [{','.join(c['bug_ids'])}]"
            print(f"    {status:18} {cid}{tag}")
            if detail and status not in ("skip",):
                # Truncate long details
                d = detail if len(detail) <= 200 else detail[:197] + "..."
                print(f"      └ {d}")

    review = report.get("review_required", [])
    if review:
        print()
        print(f"REVIEW_REQUIRED ({len(review)} cases now passing — flip markers?):")
        for cid in review:
            print(f"  - {cid}")

    print()


def _exit_code(report: Dict[str, Any]) -> int:
    s = report.get("summary", {})
    if s.get("failed", 0) > 0 or s.get("errored", 0) > 0:
        return 1
    if s.get("unexpected_passes", 0) > 0:
        return 2  # bugs may be fixed; review needed
    return 0


def _provision_test_mtls_identities() -> Dict[str, str]:
    """Generate parent + subnode keypairs in temp dirs and return paths
    + cert PEM bodies. Called BEFORE PluginCore loads so the parent's
    networking.peers can be patched with the subnode entry up-front.

    Returns a dict with keys:
        parent_keys_dir, parent_cert_path, parent_key_path,
        parent_fp, parent_cert_pem,
        sub_keys_dir, sub_cert_path, sub_key_path,
        sub_fp, sub_cert_pem,
        parent_cert_pem_file (path to a file holding parent_cert_pem,
            consumed by run_node.py via --parent-cert-pem-file flag)
    """
    parent_keys_dir = tempfile.mkdtemp(prefix="aio_test_parent_")
    sub_keys_dir = tempfile.mkdtemp(prefix="aio_test_sub_")
    p_cert, p_key, p_fp, p_pem = generate_keypair(parent_keys_dir, PARENT_HOSTNAME)
    s_cert, s_key, s_fp, s_pem = generate_keypair(sub_keys_dir, SUBNODE_HOSTNAME)

    parent_cert_pem_file = tempfile.NamedTemporaryFile(
        prefix="aio_test_parent_cert_", suffix=".pem", delete=False
    )
    try:
        parent_cert_pem_file.write(p_pem.encode("utf-8"))
    finally:
        parent_cert_pem_file.close()

    return {
        "parent_keys_dir": parent_keys_dir,
        "parent_cert_path": str(p_cert),
        "parent_key_path": str(p_key),
        "parent_fp": p_fp,
        "parent_cert_pem": p_pem,
        "sub_keys_dir": sub_keys_dir,
        "sub_cert_path": str(s_cert),
        "sub_key_path": str(s_key),
        "sub_fp": s_fp,
        "sub_cert_pem": s_pem,
        "parent_cert_pem_file": parent_cert_pem_file.name,
    }


def _patch_networking_for_mtls(pc: "PluginCore", mtls: Dict[str, str]) -> None:
    """Inject mTLS keys_dir + peers into pc.yaml_config.networking BEFORE
    NetworkManager is constructed (NetworkManager reads peers + keys_dir
    once at __init__, so this must run before wait_until_ready).
    """
    nw_cfg = pc.yaml_config.setdefault("networking", {})
    nw_cfg["keys_dir"] = mtls["parent_keys_dir"]
    nw_cfg["peers"] = [
        {
            "hostname": SUBNODE_HOSTNAME,
            "address": f"127.0.0.1:{SUBNODE_PORT}",
            "cert_pem": mtls["sub_cert_pem"],
            "system_caller": False,
        },
    ]
    nw_cfg.pop("node_ips", None)


def _set_subnode_env_for_test_remote_suite(mtls: Dict[str, str]) -> None:
    """Forward the subnode's keys + parent cert to TestRemoteSuite via
    env vars so its _spawn_subnode call passes them as flags to run_node.py.
    """
    os.environ["AIO_TEST_SUB_KEYS_DIR"] = mtls["sub_keys_dir"]
    os.environ["AIO_TEST_PARENT_CERT_PEM_FILE"] = mtls["parent_cert_pem_file"]
    os.environ["AIO_TEST_PARENT_HOSTNAME"] = PARENT_HOSTNAME
    os.environ["AIO_TEST_PARENT_PORT"] = "2510"


def _cleanup_test_mtls_identities(mtls: Dict[str, str]) -> None:
    for d in (mtls.get("parent_keys_dir"), mtls.get("sub_keys_dir")):
        if d:
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    pem_file = mtls.get("parent_cert_pem_file")
    if pem_file:
        try:
            os.unlink(pem_file)
        except OSError:
            pass


async def run_tests() -> int:
    mtls = _provision_test_mtls_identities()
    _set_subnode_env_for_test_remote_suite(mtls)

    pc = PluginCore(CONFIG_PATH)
    _patch_networking_for_mtls(pc, mtls)
    await pc.wait_until_ready()

    try:
        report = await pc.execute(
            RUNNER_PLUGIN,
            "run_all",
            args={
                # Phases built so far; later phases extend this list.
                "suites": [
                    "TestExecuteSuite",
                    "TestStreamSuite",
                    "TestEventSuite",
                    "TestLifecycleSuite",
                    "TestRemoteSuite",
                    "TestPR2Suite",
                    "TestBugSuite",
                ],
                "category": None,        # both basic and edge
                # Don't filter by host — let matrix-expanded `.remote`
                # sub-cases auto-skip via remote_available=False so we keep
                # an accurate record of what's deferred.
                "host": None,
                "dump_path": DUMP_PATH,
                "skip_slow": False,
                "allow_destructive": True,
            },
        )

        if not isinstance(report, dict):
            print(
                f"ERROR: TestRunner returned non-dict: {type(report).__name__}",
                file=sys.stderr,
            )
            return 3

        _print_summary(report)
        print(f"Full report: {Path(DUMP_PATH).resolve()}")
        return _exit_code(report)
    finally:
        await pc.graceful_shutdown()
        _cleanup_test_mtls_identities(mtls)


if __name__ == "__main__":
    try:
        rc = asyncio.run(run_tests())
    except KeyboardInterrupt:
        print("Interrupted by user", file=sys.stderr)
        rc = 130
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        rc = 4
    sys.exit(rc)
