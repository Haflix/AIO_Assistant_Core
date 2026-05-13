"""Two-TUI smoke harness — spawns two PluginCore + TUI nodes in their
own console windows, each enabling the other as an mTLS peer.

Node A (publisher): TUIDemoTimer + TUIDemoPub — fires demo events.
Node B (subscriber): TUIDemoSub + TUIDemoOrch — handles them.

Both render their own dashboards in separate windows so the same
event flow is visible from both sides (Node A as an outbound
advert, Node B as an inbound event in the Logs + per-peer drill-down
panels). Press Ctrl+C in this launcher (or close / quit either TUI
window) to terminate both processes — they are linked as a pair.

Run from the repo root:
    python plugins_test/TUI/smoke/tui_smoke_pair.py

The peer subprocesses are killed on any exit. Both nodes use
tempfile keys directories that get wiped on exit.
"""

import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO = _HERE.parents[2]
sys.path.insert(0, str(REPO))

from serialization import generate_keypair  # noqa: E402


# ── Node identities ─────────────────────────────────────────────────
NODE_A_HOSTNAME = "tui-pair-a"
NODE_B_HOSTNAME = "tui-pair-b"
NODE_A_PORT = 2510
NODE_B_PORT = 2511
NODE_A_CONFIG = str(_HERE / "tui_smoke_pair_a_config.yml")
NODE_B_CONFIG = str(_HERE / "tui_smoke_pair_b_config.yml")
NODE_RUNNER = _HERE / "tui_smoke_node.py"


def provision_keys() -> dict:
    """Generate keypairs for both nodes and write each cert PEM to a
    temp file (consumed by the OTHER node via flag for mTLS pinning)."""
    a_dir = tempfile.mkdtemp(prefix="tui_pair_a_")
    b_dir = tempfile.mkdtemp(prefix="tui_pair_b_")
    _a_cert, _a_key, a_fp, a_pem = generate_keypair(a_dir, NODE_A_HOSTNAME)
    _b_cert, _b_key, b_fp, b_pem = generate_keypair(b_dir, NODE_B_HOSTNAME)

    a_pem_file = tempfile.NamedTemporaryFile(
        prefix="tui_pair_a_cert_", suffix=".pem", delete=False
    )
    try:
        a_pem_file.write(a_pem.encode("utf-8"))
    finally:
        a_pem_file.close()

    b_pem_file = tempfile.NamedTemporaryFile(
        prefix="tui_pair_b_cert_", suffix=".pem", delete=False
    )
    try:
        b_pem_file.write(b_pem.encode("utf-8"))
    finally:
        b_pem_file.close()

    return {
        "a_dir": a_dir,
        "b_dir": b_dir,
        "a_fp": a_fp,
        "b_fp": b_fp,
        "a_pem_file": a_pem_file.name,
        "b_pem_file": b_pem_file.name,
    }


def cleanup_keys(mtls: dict) -> None:
    import shutil

    for d in (mtls.get("a_dir"), mtls.get("b_dir")):
        if d:
            shutil.rmtree(d, ignore_errors=True)
    for f in (mtls.get("a_pem_file"), mtls.get("b_pem_file")):
        if f:
            try:
                os.unlink(f)
            except OSError:
                pass


