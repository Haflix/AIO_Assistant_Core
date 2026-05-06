"""CLI helper for B-066 networking.

Currently exposes one subcommand:

    python -m networking_cli show-fingerprint --config path/to/config.yml

Reads the cert.pem under keys_dir (default: _keys relative to config.yml's
directory) and prints its SPKI SHA-256 fingerprint in `sha256:<hex>` form.

Used during peer onboarding: the operator runs this on each node to obtain
the fingerprint to paste into other nodes' peers[].fingerprint config (or
generated automatically from peers[].cert_file / peers[].cert_pem at
startup, but printing it explicitly is useful for verification).
"""

import argparse
import hashlib
import sys
from pathlib import Path

import yaml
from cryptography.x509 import load_pem_x509_certificate
from cryptography.hazmat.primitives import serialization


def _show_fingerprint(config_path: Path) -> int:
    try:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        print(f"Could not read config at {config_path}: {e}", file=sys.stderr)
        return 1
    nw_cfg = (cfg or {}).get("networking", {})
    keys_dir_raw = nw_cfg.get("keys_dir", "_keys")
    keys_dir = Path(keys_dir_raw)
    if not keys_dir.is_absolute():
        keys_dir = (config_path.parent / keys_dir).resolve()
    cert_path = keys_dir / "cert.pem"
    if not cert_path.exists():
        print(
            f"No cert found at {cert_path}. Start the node once to generate, "
            f"or check the keys_dir field in your config.",
            file=sys.stderr,
        )
        return 1
    cert = load_pem_x509_certificate(cert_path.read_bytes())
    spki = cert.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    print(f"sha256:{hashlib.sha256(spki).hexdigest()}")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="networking_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    fp = sub.add_parser("show-fingerprint", help="Print this node's SPKI fingerprint")
    fp.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "show-fingerprint":
        sys.exit(_show_fingerprint(args.config))


if __name__ == "__main__":
    main()
