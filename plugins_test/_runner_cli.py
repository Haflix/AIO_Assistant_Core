"""C-181: shared CLI parser factory for the three Plexus subprocess
runners (``_remote_node/run_node.py``, ``_smoke_node/run_smoke.py``,
``PlexusTUI/smoke/tui_smoke_node.py``).

Each runner used to declare its own argparse setup, leading to drift in
flag names for semantically identical concepts (``--parent-hostname``
vs ``--peer-hostname`` for "the other node in the pair", etc.). This
module exports ``build_runner_parser()`` returning a configured
``argparse.ArgumentParser`` with the common flags. Each runner can
``parser = build_runner_parser(); parser.add_argument(...)`` to layer
runner-specific flags on top.

Flag canonical names:
  --config           path to the subprocess config.yml
  --port             this subprocess's listen port
  --ready-file       path to write JSON marker once wait_until_ready returns
  --keys-dir         mTLS identity dir (cert.pem / key.pem location)
  --peer-hostname    the OTHER node's hostname (sender or receiver in the pair)
  --peer-port        the OTHER node's port
  --peer-ip          the OTHER node's IP (defaults to 127.0.0.1)
  --peer-cert-pem-file  PEM file path for the OTHER node's cert (for mTLS pin)

Runners that previously used ``--parent-*`` should switch to
``--peer-*`` for the new canonical name. The shared parser accepts
``--parent-*`` as legacy aliases to keep the existing test harnesses
working through the migration.
"""
from __future__ import annotations

import argparse
from typing import Optional


def build_runner_parser(
    description: Optional[str] = None,
    *,
    require_keys_dir: bool = False,
    require_peer: bool = False,
) -> argparse.ArgumentParser:
    """Construct the shared CLI parser.

    Args:
        description: argparse program description.
        require_keys_dir: True for runners that always run mTLS-enabled
            (TUI smoke); False for legacy runners that fall back to
            unencrypted localhost loops when keys_dir is absent.
        require_peer: True for runners that always expect a peer in
            their config (smoke / TUI smoke); False for runners that
            can run standalone (_remote_node accepts a no-peer parent
            for some test shapes).
    """
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--config", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--ready-file", required=True)
    ap.add_argument("--keys-dir", required=require_keys_dir, default=None)
    # Canonical --peer-* names (C-181 standardisation).
    ap.add_argument(
        "--peer-cert-pem-file",
        required=require_peer,
        default=None,
        help="PEM file for the OTHER node's mTLS cert (for SPKI pin).",
    )
    ap.add_argument(
        "--peer-hostname",
        required=require_peer,
        default="peer",
        help="The OTHER node's hostname.",
    )
    ap.add_argument(
        "--peer-port",
        type=int,
        required=require_peer,
        default=2510,
        help="The OTHER node's port.",
    )
    ap.add_argument(
        "--peer-ip",
        default="127.0.0.1",
        help="The OTHER node's IP (default 127.0.0.1).",
    )
    # Legacy --parent-* aliases. Kept for backwards-compat with
    # _remote_node's existing test harness; new runners should use
    # --peer-*. Both write into the same dest namespace via dest=.
    # argparse.SUPPRESS: an UNPROVIDED alias contributes nothing to
    # the namespace, so the canonical --peer-* flag's default (set
    # above) survives. Without SUPPRESS, registering --parent-X
    # AFTER --peer-X overwrites the peer-X default with the alias's
    # default (last-write-wins on the same dest), so e.g.
    # `args.peer_hostname` would default to None instead of "peer".
    ap.add_argument(
        "--parent-cert-pem-file",
        dest="peer_cert_pem_file",
        default=argparse.SUPPRESS,
        help="DEPRECATED alias for --peer-cert-pem-file.",
    )
    ap.add_argument(
        "--parent-hostname",
        dest="peer_hostname",
        default=argparse.SUPPRESS,
        help="DEPRECATED alias for --peer-hostname.",
    )
    ap.add_argument(
        "--parent-port",
        dest="peer_port",
        type=int,
        default=argparse.SUPPRESS,
        help="DEPRECATED alias for --peer-port.",
    )
    ap.add_argument(
        "--parent-ip",
        dest="peer_ip",
        default=argparse.SUPPRESS,
        help="DEPRECATED alias for --peer-ip.",
    )
    return ap