def spawn_node(
    label: str,
    config: str,
    port: int,
    peer_port: int,
    keys_dir: str,
    peer_cert_pem_file: str,
    peer_hostname: str,
    ready_file: str,
) -> subprocess.Popen:
    """Spawn one node-runner subprocess in its own console window.

    Windows: CREATE_NEW_CONSOLE gives the subprocess a fresh console
    so Textual can take over screen / input without fighting the
    launcher's terminal state. The subprocess's stdout/stderr go to
    that new console window.

    POSIX: redirect to a log file (terminal-spawning is fragile
    across distros — gnome-terminal vs xterm vs iTerm). Operator can
    `tail -f` the log for debugging.
    """
    cmd = [
        sys.executable,
        str(NODE_RUNNER),
        "--config", config,
        "--port", str(port),
        "--ready-file", ready_file,
        "--keys-dir", keys_dir,
        "--peer-cert-pem-file", peer_cert_pem_file,
        "--peer-hostname", peer_hostname,
        "--peer-port", str(peer_port),
        "--peer-ip", "127.0.0.1",
    ]
    if platform.system() == "Windows":
        return subprocess.Popen(
            cmd,
            cwd=str(REPO),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    log_path = _HERE / f"_pair_{label}.log"
    print(f"[pair] {label} log: {log_path}")
    log_file = open(log_path, "w", buffering=1, encoding="utf-8")
    return subprocess.Popen(
        cmd,
        cwd=str(REPO),
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def wait_for_ready(label: str, ready_file: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    path = Path(ready_file)
    while time.time() < deadline:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        time.sleep(0.1)
    raise TimeoutError(
        f"[pair] {label} didn't become ready in {timeout}s "
        f"(ready-file {ready_file} never appeared / was unreadable)"
    )


def terminate_node(label: str, proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    print(f"[pair] Terminating {label}...")
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
    except Exception:
        pass


def main() -> int:
    mtls = provision_keys()
    a_ready = tempfile.NamedTemporaryFile(
        prefix="tui_pair_a_ready_", suffix=".json", delete=False
    ).name
    b_ready = tempfile.NamedTemporaryFile(
        prefix="tui_pair_b_ready_", suffix=".json", delete=False
    ).name
    Path(a_ready).unlink(missing_ok=True)
    Path(b_ready).unlink(missing_ok=True)

    a_proc: subprocess.Popen | None = None
    b_proc: subprocess.Popen | None = None
    try:
        print(f"[pair] Node A (publisher) fingerprint:  {mtls['a_fp']}")
        print(f"[pair] Node B (subscriber) fingerprint: {mtls['b_fp']}")
        print(f"[pair] Spawning Node A on port {NODE_A_PORT}...")
        a_proc = spawn_node(
            "node_a",
            NODE_A_CONFIG,
            NODE_A_PORT,
            NODE_B_PORT,
            mtls["a_dir"],
            mtls["b_pem_file"],
            NODE_B_HOSTNAME,
            a_ready,
        )
        print(f"[pair] Spawning Node B on port {NODE_B_PORT}...")
        b_proc = spawn_node(
            "node_b",
            NODE_B_CONFIG,
            NODE_B_PORT,
            NODE_A_PORT,
            mtls["b_dir"],
            mtls["a_pem_file"],
            NODE_A_HOSTNAME,
            b_ready,
        )

        print("[pair] Waiting for both nodes to become ready...")
        try:
            a_info = wait_for_ready("Node A", a_ready, timeout=30.0)
            b_info = wait_for_ready("Node B", b_ready, timeout=30.0)
        except TimeoutError as e:
            print(str(e), file=sys.stderr)
            return 1

        print(f"[pair] Node A ready: {a_info}")
        print(f"[pair] Node B ready: {b_info}")
        print("[pair] Both TUIs are running in separate console windows.")
        print("[pair] Press Ctrl+C here, OR press 'q' in either TUI, "
              "to terminate both nodes.")

        # Linked-shutdown loop: exit as soon as EITHER subprocess dies,
        # OR Ctrl+C fires in this launcher. The finally block then
        # terminates whichever process is still alive.
        shutdown_requested = False

        def _sig_handler(_signum, _frame):
            nonlocal shutdown_requested
            shutdown_requested = True

        signal.signal(signal.SIGINT, _sig_handler)
        if platform.system() != "Windows":
            signal.signal(signal.SIGTERM, _sig_handler)

        while not shutdown_requested:
            a_dead = a_proc.poll() is not None
            b_dead = b_proc.poll() is not None
            if a_dead or b_dead:
                which = "Node A" if a_dead else "Node B"
                print(f"[pair] {which} exited — tearing down the pair.")
                break
            time.sleep(0.5)

        return 0

    finally:
        terminate_node("Node A", a_proc)
        terminate_node("Node B", b_proc)
        for f in (a_ready, b_ready):
            try:
                Path(f).unlink(missing_ok=True)
            except OSError:
                pass
        cleanup_keys(mtls)
        print("[pair] done.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("[pair] interrupted by user")
        sys.exit(0)
