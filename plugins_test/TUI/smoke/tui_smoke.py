"""Single-node TUI smoke harness — boots PluginCore with a minimal
config so the dashboard's Settings / Networking disabled-state UI,
Plugins table (with one DISABLED row from TUIDemoSpare), Logs view,
and general navigation can be exercised end-to-end.

Networking is OFF in this single-node config — for a populated
Peers table + per-peer drill-down, use `tui_smoke_pair.py` instead,
which spawns a peer subprocess and wires mTLS automatically.

Run from the repo root:
    python plugins_test/TUI/smoke/tui_smoke.py

In the TUI:
    - Press 1-6 to switch tabs (Home / Plugins / Config / Logs /
      Networking / Settings).
    - Plugins tab → TUIDemoSpare → click Enable → row flips
      DISABLED → LOADING → READY live (bus-driven).
    - Plugins tab → Open Tab on TUIDemoOrch → Call `count_stream`
      with n=10 to see a streaming endpoint render.
    - Plugins tab → Open Tab on TUIDemoTimer → Call `pause` to
      stop / resume the background event traffic.
    - Press q to quit (Ctrl+Q for force-quit).
"""

import asyncio
import signal
import sys
from pathlib import Path

# Script lives in plugins_test/TUI/smoke/. Push the repo root onto
# sys.path so `from PluginCore import PluginCore` resolves regardless
# of CWD, and resolve the config path relative to the script.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path.insert(0, str(_REPO))

from PluginCore import PluginCore  # noqa: E402


CONFIG = str(_HERE / "tui_smoke_config.yml")


async def main():
    pc = PluginCore(CONFIG)
    await pc.wait_until_ready()
    pc._shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, pc._shutdown_event.set)
        except NotImplementedError:
            # Windows doesn't always support add_signal_handler.
            signal.signal(sig, lambda s, f: pc._shutdown_event.set())

    await pc._shutdown_event.wait()
    print("Shutting down...")
    await pc.graceful_shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user")
    finally:
        print("Successfully shutdown the smoke harness.")
