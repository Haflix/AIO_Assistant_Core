"""PR3 Stage C 2-node smoke test runner.

Spawns a subnode subprocess loading TestPR3SmokeSub, then runs a parent
PluginCore loading TestPR3SmokePub. Verifies that publish_event +
request_event both work cross-node.

PR4 Stage K (B-066): rebuilt around mTLS + SPKI fingerprint pinning.
The smoke runner now:
  1. Generates a parent keypair and a subnode keypair in tmp dirs.
  2. Spawns the subnode subprocess with --keys-dir + --parent-cert-pem
     so the subnode pins the parent.
  3. Builds the parent peers config in-memory pointing at the subnode's
     ip:port + cert_pem, and starts the parent.

No shared secret. No NETWORKING_SECRET env var.

Usage (from repo root):
    python plugins_test/_smoke_node/run_smoke.py

Exit codes:
    0 — smoke passed (sub received >= 1 event AND request_event returned)
    1 — smoke failed (cross-node delivery broken)
    2 — infra failure (subnode didn't start, etc.)
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PluginCore import PluginCore  # noqa: E402
from serialization import generate_keypair  # noqa: E402


def _spawn_subnode(
    ready_file: str,
    port: int,
    sub_keys_dir: str,
    parent_cert_pem_file: str,
    parent_hostname: str,
    parent_port: int,
) -> subprocess.Popen:
    env = os.environ.copy()
    cfg = REPO_ROOT / "plugins_test" / "_smoke_node" / "config.smoke_sub.yml"
    runner = REPO_ROOT / "plugins_test" / "_remote_node" / "run_node.py"
    return subprocess.Popen(
        [
            sys.executable,
            str(runner),
            "--config",
            str(cfg),
            "--port",
            str(port),
            "--ready-file",
            ready_file,
            "--keys-dir",
            sub_keys_dir,
            "--parent-cert-pem-file",
            parent_cert_pem_file,
            "--parent-hostname",
            parent_hostname,
            "--parent-port",
            str(parent_port),
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _wait_for_ready(ready_file: str, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(ready_file).exists():
            try:
                return json.loads(Path(ready_file).read_text())
            except Exception:
                pass
        time.sleep(0.2)
    return None


async def _run_parent(
    subnode_port: int,
    parent_keys_dir: str,
    sub_cert_pem: str,
    sub_hostname: str,
):
    cfg = REPO_ROOT / "plugins_test" / "_smoke_node" / "config.smoke_pub.yml"
    pc = PluginCore(str(cfg))
    nw_cfg = pc.yaml_config.setdefault("networking", {})
    nw_cfg["keys_dir"] = parent_keys_dir
    nw_cfg["peers"] = [
        {
            "hostname": sub_hostname,
            "address": f"127.0.0.1:{subnode_port}",
            "cert_pem": sub_cert_pem,
            "system_caller": False,
        },
    ]
    nw_cfg.pop("node_ips", None)
    pc.networking_port = 2510

    await pc.wait_until_ready()

    # Pub plugin's on_enable already published 3 events + 1 request.
    # Give it ~6s total to complete (2s sleep + 3*0.3s + request roundtrip).
    await asyncio.sleep(7.0)

    try:
        status = await pc.execute("TestPR3SmokePub", "get_status")
    except Exception as e:
        await pc.graceful_shutdown()
        return False, f"could not query Pub status: {e}"

    pubs = status.get("publish_count", 0)
    req_result = status.get("request_result")
    msg = (
        f"Pub fired {pubs} publish_event call(s); "
        f"request_event returned {req_result!r}"
    )

    await pc.graceful_shutdown()

    if pubs >= 3 and req_result and req_result.get("pong") == "ping":
        return True, msg
    return False, msg


def main() -> int:
    subnode_port = 2511
    parent_port = 2510
    parent_hostname = "smoke_parent"
    sub_hostname = "smoke_sub"

    parent_keys_dir = tempfile.mkdtemp(prefix="aio_smoke_parent_")
    sub_keys_dir = tempfile.mkdtemp(prefix="aio_smoke_sub_")
    parent_cert_pem_file = tempfile.NamedTemporaryFile(
        prefix="aio_smoke_parent_cert_", suffix=".pem", delete=False
    )
    parent_cert_pem_file.close()

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as ready_tmp:
        ready_file = ready_tmp.name

    proc = None
    try:
        # Pre-generate both keypairs so we can cross-pin before spawning.
        _, _, parent_fp, parent_cert_pem = generate_keypair(parent_keys_dir, parent_hostname)
        _, _, sub_fp, sub_cert_pem = generate_keypair(sub_keys_dir, sub_hostname)
        print(f"[SMOKE] parent fingerprint: {parent_fp}")
        print(f"[SMOKE] sub fingerprint:    {sub_fp}")

        # Write parent cert PEM to a file the subnode subprocess can read.
        Path(parent_cert_pem_file.name).write_text(parent_cert_pem, encoding="utf-8")

        print(f"[SMOKE] spawning subnode on port {subnode_port}...")
        proc = _spawn_subnode(
            ready_file, subnode_port, sub_keys_dir,
            parent_cert_pem_file.name, parent_hostname, parent_port,
        )
        info = _wait_for_ready(ready_file, timeout=15.0)
        if info is None:
            print("[SMOKE] subnode failed to become ready in 15s")
            try:
                stdout, _ = proc.communicate(timeout=2.0)
                if stdout:
                    print("[SMOKE] subnode stdout:")
                    print(stdout.decode(errors="replace"))
            except subprocess.TimeoutExpired:
                pass
            return 2
        print(f"[SMOKE] subnode ready: {info}")

        ok, msg = asyncio.run(_run_parent(
            subnode_port, parent_keys_dir, sub_cert_pem, sub_hostname,
        ))
        print(f"[SMOKE] {msg}")
        if ok:
            print("[SMOKE] PASS — cross-node publish_event + request_event verified")
            return 0
        print("[SMOKE] FAIL — events did not flow cross-node")
        return 1
    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        for path in (ready_file, parent_cert_pem_file.name):
            try:
                os.unlink(path)
            except OSError:
                pass
        for d in (parent_keys_dir, sub_keys_dir):
            try:
                import shutil
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
