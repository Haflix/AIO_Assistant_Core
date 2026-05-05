"""PR3 Stage C 2-node smoke test runner.

Spawns a subnode subprocess loading TestPR3SmokeSub on port 2511, then
runs a parent PluginCore loading TestPR3SmokePub on port 2510. Verifies
that publish_event + request_event both work cross-node.

Usage (from worktree root):
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


def _spawn_subnode(ready_file: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["NETWORKING_SECRET"] = "aio_smoke_secret"
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
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _wait_for_ready(ready_file: str, timeout: float) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(ready_file).exists():
            try:
                return json.loads(Path(ready_file).read_text())
            except Exception:
                pass
        time.sleep(0.2)
    return None


async def _run_parent(subnode_port: int) -> tuple[bool, str]:
    cfg = REPO_ROOT / "plugins_test" / "_smoke_node" / "config.smoke_pub.yml"
    pc = PluginCore(str(cfg))
    pc.yaml_config.setdefault("networking", {})["node_ips"] = [
        f"127.0.0.1:{subnode_port}"
    ]
    pc.networking_node_ips = [f"127.0.0.1:{subnode_port}"]
    pc.networking_port = 2510

    await pc.wait_until_ready()

    # Pub plugin's on_enable already published 3 events + 1 request.
    # Give it ~6s total to complete (2s sleep + 3*0.3s + request roundtrip).
    await asyncio.sleep(7.0)

    # Pull pub status to inspect counters
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
    with tempfile.NamedTemporaryFile(
        suffix=".json", delete=False
    ) as ready_tmp:
        ready_file = ready_tmp.name

    proc = None
    try:
        print(f"[SMOKE] spawning subnode on port {subnode_port}...")
        proc = _spawn_subnode(ready_file, subnode_port)
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

        ok, msg = asyncio.run(_run_parent(subnode_port))
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
        try:
            os.unlink(ready_file)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
