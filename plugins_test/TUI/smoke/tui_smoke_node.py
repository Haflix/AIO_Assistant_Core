"""TUI-enabled Plexus subprocess for the two-TUI smoke harness.

Boots a Plexus + dashboard TUI in its own console window. Spawned
by `tui_smoke_pair.py` so each side of the pair has a live, interactive
dashboard (rather than one TUI + one headless peer).

CLI flags mirror `plugins_test/_remote_node/run_node.py` so the
launcher can keep the same provisioning shape (keys directory, peer
cert PEM file, peer host / port). All networking knobs are injected
at runtime; the YAML's networking section is overwritten in memory
before `wait_until_ready` constructs the NetworkManager.

Not intended to run directly — `tui_smoke_pair.py` is the entry point.
"""

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from plexus.core import Plexus  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--ready-file", required=True)
    ap.add_argument("--keys-dir", required=True)
    ap.add_argument("--peer-cert-pem-file", required=True)
    ap.add_argument("--peer-hostname", required=True)
    ap.add_argument("--peer-port", type=int, required=True)
    ap.add_argument("--peer-ip", default="127.0.0.1")
    args = ap.parse_args()

    pc = Plexus(args.config)

    # `_shutdown_event` must exist BEFORE wait_until_ready so the TUI
    # plugin's _run_tui_thread can pick it up via hasattr at TUI thread
    # start. Original tui_smoke_pair.py noted the same race.
    pc._shutdown_event = asyncio.Event()

    # Patch networking config BEFORE NetworkManager construction.
    # _build_network_manager reads peers + keys_dir + port from
    # yaml_config["networking"]; the pc.networking_* instance attrs
    # are mirrored for downstream readers (apply_configvalues path).
    nw = pc.yaml_config.setdefault("networking", {})
    nw["enabled"] = True
    nw["port"] = args.port
    nw["keys_dir"] = args.keys_dir
    peer_pem = Path(args.peer_cert_pem_file).read_text(encoding="utf-8")
    nw["peers"] = [
        {
            "hostname": args.peer_hostname,
            "address": f"{args.peer_ip}:{args.peer_port}",
            "cert_pem": peer_pem,
            "system_caller": False,
        },
    ]
    nw["discover_nodes"] = True
    nw["direct_discoverable"] = True
    nw["auto_discoverable"] = True
    nw.pop("node_ips", None)
    pc.networking_enabled = True
    pc.networking_port = args.port
    pc.networking_auto_discoverable = True
    pc.networking_direct_discoverable = True

    await pc.wait_until_ready()

    Path(args.ready_file).write_text(
        json.dumps(
            {
                "hostname": pc.hostname,
                "port": args.port,
                "ip": args.peer_ip,
                "pid": os.getpid(),
            }
        )
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, pc._shutdown_event.set)
        except NotImplementedError:
            # Windows fallback — Selector loop doesn't always support
            # add_signal_handler.
            signal.signal(sig, lambda s, f: pc._shutdown_event.set())

    await pc._shutdown_event.wait()
    await pc.graceful_shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
