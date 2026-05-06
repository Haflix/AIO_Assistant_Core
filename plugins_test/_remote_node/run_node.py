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
    # PR4 Stage K (B-066): subprocess-driven mTLS bootstrap. The parent
    # passes its keys_dir + cert PEM file path; the subprocess loads its
    # OWN cert from keys_dir and pins the parent via the cert PEM file.
    ap.add_argument("--keys-dir", default=None)
    ap.add_argument("--parent-cert-pem-file", default=None)
    ap.add_argument("--parent-hostname", default="parent")
    ap.add_argument("--parent-port", type=int, default=2510)
    ap.add_argument("--parent-ip", default="127.0.0.1")
    args = ap.parse_args()

    pc = PluginCore(args.config)

    # Override port BEFORE wait_until_ready: NetworkManager is constructed
    # there and reads pc.networking_port (the INSTANCE attribute), not yaml.
    pc.networking_port = args.port
    nw_cfg = pc.yaml_config.setdefault("networking", {})
    nw_cfg["port"] = args.port

    if args.keys_dir is not None:
        nw_cfg["keys_dir"] = args.keys_dir
    if args.parent_cert_pem_file is not None:
        parent_cert_pem = Path(args.parent_cert_pem_file).read_text(encoding="utf-8")
        nw_cfg["peers"] = [
            {
                "hostname": args.parent_hostname,
                "address": f"{args.parent_ip}:{args.parent_port}",
                "cert_pem": parent_cert_pem,
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
