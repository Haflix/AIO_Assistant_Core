"""Headless PluginCore subprocess for Phase 5 remote tests.

Used by TestRemoteSuite to bring up a peer node on localhost. The subprocess
loads only the TestRemote* fixtures and writes a ready-file once
wait_until_ready returns. The suite reads the ready-file to learn the peer's
hostname / port / pid.

CLI:
    python plugins_test/_remote_node/run_node.py \
        --config plugins_test/_remote_node/config.subnode.yml \
        --port 2511 \
        --ready-file <path>
"""

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PluginCore import PluginCore  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--ready-file", required=True)
    args = ap.parse_args()

    pc = PluginCore(args.config)

    # Override port BEFORE wait_until_ready: NetworkManager is constructed
    # there and reads pc.networking_port (the INSTANCE attribute), not yaml.
    pc.networking_port = args.port
    pc.yaml_config.setdefault("networking", {})["port"] = args.port

    await pc.wait_until_ready()

    Path(args.ready_file).write_text(
        json.dumps(
            {
                "hostname": pc.hostname,
                "port": args.port,
                "ip": "127.0.0.1",
                "pid": os.getpid(),
            }
        )
    )

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:
            # Windows fallback (Selector loop doesn't support add_signal_handler)
            signal.signal(sig, lambda s, f: shutdown.set())
    await shutdown.wait()
    await pc.graceful_shutdown()


if __name__ == "__main__":
    asyncio.run(main())
