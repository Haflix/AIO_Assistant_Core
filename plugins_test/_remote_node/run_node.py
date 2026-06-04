"""Headless Plexus subprocess for Phase 5 remote tests.

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

from plexus.core import Plexus  # noqa: E402
from plugins_test._runner_cli import build_runner_parser  # noqa: E402


async def main() -> None:
    # C-181: shared CLI parser. Legacy --parent-* aliases preserved
    # so this runner's existing test harness keeps working.
    ap = build_runner_parser(
        description="TestRemoteSuite remote-node subprocess (Phase 5).",
    )
    args = ap.parse_args()

    pc = Plexus(args.config)

    # Override port BEFORE wait_until_ready: NetworkManager is constructed
    # there via ``_build_network_manager``, which reads
    # ``yaml_config["networking"]["port"]`` (NOT the ``pc.networking_port``
    # instance attribute) per Commit 2b cycle 3 HIGH-γ. The instance
    # attribute write below is kept for parity with code paths that
    # still read ``self.networking_*`` (e.g. apply_configvalues' own
    # state); the yaml write is the load-bearing one for construction.
    pc.networking_port = args.port
    nw_cfg = pc.yaml_config.setdefault("networking", {})
    nw_cfg["port"] = args.port

    if args.keys_dir is not None:
        nw_cfg["keys_dir"] = args.keys_dir
    # C-181: --peer-* is canonical; --parent-* legacy aliases write to
    # the same args.peer_* attrs via dest=.
    if args.peer_cert_pem_file is not None:
        peer_cert_pem = Path(args.peer_cert_pem_file).read_text(encoding="utf-8")
        nw_cfg["peers"] = [
            {
                "hostname": args.peer_hostname,
                "address": f"{args.peer_ip}:{args.peer_port}",
                "cert_pem": peer_cert_pem,
                "system_caller": False,
            },
        ]
        nw_cfg.pop("node_ips", None)

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
