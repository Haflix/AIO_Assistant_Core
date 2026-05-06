from logging import Logger
import ssl
import socket
import ipaddress
import asyncio
import contextlib
import datetime
import hashlib
import inspect
import pickle
import struct
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Set, Union, Optional, Tuple
from decorators import async_log_errors, async_handle_errors, async_gen_handle_errors, async_gen_log_errors
from exceptions import (
    NetworkRequestException,
    NodeException,
    NoLocalSubException,
    RequestException,
)
from networking_classes import Node
from networking_classes import RemotePlugin
from serialization import safe_loads, FINGERPRINT_CLI_CMD, generate_keypair


# PR3 Stage C — in-memory advertised-subscription record (per-peer wire
# projection). Plain dataclass so it pickles cleanly and only carries the
# four wire-eligible filter fields plus identity. Receiver-only fields
# (target_plugin, target_access_name, target_plugin_uuid, declared_id,
# enabled, plugin_name, plugin_uuid) stay private to the owning node.
@dataclass
class AdvertSub:
    sub_uuid: str
    topic_pattern: str
    hosts: Union[str, list, None]
    blocked_hosts: Union[str, list, None]
    authors: Union[str, list, None]
    blocked_authors: Union[str, list, None]


# PR4 Stage K (B-066) — peer config entry. cert_pem is required (resolved
# from cert_file at config-load time if needed). fingerprint is derived from
# cert_pem at parse time and used as the post-handshake identity gate.
@dataclass(frozen=True)
class PeerSpec:
    hostname: str
    ip: str
    port: int
    cert_pem: str
    fingerprint: str
    system_caller: bool = False


# Message type constants
MSG_EXECUTE = 1
MSG_EXECUTE_STREAM = 2
MSG_HAS_ENDPOINT = 3
MSG_PING = 4
MSG_INFO = 5
MSG_FIND_TAGGED_ENDPOINTS = 6

MSG_RESULT = 10
MSG_STREAM_CHUNK = 11
MSG_ERROR = 12
MSG_END_STREAM = 13

# MSG types 7-9 reserved (removed in PR3 Stage D —
# ex-MSG_NOTIFY / MSG_TOPIC_REQUEST / MSG_TOPIC_REQUEST_STREAM).
# Do not reuse these numbers for new MSG types.

MSG_STREAM_ITEM_END = 14  # Marks end of one item in a streaming response

# PR3 Stage C — event protocol message types (locked #1, locked #13)
MSG_PUBLISH_EVENT = 15
MSG_REQUEST_EVENT = 16
MSG_REQUEST_EVENT_STREAM = 17
MSG_SUB_ADVERTISE = 18
MSG_SUB_DELTA = 19

# MSG_AUTH = 20 was removed in PR4 Stage K K-3 (B-066 fix). The message-type
# number is reserved and must not be reused for new message types.

CHUNK_SIZE = 64 * 1024  # 64KB chunks for streaming
MAX_MESSAGE_SIZE = 100 * 1024 * 1024  # 100MB max message size
MAX_ADVERT_SUBS_PER_PEER = 100_000  # Cap MSG_SUB_ADVERTISE entries to bound _adverts_struct_lock hold time

# Sentinel returned by request_event_remote when server sends no result data.
# Distinguishes "handler returned None" (valid) from "no response received."
REMOTE_NO_RESULT = object()


class NetworkManager:
    def __init__(
        self,
        plugin_core,
        logger: Logger,
        node_ips: list,
        discover_nodes: bool,
        direct_discoverable: bool,
        auto_discoverable: bool,
        port=2510,
        secret: Optional[str] = None,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        pool_size: int = 5,
        networking_config: Optional[dict] = None,
        config_dir: Optional[Path] = None,
    ):
        self.plugin_core = plugin_core
        self._logger = logger

        # PR4 Stage K (B-066) — hard error on legacy node_ips schema.
        # Operators must migrate to the peers: schema. Fires BEFORE any other
        # init so a misconfigured node fails fast with an actionable message.
        nw_cfg = networking_config or {}
        if "node_ips" in nw_cfg:
            raise RuntimeError(
                "node_ips schema removed in PR4 Stage K (B-066 fix). Migrate to:\n"
                "  networking:\n"
                "    peers:\n"
                "      - hostname: <peer-name>\n"
                "        address: <ip[:port]>\n"
                "        cert_file: _keys/peers/<peer-name>.pem  # OR cert_pem: |\n"
                "        system_caller: false\n"
                f"After migrating, run '{FINGERPRINT_CLI_CMD} --config <path>' on each "
                "node to print its fingerprint, then paste each node's cert PEM "
                "(or save it under _keys/peers/<name>.pem and reference via "
                "cert_file) into the other nodes' peers[] entries."
            )

        # Internal node_ips is normalized to list[tuple[str, Optional[int]]].
        # Each entry is (ip, port_or_None); port=None means "use the cluster
        # default port self.port". K-3 removes this field — kept here for K-2
        # to preserve test invariant during the transition.
        self.node_ips: list[tuple[str, Optional[int]]] = [
            self._parse_endpoint(e) for e in (node_ips or [])
        ]
        # De-duplicate on (ip, port).
        self.node_ips = list(dict.fromkeys(self.node_ips))

        self.discover_nodes = discover_nodes
        self.direct_discoverable = direct_discoverable
        self.auto_discoverable = auto_discoverable

        self.port = port
        self.nodes: list[Node] = []

        # PR4 Stage K identity + peer config (replaces self.secret / cert_file /
        # key_file in K-3 — kept side-by-side here for atomic test invariant).
        self.hostname: str = nw_cfg.get("hostname", socket.gethostname())
        self.keys_dir: Path = Path(nw_cfg.get("keys_dir", "_keys"))
        if not self.keys_dir.is_absolute():
            base = config_dir if config_dir is not None else Path.cwd()
            self.keys_dir = (Path(base) / self.keys_dir).resolve()
        self.cert_path: Path = self.keys_dir / "cert.pem"
        self.key_path: Path = self.keys_dir / "key.pem"
        self.peers: List[PeerSpec] = self._parse_peers(nw_cfg.get("peers", []))
        self.peers_by_fingerprint: Dict[str, PeerSpec] = {
            p.fingerprint: p for p in self.peers
        }
        self.peers_by_endpoint: Dict[Tuple[str, int], PeerSpec] = {
            (p.ip, p.port): p for p in self.peers
        }
        self.own_fingerprint: str = ""  # populated by _load_or_generate_identity

        # PR4 Stage K (B-066): seed self.node_ips from peers so the existing
        # discovery flow (update_all_nodes / heartbeat) can find peers via
        # the new schema without rewriting discovery itself.
        for spec in self.peers:
            entry = (spec.ip, spec.port)
            if entry not in self.node_ips:
                self.node_ips.append(entry)

        # PR4 Stage K: legacy secret / cert_file / key_file kwargs accepted
        # for PluginCore call-site compatibility but no longer used. The
        # K-3 startup gate uses self.peers, not self.secret.
        self.secret = secret or os.getenv("NETWORKING_SECRET", "")
        if isinstance(self.secret, str):
            self.secret = self.secret.encode()
        self.cert_file = cert_file
        self.key_file = key_file
        self.pool_size = pool_size

        # Connection pools: keyed by (IP, port) tuple so that two peers on
        # the same IP but different ports (e.g. parent + subnode on the same
        # machine) get separate pools. _pool_key(IP) resolves the port.
        self.connection_pools: dict[tuple[str, int], asyncio.Queue] = {}

        # Server state
        self.server = None
        self.server_task = None
        self.heartbeat_task = None
        self.discovery_task = None

        # SSL context (will be initialized in start)
        self.ssl_context = None
        self._temp_ssl_files = []  # Track temp cert/key files for cleanup

        # Loop intervals and timeouts (defaults per plan)
        self.heartbeat_interval: float = 10.0
        self.lookup_interval: float = 60.0
        self.liveness_timeout: float = 30.0

        # ── PR3 Stage C advert-protocol state ─────────────────────────
        # Per-peer table of subs the peer told us about. Keyed by peer
        # hostname (locked #8) — stable across reconnects/IP changes.
        self._inbound_adverts: Dict[str, Dict[str, AdvertSub]] = {}

        # Global insertion-order structure for C11 tie-break — first peer
        # to advertise a matching sub wins on no-local-match. Key is
        # (peer_hostname, sub_uuid); value is AdvertSub.
        self._inbound_global_order: Dict[Tuple[str, str], AdvertSub] = {}

        # Per-peer record of subs we've already told that peer about. Used
        # to compute deltas at register/unregister time and to avoid
        # double-sending a sub we filtered out. Inner key is OUR local
        # sub_uuid.
        self._outbound_adverts: Dict[str, Dict[str, AdvertSub]] = {}

        # Per-peer advert lock (locked #9). Acquired around the FULL
        # outbound advert lifecycle (build content + send) so snapshot vs
        # delta can never race into the wire pool.
        self._advert_locks: Dict[str, asyncio.Lock] = {}

        # Single global mutation lock for atomic mutation of the three
        # tables together. ALWAYS acquired AFTER a per-peer lock when both
        # are needed; never held across wire send / _get_connection await.
        self._adverts_struct_lock: asyncio.Lock = asyncio.Lock()

        # Per-peer in-flight publish tasks (locked #4). Mutated under
        # _adverts_struct_lock per locked #16. Tasks self-deregister on
        # completion; disconnect hook iterates and cancels.
        self._inflight_publishes: Dict[str, set] = {}

        # One-shot trigger guard for initial-snapshot send. Set after
        # first successful _initial_advert_exchange for a peer; cleared
        # in _drop_peer_advert_state so reconnects re-arm.
        self._snapshot_sent: set = set()

        # Per-peer in-flight initial-exchange task (cancellable on
        # disconnect via _drop_peer_advert_state).
        self._initial_exchange_tasks: Dict[str, asyncio.Task] = {}

        # Networking-ready flag. True after start() finishes wiring
        # background tasks; False at top of stop(). Used by add/remove
        # broadcast hooks to no-op until peers can be reached.
        self.is_ready: bool = False

    # ── Per-node port helpers ──────────────────────────────────────────

    def _parse_endpoint(self, entry) -> tuple[str, Optional[int]]:
        """Normalise a node_ips entry into (ip, port_or_None).

        Accepts:
          - "10.0.0.1"                       → ("10.0.0.1", None)
          - "10.0.0.1:2511"                  → ("10.0.0.1", 2511)
          - {"ip": "10.0.0.1"}               → ("10.0.0.1", None)
          - {"ip": "10.0.0.1", "port": 2511} → ("10.0.0.1", 2511)
          - ("10.0.0.1", 2511)               → ("10.0.0.1", 2511)
          - ("10.0.0.1", 2511, "host")       → ("10.0.0.1", 2511) [3-tuple from _to_tuple]
          - already-parsed tuple             → returned as-is
        """
        if isinstance(entry, tuple):
            if len(entry) >= 2:
                ip, port = entry[0], entry[1]
                return (
                    str(ip),
                    int(port) if port is not None else None,
                )
            if len(entry) == 1:
                return (str(entry[0]), None)
        if isinstance(entry, dict):
            ip = entry.get("ip") or entry.get("IP")
            port = entry.get("port")
            return (
                str(ip),
                int(port) if port is not None else None,
            )
        if isinstance(entry, str):
            # IPv4-style "host:port". Bracketed IPv6 ([::1]:2511) is not
            # supported yet — log a warning so the silent drop is visible.
            colon_count = entry.count(":")
            if colon_count == 1:
                ip, port_s = entry.rsplit(":", 1)
                try:
                    return (ip, int(port_s))
                except ValueError:
                    return (entry, None)
            if colon_count > 1:
                # Likely IPv6. We don't parse [::1]:2511 yet — log and treat
                # the whole string as the IP (port falls back to default).
                self._logger.warning(
                    f"[CONFIG] node_ips entry {entry!r} looks like IPv6 with "
                    f"port; bracketed-IPv6 form not supported, port ignored"
                )
            return (entry, None)
        # Unknown type — coerce to string and treat as IP-only.
        return (str(entry), None)

    def _resolve_port(self, IP: str) -> int:
        """Return the port to use when connecting to a peer at this IP.

        PR4 Stage K: walks self.peers_by_endpoint first (authoritative for
        the new mTLS-pinned regime). Falls back to the legacy self.nodes /
        self.node_ips lookup so existing tests that still use node_ips
        continue to work during the K-2 → K-3 transition window.
        """
        for peer_ip, peer_port in self.peers_by_endpoint.keys():
            if peer_ip == IP:
                return peer_port
        for node in self.nodes:
            if node.IP == IP:
                return node.port if node.port is not None else self.port
        for ip, port in self.node_ips:
            if ip == IP and port is not None:
                return port
        return self.port

    # ── PR4 Stage K (B-066) — B-018b split helper ────────────────────

    async def _apply_b018b_guard(
        self,
        author: str,
        author_id: str,
        conn_context: Dict[str, Any],
        writer: asyncio.StreamWriter,
        log_prefix: str,
    ) -> Tuple[str, str, bool]:
        """Apply the split B-018b guard. Returns (author, author_id, denied).

        Part 1: author == "system" gated by conn_context["system_caller"].
        Permitted privileged peers keep author="system"; non-privileged
        peers receive a MSG_ERROR and the caller MUST early-return.

        Part 2: author_id impersonation rewrite — UNCONDITIONAL with
        respect to author. If Part 1 permitted author="system", Part 2
        rewrites only author_id. Otherwise rewrites both.
        """
        _peer_addr = writer.get_extra_info("peername")
        _peer_repr = (
            f"{_peer_addr[0]}:{_peer_addr[1]}" if _peer_addr else "unknown"
        )

        # Part 1
        if author == "system":
            if conn_context.get("system_caller"):
                self._logger.info(
                    "%s B-018b: author='system' permitted from privileged peer fp=%s",
                    log_prefix, conn_context.get("peer_fingerprint"),
                )
            else:
                self._logger.warning(
                    "%s B-018b: rejected author='system' from non-privileged peer hostname=%s",
                    log_prefix, conn_context.get("peer_hostname"),
                )
                try:
                    await self._send_error_pickled(writer, NetworkRequestException(
                        "author='system' not permitted from this peer; "
                        "system_caller=false in your peer config. "
                        "Set system_caller=true on this peer's entry to permit."
                    ))
                except Exception as _send_err:
                    # K-5 review MED fix: log instead of silently swallowing.
                    # If the writer is broken at this moment, the caller may
                    # observe a hang via its own _receive_message timeout —
                    # a logged WARNING gives operators a trail.
                    self._logger.warning(
                        "%s B-018b denial: failed to send privilege-denial "
                        "MSG_ERROR to peer (hostname=%s): %s",
                        log_prefix, conn_context.get("peer_hostname"), _send_err,
                    )
                return author, author_id, True

        # Part 2 — author_id impersonation rewrite, runs in all paths that
        # didn't return above. Preserves author="system" if Part 1 permitted.
        if (
            author_id in self.plugin_core.plugins_by_uuid
            or author_id == self.plugin_core.hostname
        ):
            if author == "system":
                self._logger.warning(
                    "%s B-018b: privileged peer=%s attempted author_id spoof "
                    "(author_id=%r matched local entity); rewriting author_id only",
                    log_prefix, _peer_repr, author_id,
                )
                author_id = f"remote-peer:{_peer_repr}"
            else:
                self._logger.warning(
                    "%s B-018b: rejected wire-supplied author_id=%r from peer=%s; "
                    "rewriting to remote sentinel",
                    log_prefix, author_id, _peer_repr,
                )
                author = f"remote-peer:{_peer_repr}"
                author_id = f"remote-peer:{_peer_repr}"

        return author, author_id, False

    # ── PR4 Stage K (B-066) — peer parsing + identity helpers ─────────

    def _parse_peers(self, raw_peers) -> List[PeerSpec]:
        """Parse the peers config list, resolve cert_file → cert_pem,
        derive SPKI fingerprint, validate uniqueness.

        Accepts None (bare YAML key with no value) and treats as empty.
        """
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization as _ser

        if raw_peers is None:
            raw_peers = []

        peers: List[PeerSpec] = []
        seen_fps: Set[str] = set()
        seen_endpoints: Set[Tuple[str, int]] = set()

        for entry in raw_peers:
            hostname = entry.get("hostname")
            address = entry.get("address")
            if not hostname or not address:
                raise RuntimeError(
                    f"Peer entry missing hostname or address: {entry}"
                )

            cert_file = entry.get("cert_file")
            cert_pem_inline = entry.get("cert_pem")
            if cert_file and cert_pem_inline:
                raise RuntimeError(
                    f"Peer {hostname} has both cert_file and cert_pem set. "
                    "Pick one (cert_file is preferred for cleaner config)."
                )
            if not cert_file and not cert_pem_inline:
                raise RuntimeError(
                    f"Peer {hostname} missing cert_file or cert_pem. "
                    "Either provide a path-relative cert_file or paste the "
                    "PEM body inline as cert_pem (multi-line YAML block)."
                )

            if cert_file:
                cf_path = Path(cert_file)
                if not cf_path.is_absolute():
                    cf_path = (self.keys_dir.parent / cf_path).resolve()
                try:
                    cert_pem = cf_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as e:
                    raise RuntimeError(
                        f"Peer {hostname} cert_file {cf_path} could not be read: {e}. "
                        "Check the path exists, is a file (not a directory), is "
                        "readable, and contains UTF-8 PEM text (not DER binary)."
                    )
            else:
                cert_pem = cert_pem_inline

            cert_pem = cert_pem.strip()
            if not cert_pem.startswith("-----BEGIN CERTIFICATE-----"):
                raise RuntimeError(
                    f"Peer {hostname} cert_pem missing PEM header after strip. "
                    "Check YAML indentation or file content."
                )

            try:
                cert = x509.load_pem_x509_certificate(cert_pem.encode())
            except (ValueError, TypeError) as e:
                raise RuntimeError(
                    f"Peer {hostname} cert_pem is not valid PEM-encoded X.509: {e}"
                )
            spki = cert.public_key().public_bytes(
                encoding=_ser.Encoding.DER,
                format=_ser.PublicFormat.SubjectPublicKeyInfo,
            )
            derived_fp = f"sha256:{hashlib.sha256(spki).hexdigest()}"

            declared_fp = entry.get("fingerprint")
            if declared_fp and declared_fp != derived_fp:
                raise RuntimeError(
                    f"Peer {hostname} fingerprint mismatch: config says "
                    f"{declared_fp} but cert hashes to {derived_fp}"
                )

            if derived_fp in seen_fps:
                raise RuntimeError(
                    f"Duplicate peer fingerprint across config: {derived_fp}"
                )
            seen_fps.add(derived_fp)

            # K-2 review MED fix: handle IPv6 (bracketed and bare). A bare
            # IPv6 address like "::1" has multiple colons; partition would
            # split at the first colon and yield port="1". The bracketed
            # form "[::1]:2511" is the standard host:port wire format. We
            # accept both bracketed (with explicit port) and bare (port
            # defaults to self.port).
            if address.startswith("["):
                end_bracket = address.find("]")
                if end_bracket == -1:
                    raise RuntimeError(
                        f"Peer {hostname} address {address!r}: opening bracket "
                        "without closing bracket. Use [ipv6]:port form."
                    )
                ip = address[1:end_bracket]
                rest = address[end_bracket + 1:]
                if rest.startswith(":"):
                    try:
                        port = int(rest[1:])
                    except ValueError:
                        raise RuntimeError(
                            f"Peer {hostname} address {address!r}: bracketed IPv6 "
                            "port suffix is not a valid integer."
                        )
                elif rest == "":
                    port = self.port
                else:
                    raise RuntimeError(
                        f"Peer {hostname} address {address!r}: unexpected suffix "
                        f"{rest!r} after closing bracket."
                    )
            elif address.count(":") > 1:
                # Bare IPv6 — treat the whole string as the IP, port defaults.
                ip = address
                port = self.port
                self._logger.warning(
                    "[CONFIG] Peer %s address %r is bare IPv6; using default port "
                    "%d. To specify a non-default port, use [%s]:port form.",
                    hostname, address, port, address,
                )
            else:
                ip, _, port_str = address.partition(":")
                port = int(port_str) if port_str else self.port
            endpoint = (ip, port)
            if endpoint in seen_endpoints:
                raise RuntimeError(
                    f"Duplicate peer endpoint across config: {ip}:{port}"
                )
            seen_endpoints.add(endpoint)

            peers.append(PeerSpec(
                hostname=hostname, ip=ip, port=port,
                cert_pem=cert_pem, fingerprint=derived_fp,
                system_caller=entry.get("system_caller", False),
            ))

        if not peers:
            self._logger.debug("[NETWORKING] _parse_peers returned empty list")

        return peers

    def _load_or_generate_identity(self):
        """Load existing cert.pem / key.pem from keys_dir, or generate a
        fresh self-signed pair if both are missing. Sync function — disk
        I/O only, no async operations.

        Hard-errors on inconsistent state (one file present, the other
        missing) and on stale .tmp orphans from a crashed mid-write.
        """
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization as _ser

        cert_exists = self.cert_path.exists()
        key_exists = self.key_path.exists()

        cert_tmp = self.cert_path.with_suffix(".pem.tmp")
        key_tmp = self.key_path.with_suffix(".pem.tmp")
        tmp_orphans = [str(p) for p in (cert_tmp, key_tmp) if p.exists()]
        if tmp_orphans:
            raise RuntimeError(
                f"Found stale .tmp file(s) at {self.keys_dir}: {tmp_orphans}. "
                "A previous run crashed mid-write. Delete the .tmp files to "
                "allow a clean regeneration on next start (the .pem files "
                "are intact if both are present)."
            )

        if cert_exists != key_exists:
            raise RuntimeError(
                f"Inconsistent identity state at {self.keys_dir}: "
                f"cert.pem={cert_exists}, key.pem={key_exists}. "
                "Either both or neither must exist. Delete the orphan .pem "
                "file to allow regeneration on next start."
            )

        if cert_exists and key_exists:
            try:
                cert_pem = self.cert_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise RuntimeError(
                    f"Existing cert at {self.cert_path} could not be read: {e}. "
                    "If the file is corrupted, delete BOTH cert.pem and key.pem "
                    "to regenerate (this changes the node's fingerprint — all "
                    "peers must update their peers[].cert_pem entries)."
                )
            try:
                cert = x509.load_pem_x509_certificate(cert_pem.encode())
            except (ValueError, TypeError) as e:
                raise RuntimeError(
                    f"Existing cert at {self.cert_path} is not valid PEM-encoded X.509: {e}"
                )
            spki = cert.public_key().public_bytes(
                encoding=_ser.Encoding.DER,
                format=_ser.PublicFormat.SubjectPublicKeyInfo,
            )
            self.own_fingerprint = f"sha256:{hashlib.sha256(spki).hexdigest()}"
        else:
            _, _, fp, cert_pem = generate_keypair(str(self.keys_dir), self.hostname)
            self.own_fingerprint = fp

        if os.name == "nt":
            self._logger.warning(
                "[NETWORKING] Running on Windows — key file 0o600 permission "
                "could not be enforced. Ensure key.pem is not world-readable "
                "via NTFS ACLs or move it to a per-user directory."
            )

        # K-2 review HIGH fix: also print the cert PEM for paste-into-peer-config
        # bootstrapping. Operators can grep the log for the SHARE block instead
        # of running networking_cli show-fingerprint separately.
        self._logger.info(
            "[NETWORKING] Identity ready. Fingerprint: %s\n"
            "[NETWORKING] Cert PEM (paste into peers[].cert_pem on other "
            "nodes, OR save as their _keys/peers/<this-hostname>.pem and "
            "use cert_file):\n%s",
            self.own_fingerprint, cert_pem,
        )

    def _extract_peer_fingerprint(self, writer: asyncio.StreamWriter) -> str:
        """Compute SPKI SHA-256 fingerprint of the TLS peer's cert. Used
        as the post-handshake identity gate in K-3.
        """
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization as _ser

        ssl_object = writer.get_extra_info("ssl_object")
        if ssl_object is None:
            raise ConnectionError("non-TLS connection")
        cert_der = ssl_object.getpeercert(binary_form=True)
        if not cert_der:
            raise ConnectionError("peer presented no cert")
        cert = x509.load_der_x509_certificate(cert_der)
        spki = cert.public_key().public_bytes(
            encoding=_ser.Encoding.DER,
            format=_ser.PublicFormat.SubjectPublicKeyInfo,
        )
        return f"sha256:{hashlib.sha256(spki).hexdigest()}"

    def _create_server_ssl_context(self) -> ssl.SSLContext:
        """K-2 add: mTLS-pinned server context. Switched on in K-3."""
        return self._create_pinned_ssl_context(ssl.PROTOCOL_TLS_SERVER)

    def _create_client_ssl_context(self) -> ssl.SSLContext:
        """K-2 add: mTLS-pinned client context. Switched on in K-3."""
        return self._create_pinned_ssl_context(ssl.PROTOCOL_TLS_CLIENT)

    def _create_pinned_ssl_context(self, protocol) -> ssl.SSLContext:
        """Shared body for the K-2 pinned-mTLS contexts. Each peer's PEM
        cert is loaded as a trust anchor (each self-signed cert is its
        own CA after the BasicConstraints(ca=True) extension added in
        generate_keypair). Post-handshake SPKI pin check is the actual
        identity gate (in _handle_client / _create_connection, K-3).
        """
        context = ssl.SSLContext(protocol)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        # Order matters in Python 3.10+: verify_mode must be set BEFORE
        # check_hostname=False, otherwise check_hostname=False raises ValueError.
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = False
        context.load_cert_chain(str(self.cert_path), str(self.key_path))
        if self.peers:
            cadata = "\n".join(p.cert_pem for p in self.peers)
            context.load_verify_locations(cadata=cadata)
        return context

    def _pool_key(self, IP: str) -> tuple[str, int]:
        """Connection-pool key for an IP. Always (IP, port) — two peers on
        the same IP but different ports get separate pools.
        """
        return (IP, self._resolve_port(IP))

    # Message Protocol Utilities

    async def _send_message(
        self, writer: asyncio.StreamWriter, msg_type: int, data: any
    ) -> None:
        """Serialize and send a message with length prefix."""
        try:
            payload = pickle.dumps(data)
            if len(payload) > MAX_MESSAGE_SIZE:
                raise ValueError(
                    f"Message size {len(payload)} exceeds maximum {MAX_MESSAGE_SIZE}"
                )

            # Format: [4-byte length][1-byte message_type][payload]
            msg_length = len(payload) + 1  # +1 for message type byte
            header = struct.pack(">IB", msg_length, msg_type)

            # Log endpoint-related messages
            if msg_type == MSG_HAS_ENDPOINT:
                self._logger.debug(
                    f"[MESSAGE] Sending HAS_ENDPOINT: payload_size={len(payload)}, "
                    f"data={data}"
                )

            writer.write(header + payload)
            await writer.drain()
        except Exception as e:
            msg_type_name = {
                MSG_HAS_ENDPOINT: "HAS_ENDPOINT",
                MSG_EXECUTE: "EXECUTE",
                MSG_EXECUTE_STREAM: "EXECUTE_STREAM",
                MSG_PING: "PING",
                MSG_INFO: "INFO",
                MSG_FIND_TAGGED_ENDPOINTS: "FIND_TAGGED_ENDPOINTS",
                MSG_PUBLISH_EVENT: "PUBLISH_EVENT",
                MSG_REQUEST_EVENT: "REQUEST_EVENT",
                MSG_REQUEST_EVENT_STREAM: "REQUEST_EVENT_STREAM",
                MSG_SUB_ADVERTISE: "SUB_ADVERTISE",
                MSG_SUB_DELTA: "SUB_DELTA",
                MSG_RESULT: "RESULT",
                MSG_ERROR: "ERROR",
            }.get(msg_type, f"UNKNOWN({msg_type})")
            self._logger.exception(
                f"[MESSAGE] Error sending message type {msg_type_name} ({msg_type})"
            )
            raise

    async def _receive_message(self, reader: asyncio.StreamReader) -> Tuple[int, any]:
        """Read a message: length, message type, and payload."""
        try:
            # Read 4-byte length header
            length_bytes = await reader.readexactly(4)
            msg_length = struct.unpack(">I", length_bytes)[0]

            if msg_length > MAX_MESSAGE_SIZE:
                raise ValueError(
                    f"Message length {msg_length} exceeds maximum {MAX_MESSAGE_SIZE}"
                )

            # Read message type (1 byte) and payload
            msg_type_byte = await reader.readexactly(1)
            msg_type = msg_type_byte[0]

            payload_length = msg_length - 1
            if payload_length > 0:
                payload = await reader.readexactly(payload_length)
                data = safe_loads(payload)
            else:
                data = None

            # Log endpoint-related messages
            if msg_type == MSG_HAS_ENDPOINT:
                self._logger.debug(
                    f"[MESSAGE] Received HAS_ENDPOINT: payload_size={payload_length}, "
                    f"data_keys={list(data.keys()) if isinstance(data, dict) else 'N/A'}"
                )
            elif (
                msg_type == MSG_RESULT
                and isinstance(data, dict)
                and "available" in data
            ):
                self._logger.debug(
                    f"[MESSAGE] Received RESULT (endpoint check): available={data.get('available')}, "
                    f"hostname={data.get('hostname')}"
                )

            return msg_type, data
        except asyncio.IncompleteReadError as e:
            self._logger.debug(f"[MESSAGE] Incomplete read: {e}")
            raise ConnectionError("Connection closed unexpectedly")
        except Exception as e:
            self._logger.exception("[MESSAGE] Error receiving message")
            raise

    async def _send_stream_chunk(
        self, writer: asyncio.StreamWriter, chunk: any
    ) -> None:
        """Send a chunk for streaming (automatically chunks large objects)."""
        try:
            payload = pickle.dumps(chunk)

            # If chunk is large, split it
            if len(payload) > CHUNK_SIZE:
                offset = 0
                while offset < len(payload):
                    chunk_data = payload[offset : offset + CHUNK_SIZE]
                    chunk_length = len(chunk_data) + 1
                    header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                    writer.write(header + chunk_data)
                    await writer.drain()
                    offset += CHUNK_SIZE
            else:
                # Small chunk, send directly
                chunk_length = len(payload) + 1
                header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                writer.write(header + payload)
                await writer.drain()
        except Exception as e:
            self._logger.exception("Error sending stream chunk")
            raise

    async def _send_end_stream(self, writer: asyncio.StreamWriter) -> None:
        """Send end of stream marker."""
        try:
            header = struct.pack(">IB", 1, MSG_END_STREAM)
            writer.write(header)
            await writer.drain()
        except Exception as e:
            self._logger.exception("Error sending end stream marker")
            raise

    async def _send_error(self, writer: asyncio.StreamWriter, error_msg: str) -> None:
        """Send an error message."""
        try:
            await self._send_message(writer, MSG_ERROR, error_msg)
        except Exception as e:
            self._logger.exception("Error sending error message")
            raise

    async def _send_error_pickled(
        self, writer: asyncio.StreamWriter, exc: BaseException
    ) -> None:
        """Ship pickled exception INSTANCE on MSG_ERROR (locked #13).

        Stage A/B's existing _send_error(writer, str) path coexists; Stage
        D removes the bare-string variant when old MSG types die.
        """
        try:
            await self._send_message(writer, MSG_ERROR, exc)
        except Exception:
            self._logger.exception("Error sending pickled error message")
            raise

    def _create_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context for server."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

        if self.cert_file and self.key_file:
            # Load certificates from files
            context.load_cert_chain(self.cert_file, self.key_file)
            self._logger.info(
                f"Loaded SSL certificates: cert={self.cert_file}, key={self.key_file}"
            )
        else:
            # For testing, create a self-signed cert (requires cryptography library)
            # For production, users should provide proper certificates
            self._logger.warning(
                "No SSL certificates provided. Generating self-signed certificate for testing. "
                "For production, provide cert_file and key_file in config."
            )
            try:
                from cryptography import x509
                from cryptography.x509.oid import NameOID
                from cryptography.hazmat.primitives import hashes, serialization
                from cryptography.hazmat.primitives.asymmetric import rsa
                import datetime

                # Generate private key
                private_key = rsa.generate_private_key(
                    public_exponent=65537,
                    key_size=2048,
                )

                # Create certificate
                subject = issuer = x509.Name(
                    [
                        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
                        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Test"),
                        x509.NameAttribute(NameOID.LOCALITY_NAME, "Test"),
                        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AIO Assistant"),
                        x509.NameAttribute(NameOID.COMMON_NAME, socket.gethostname()),
                    ]
                )

                cert = (
                    x509.CertificateBuilder()
                    .subject_name(subject)
                    .issuer_name(issuer)
                    .public_key(private_key.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(datetime.datetime.utcnow())
                    .not_valid_after(
                        datetime.datetime.utcnow() + datetime.timedelta(days=365)
                    )
                    .add_extension(
                        x509.SubjectAlternativeName(
                            [
                                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                            ]
                        ),
                        critical=False,
                    )
                    .sign(private_key, hashes.SHA256())
                )

                # Load into SSL context
                cert_pem = cert.public_bytes(serialization.Encoding.PEM)
                key_pem = private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )

                import tempfile

                with tempfile.NamedTemporaryFile(
                    mode="wb", delete=False, suffix=".pem"
                ) as cert_file:
                    cert_file.write(cert_pem)
                    temp_cert = cert_file.name
                with tempfile.NamedTemporaryFile(
                    mode="wb", delete=False, suffix=".pem"
                ) as key_file:
                    key_file.write(key_pem)
                    temp_key = key_file.name

                context.load_cert_chain(temp_cert, temp_key)
                self._temp_ssl_files.extend([temp_cert, temp_key])
                self._logger.info("Generated self-signed certificate for testing")
            except ImportError:
                raise RuntimeError(
                    "SSL certificates required. Either provide cert_file and key_file in config, "
                    "or install 'cryptography' package to generate self-signed certificates."
                )
            except Exception as e:
                self._logger.exception("Failed to generate SSL certificate")
                raise RuntimeError(f"Failed to set up SSL: {e}")

        return context

    async def start(self):
        """Starts socket server without blocking the main loop.

        K-3 (B-066): identity is loaded/generated, peers must be configured
        non-empty (else the trust store is empty and OpenSSL rejects every
        connection with an opaque error — fail fast with an actionable
        message instead).
        """
        if not self.peers:
            raise RuntimeError(
                "[NETWORKING] Cannot start with empty peers list. The mTLS "
                "trust store would be empty, causing every incoming and outgoing "
                "connection to fail with an opaque OpenSSL error. Either:\n"
                "  - Add at least one peer to networking.peers in your config, OR\n"
                "  - Disable networking entirely by removing the networking section."
            )
        self._load_or_generate_identity()
        self._logger.info(
            f"[SERVER] Starting server: port={self.port}, discover_nodes={self.discover_nodes}, "
            f"direct_discoverable={self.direct_discoverable}, auto_discoverable={self.auto_discoverable}, "
            f"heartbeat_interval={self.heartbeat_interval}, lookup_interval={self.lookup_interval}, "
            f"liveness_timeout={self.liveness_timeout}"
        )
        self.ssl_context = self._create_server_ssl_context()

        # Create socket server
        async def handle_client(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ):
            """Handle incoming client connection."""
            try:
                await self._handle_client(reader, writer)
            except Exception as e:
                self._logger.exception("Error handling client connection")
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        self.server = await asyncio.start_server(
            handle_client,
            host="0.0.0.0",
            port=self.port,
            ssl=self.ssl_context,
        )

        self._logger.info(f"Socket server started on 0.0.0.0:{self.port} with TLS")

        # Run server in background
        async def serve():
            async with self.server:
                await self.server.serve_forever()

        self.server_task = asyncio.create_task(serve())
        await asyncio.sleep(0)  # let it start properly

        # Start background loops
        if self.discover_nodes:
            # Initial discovery to seed nodes
            try:
                await self.update_all_nodes()
            except Exception:
                self._logger.debug("Initial node discovery failed; continuing")

            async def lookup_loop():
                while True:
                    try:
                        await self.update_all_nodes()
                    except Exception:
                        self._logger.debug("Periodic node discovery failed")
                    await asyncio.sleep(self.lookup_interval)

            self._logger.debug("[SERVER] Starting discovery lookup loop task")
            self.discovery_task = asyncio.create_task(lookup_loop())

        async def heartbeat_loop():
            while True:
                try:
                    # Iterate over a snapshot to avoid concurrent modification
                    for node in list(self.nodes):
                        try:
                            if not node.enabled:
                                continue
                            ok = await self.heartbeat_node(
                                node, timeout=self.liveness_timeout
                            )
                            if not ok:
                                # PR3 Stage C step 17 path #1: route through
                                # _mark_node_dead so advert state drops.
                                await self._mark_node_dead(node)
                        except Exception:
                            # Mark node disabled on heartbeat failure
                            try:
                                await self._mark_node_dead(node)
                            except Exception:
                                pass
                except Exception:
                    self._logger.debug("Heartbeat iteration failed")
                await asyncio.sleep(self.heartbeat_interval)

        self._logger.debug("[SERVER] Starting heartbeat loop task")
        self.heartbeat_task = asyncio.create_task(heartbeat_loop())

        # PR3 Stage C: networking is now ready for advert broadcasts
        # (locked #18 step 15 guard). Subscriptions registered before
        # start() get picked up by the symmetric initial-exchange when
        # peers are discovered.
        self.is_ready = True

    async def stop(self):
        """Stop the socket server and close all connections."""
        self._logger.info("[SERVER] Stopping server and background tasks")

        # PR3 Stage C: flip ready flag FIRST so concurrent broadcast
        # hooks become no-ops (locked #18 step 15 guard).
        self.is_ready = False

        # Cancel background tasks first
        for task in [self.heartbeat_task, self.discovery_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # PR3 Stage C step 17 path #3: drop advert state for every peer
        # we've ever known about, BEFORE closing pooled connections.
        try:
            async with self._adverts_struct_lock:
                hosts = list(
                    set(self._inbound_adverts.keys())
                    | set(self._outbound_adverts.keys())
                    | set(self._snapshot_sent)
                )
            for h in hosts:
                try:
                    await self._drop_peer_advert_state(h)
                except Exception:
                    self._logger.debug(
                        "advert drop on stop() for %s failed", h, exc_info=True
                    )
        except Exception:
            self._logger.debug("advert cleanup loop in stop() failed", exc_info=True)

        if self.server:
            self.server.close()
            await self.server.wait_closed()

        if self.server_task:
            self.server_task.cancel()
            try:
                await self.server_task
            except asyncio.CancelledError:
                pass

        # Close all pooled connections (key is (ip, port))
        for key, pool in self.connection_pools.items():
            closed = 0
            while not pool.empty():
                try:
                    reader, writer = await asyncio.wait_for(pool.get(), timeout=0.1)
                    writer.close()
                    await writer.wait_closed()
                    closed += 1
                except (asyncio.TimeoutError, Exception):
                    break
            if closed:
                ip_, port_ = key
                self._logger.debug(
                    f"[CONNECTION] Closed {closed} pooled connections for {ip_}:{port_}"
                )

        # Clean up temp SSL files
        for path in self._temp_ssl_files:
            try:
                os.remove(path)
                self._logger.debug(f"[SSL] Removed temp file: {path}")
            except OSError:
                pass
        self._temp_ssl_files.clear()

        self._logger.info("Socket server stopped")

    # Server-side Request Handlers

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Main server-side connection handler with mTLS pin check.

        K-3 (B-066) FIRST ACT: extract peer SPKI fingerprint, look up in
        peers_by_fingerprint. Pin failure -> SILENT close, NO _send_message
        ever fires on a non-pinned peer. Pin check is in its own try/except
        so unexpected exceptions (e.g. ssl_object is None) also take the
        silent-close path — the outer except Exception is unreachable from
        a pre-pin failure.
        """
        client_addr = writer.get_extra_info("peername")
        self._logger.debug(f"New client connection from {client_addr}")

        conn_context: Dict[str, Any] = {}

        # === FIRST ACT — pin check (silent close on fail) ===
        try:
            peer_fp = self._extract_peer_fingerprint(writer)
            peer_cfg = self.peers_by_fingerprint.get(peer_fp)
            if peer_cfg is None:
                # Security review MED fix: log at DEBUG, not WARNING.
                # A port scanner sweeping the listener generates one log
                # line per probe; at WARNING that floods the log and
                # buries real security events.
                self._logger.debug(
                    "[B066] unpinned peer fingerprint=%s from %s — closing silently",
                    peer_fp, client_addr,
                )
                writer.close()
                try: await writer.wait_closed()
                except Exception: pass
                return
        except Exception as e:
            # Security review MED fix (companion): pin-extraction failure
            # via TLS-layer probe is also high-volume during scans. DEBUG
            # is correct severity; an actual misconfiguration produces a
            # one-shot log because only legitimate peers ever reach here.
            self._logger.debug(
                "[B066] pin extraction failed for %s: %s — closing silently",
                client_addr, e,
            )
            writer.close()
            try: await writer.wait_closed()
            except Exception: pass
            return

        conn_context["peer_fingerprint"] = peer_fp
        conn_context["peer_hostname"] = peer_cfg.hostname
        conn_context["system_caller"] = peer_cfg.system_caller
        self._logger.info(
            "[NETWORKING] Pinned connection from %s hostname=%s system_caller=%s",
            client_addr, peer_cfg.hostname, peer_cfg.system_caller,
        )

        try:
            # Process requests
            while True:
                try:
                    msg_type, data = await self._receive_message(reader)
                except (ConnectionError, ConnectionResetError):
                    self._logger.debug(
                        f"Connection lost while handling client {client_addr}"
                    )
                    break
                except pickle.UnpicklingError as e:
                    # K-3 review HIGH fix: SafeUnpickler rejection from a
                    # pinned peer is treated as RCE attempt. Log and SILENT
                    # close — no _send_error wire response (don't help an
                    # attacker map the allowlist by observing error frames).
                    self._logger.warning(
                        "[B066] disallowed-class deserialization from %s "
                        "(fp=%s hostname=%s): %s",
                        client_addr, peer_fp,
                        conn_context.get("peer_hostname"), e,
                    )
                    break

                if msg_type == MSG_EXECUTE:
                    await self._handle_execute(reader, writer, data, conn_context)
                elif msg_type == MSG_EXECUTE_STREAM:
                    await self._handle_execute_stream(reader, writer, data, conn_context)
                elif msg_type == MSG_HAS_ENDPOINT:
                    self._logger.debug(
                        f"[ENDPOINT] Routing HAS_ENDPOINT message from {client_addr} to handler"
                    )
                    await self._handle_has_endpoint(reader, writer, data, conn_context)
                elif msg_type == MSG_PING:
                    await self._handle_ping(reader, writer, data, conn_context)
                elif msg_type == MSG_INFO:
                    await self._handle_info(reader, writer, data, conn_context)
                elif msg_type == MSG_FIND_TAGGED_ENDPOINTS:
                    await self._handle_find_tagged_endpoints(reader, writer, data, conn_context)
                # PR3 Stage C dispatch — locked #17 conn_context threaded
                elif msg_type == MSG_PUBLISH_EVENT:
                    await self._handle_publish_event(reader, writer, data, conn_context)
                elif msg_type == MSG_REQUEST_EVENT:
                    await self._handle_request_event(reader, writer, data, conn_context)
                elif msg_type == MSG_REQUEST_EVENT_STREAM:
                    await self._handle_request_event_stream(
                        reader, writer, data, conn_context
                    )
                elif msg_type == MSG_SUB_ADVERTISE:
                    await self._handle_sub_advertise(reader, writer, data, conn_context)
                elif msg_type == MSG_SUB_DELTA:
                    await self._handle_sub_delta(reader, writer, data, conn_context)
                else:
                    self._logger.warning(
                        f"[MESSAGE] Unknown message type {msg_type} from {client_addr}"
                    )
                    await self._send_error(writer, f"Unknown message type: {msg_type}")
                    break

        except ConnectionError:
            self._logger.debug(f"Client {client_addr} disconnected")
        except Exception as e:
            self._logger.exception(f"Error handling client {client_addr}")
            try:
                await self._send_error(writer, str(e))
            except Exception:
                pass
        finally:
            # PR3 Stage C (locked #17): drop advert state on connection
            # close ONLY IF heartbeat has also marked the peer dead.
            # Heartbeat is the source-of-truth — pooled-connection-recycle
            # would over-eagerly drop on every transient pool churn.
            try:
                peer_hostname = conn_context.get("peer_hostname")
                if peer_hostname:
                    node = next(
                        (
                            n for n in list(self.nodes)
                            if n.hostname == peer_hostname
                        ),
                        None,
                    )
                    if node is None:
                        # Cycle-5 fix: client-only peer (never in
                        # self.nodes — e.g. an inbound publish_event from a
                        # transient one-shot client). Heartbeat only watches
                        # self.nodes, so deferring to heartbeat would leak
                        # _inbound_adverts / _inbound_global_order entries
                        # forever. With no Node entry there is no heartbeat
                        # ownership to defer to — drop directly.
                        await self._drop_peer_advert_state(peer_hostname)
                    elif not node.enabled:
                        await self._drop_peer_advert_state(peer_hostname)
                    else:
                        self._logger.debug(
                            "_handle_client finally: peer %s still enabled — heartbeat owns drop",
                            peer_hostname,
                        )
            except Exception:
                self._logger.debug(
                    "_handle_client finally: advert cleanup hook failed",
                    exc_info=True,
                )

    async def _handle_execute(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data: dict,
        conn_context: Optional[Dict[str, Any]] = None,
    ):
        """Handle EXECUTE message."""
        conn_context = conn_context or {}
        try:
            plugin = data.get("plugin")
            method = data.get("method")
            plugin_uuid = data.get("plugin_uuid", None)
            author = data.get("author", "remote")
            author_id = data.get("author_id", "remote")
            timeout = data.get("timeout")
            author_host = data.get("author_host")
            request_id = data.get("request_id")
            args = data.get("args", [])

            # B-018b GUARD (split, K-5) — see _apply_b018b_guard for the full
            # logic. Part 1 gates author=="system" on system_caller; Part 2
            # rewrites impersonating author_id unconditionally (preserving
            # author="system" if Part 1 permitted it).
            author, author_id, _denied = await self._apply_b018b_guard(
                author, author_id, conn_context, writer, "[EXECUTE]"
            )
            if _denied:
                return

            self._logger.info(
                f"[EXECUTE] Request: plugin={plugin}, method={method}, plugin_uuid={plugin_uuid}, "
                f"author={author}, author_id={author_id}, author_host={author_host}, request_id={request_id}, "
                f"args_type={type(args).__name__}, timeout={timeout}"
            )

            # Execute plugin method
            # Pass args as a single object — PluginCore.execute unpacks internally
            if isinstance(args, list):
                args = tuple(args)
            result = await self.plugin_core.execute(
                plugin,
                method,
                args=args if args else None,
                plugin_uuid=plugin_uuid,
                hosts="local",
                timeout=timeout,
                author=author,
                author_id=author_id,
                author_host=author_host,
                request_id=request_id,
            )

            # Send result as a single message (use streaming protocol for large objects)
            # For large objects, we still use STREAM_CHUNK + END_STREAM to be consistent
            payload = pickle.dumps(result)
            if len(payload) > CHUNK_SIZE:
                # Split large result into chunks
                sent = 0
                offset = 0
                while offset < len(payload):
                    chunk_data = payload[offset : offset + CHUNK_SIZE]
                    chunk_length = len(chunk_data) + 1
                    header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                    writer.write(header + chunk_data)
                    await writer.drain()
                    offset += CHUNK_SIZE
                    sent += 1
                self._logger.debug(
                    f"[EXECUTE] Sent chunked result: chunks={sent}, bytes={len(payload)}"
                )
            else:
                # Small result, send as single chunk
                chunk_length = len(payload) + 1
                header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                writer.write(header + payload)
                await writer.drain()

            try:
                result_type = type(result).__name__
            except Exception:
                result_type = "unknown"

            await self._send_end_stream(writer)
            self._logger.info(
                f"[EXECUTE] Completed: result_type={result_type}, size_bytes={len(payload)}"
            )

        except NetworkRequestException as e:
            await self._send_error(writer, str(e))
        except Exception as e:
            self._logger.exception("Exception in _handle_execute")
            await self._send_error(writer, str(e))

    async def _handle_execute_stream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data: dict,
        conn_context: Optional[Dict[str, Any]] = None,
    ):
        """Handle EXECUTE_STREAM message."""
        conn_context = conn_context or {}
        try:
            plugin = data.get("plugin")
            method = data.get("method")
            plugin_uuid = data.get("plugin_uuid")
            author = data.get("author", "remote")
            author_id = data.get("author_id", "remote")
            timeout = data.get("timeout")
            author_host = data.get("author_host")
            request_id = data.get("request_id")
            args = data.get("args", [])

            # B-018b GUARD (split, K-5) — see _apply_b018b_guard.
            author, author_id, _denied = await self._apply_b018b_guard(
                author, author_id, conn_context, writer, "[EXECUTE_STREAM]"
            )
            if _denied:
                return

            self._logger.info(
                f"[EXECUTE_STREAM] Request: plugin={plugin}, method={method}, plugin_uuid={plugin_uuid}, "
                f"author={author}, author_id={author_id}, author_host={author_host}, request_id={request_id}, "
                f"args_type={type(args).__name__}, timeout={timeout}"
            )

            # Execute streaming plugin method
            # Pass args as a single object — PluginCore.execute_stream unpacks internally
            if isinstance(args, list):
                args = tuple(args)
            agen = self.plugin_core.execute_stream(
                plugin=plugin,
                method=method,
                args=args if args else None,
                plugin_uuid=plugin_uuid,
                hosts="local",
                author=author,
                author_id=author_id,
                timeout=timeout,
                author_host=author_host,
                request_id=request_id,
            )

            # Stream results - each yielded item as chunks + item boundary marker
            sent_items = 0
            async for line in agen:
                try:
                    # Send each item - may split if very large
                    payload = pickle.dumps(line)
                    if len(payload) > CHUNK_SIZE:
                        # Split large item into chunks
                        parts = 0
                        offset = 0
                        while offset < len(payload):
                            chunk_data = payload[offset : offset + CHUNK_SIZE]
                            chunk_length = len(chunk_data) + 1
                            header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                            writer.write(header + chunk_data)
                            await writer.drain()
                            offset += CHUNK_SIZE
                            parts += 1
                        self._logger.debug(
                            f"[EXECUTE_STREAM] Sent large item in {parts} chunks, bytes={len(payload)}"
                        )
                    else:
                        # Small item, send as single chunk
                        chunk_length = len(payload) + 1
                        header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                        writer.write(header + payload)
                        await writer.drain()
                    # Mark end of this item so receiver knows where item boundaries are
                    item_end_header = struct.pack(">IB", 1, MSG_STREAM_ITEM_END)
                    writer.write(item_end_header)
                    await writer.drain()
                    sent_items += 1
                except Exception as e:
                    self._logger.exception("Failed to send stream chunk")
                    err_obj = ("__STREAM_ERROR__", str(e))
                    err_payload = pickle.dumps(err_obj)
                    chunk_length = len(err_payload) + 1
                    header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                    writer.write(header + err_payload)
                    await writer.drain()
                    # F5 fix: MUST send MSG_STREAM_ITEM_END after the error
                    # chunk so the client decoder's sentinel check in the
                    # MSG_STREAM_ITEM_END branch fires (lines 3083-3105 of
                    # this file). Without ITEM_END, the chunk gets buffered
                    # and yielded as a normal final item via MSG_END_STREAM
                    # path, with the client's caller seeing the error tuple
                    # as data and no exception.
                    item_end_header = struct.pack(">IB", 1, MSG_STREAM_ITEM_END)
                    writer.write(item_end_header)
                    await writer.drain()
                    break

            await self._send_end_stream(writer)
            self._logger.info(f"[EXECUTE_STREAM] Completed: items_sent={sent_items}")

        except Exception as e:
            self._logger.exception("Exception while streaming")
            try:
                err_obj = ("__STREAM_EXCEPTION__", str(e))
                await self._send_stream_chunk(writer, err_obj)
                # F5 fix: MSG_STREAM_ITEM_END before MSG_END_STREAM so the
                # client decoder's sentinel check fires (line 3223+
                # empty-payload ITEM_END branch).
                item_end_header = struct.pack(">IB", 1, MSG_STREAM_ITEM_END)
                writer.write(item_end_header)
                await writer.drain()
                await self._send_end_stream(writer)
            except Exception:
                pass

    async def _handle_has_endpoint(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data: dict,
        conn_context: Optional[Dict[str, Any]] = None,
    ):
        """Handle HAS_ENDPOINT message (checks plugin existence AND endpoint in one call)."""
        conn_context = conn_context or {}
        client_addr = writer.get_extra_info("peername")
        try:
            access_name = data.get("access_name")
            plugin_uuid = data.get("plugin_uuid")
            requester_id = data.get("requester_id")
            target_plugin = data.get("target_plugin")

            # F3 fix (B-018b sibling): wire-supplied requester_id cannot
            # be trusted for find_endpoint's trust calculus. A peer can
            # claim requester_id == self.hostname (is_local_system=True)
            # or requester_id in plugins_by_uuid (is_local_plugin=True),
            # bypassing the remote: false eligibility check. Force a
            # remote-classified sentinel that is neither.
            _peer_addr = writer.get_extra_info("peername")
            _peer_repr = (
                f"{_peer_addr[0]}:{_peer_addr[1]}" if _peer_addr else "unknown"
            )
            if (
                requester_id == self.plugin_core.hostname
                or requester_id in self.plugin_core.plugins_by_uuid
            ):
                self._logger.warning(
                    "[ENDPOINT] B-018b guard: rejected wire-supplied "
                    "requester_id=%r from peer=%s; rewriting to remote sentinel",
                    requester_id, _peer_repr,
                )
                requester_id = f"remote-peer:{_peer_repr}"

            self._logger.info(
                f"[ENDPOINT] Received HAS_ENDPOINT request from {client_addr}: "
                f"access_name='{access_name}', plugin_uuid={plugin_uuid}, "
                f"requester_id={requester_id}, target_plugin={target_plugin}"
            )

            # Use find_endpoint which already does both checks
            self._logger.debug(
                f"[ENDPOINT] Calling find_endpoint: access_name='{access_name}', "
                f"hosts='local', plugin_uuid={plugin_uuid}, requester_id={requester_id}, "
                f"target_plugin={target_plugin}"
            )
            plugin, endpoint, node = await self.plugin_core.find_endpoint(
                access_name=access_name,
                hosts="local",
                plugin_uuid=plugin_uuid,
                requester_id=requester_id,
                target_plugin=target_plugin,
            )

            available = plugin is not None and endpoint is not None
            self._logger.info(
                f"[ENDPOINT] find_endpoint result: available={available}, "
                f"plugin={plugin.plugin_name if plugin else None}, "
                f"endpoint={endpoint.get('name') if endpoint else None}, "
                f"node={node.IP if node else None}"
            )

            response = {
                "available": available,
                "hostname": self.plugin_core.hostname,
            }

            if plugin:
                response["plugin_info"] = {
                    "name": plugin.plugin_name,
                    "version": getattr(plugin, "version", "unknown"),
                    "uuid": plugin.plugin_uuid,
                    "description": getattr(plugin, "description", "Remote plugin"),
                }
                self._logger.debug(
                    f"[ENDPOINT] Plugin info: name={plugin.plugin_name}, "
                    f"uuid={plugin.plugin_uuid}, version={getattr(plugin, 'version', 'unknown')}"
                )
            else:
                response["plugin_info"] = None
                self._logger.debug("[ENDPOINT] No plugin found")

            if endpoint:
                response["endpoint"] = endpoint
                self._logger.debug(
                    f"[ENDPOINT] Endpoint info: {endpoint.get('name') if isinstance(endpoint, dict) else endpoint}"
                )
            else:
                response["endpoint"] = None
                self._logger.debug("[ENDPOINT] No endpoint found")

            self._logger.info(
                f"[ENDPOINT] Sending response to {client_addr}: available={available}"
            )
            await self._send_message(writer, MSG_RESULT, response)

        except Exception as e:
            self._logger.exception(
                f"[ENDPOINT] Exception in _handle_has_endpoint from {client_addr}: {e}"
            )
            await self._send_error(writer, str(e))

    async def _handle_find_tagged_endpoints(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data: dict,
        conn_context: Optional[Dict[str, Any]] = None,
    ):
        """Handle FIND_TAGGED_ENDPOINTS message -- returns all local endpoints matching a tag."""
        conn_context = conn_context or {}
        client_addr = writer.get_extra_info("peername")
        try:
            tag = data.get("tag")
            self._logger.info(
                f"[TAG_SEARCH] Received FIND_TAGGED_ENDPOINTS from {client_addr}: tag='{tag}'"
            )

            endpoints = []
            # F4 fix: skip plugins/endpoints not flagged remote-eligible
            # so wire callers can't enumerate non-public endpoint metadata
            # via tag search. Mirrors find_endpoint's remote-eligibility
            # gate (PluginCore.py:2104-2107). Tag-search bypassed it
            # entirely before this guard.
            for plugin in self.plugin_core.plugins.values():
                if not plugin.enabled:
                    continue
                if not getattr(plugin, "remote", False):
                    continue
                for endpoint in plugin.endpoints.values():
                    if not endpoint.get("remote", False):
                        continue
                    if tag in endpoint.get("tags", []):
                        endpoints.append(
                            {
                                "plugin_name": plugin.plugin_name,
                                "plugin_uuid": plugin.plugin_uuid,
                                "plugin_version": getattr(
                                    plugin, "version", "unknown"
                                ),
                                "plugin_description": getattr(
                                    plugin, "description", ""
                                ),
                                "endpoint": endpoint,
                            }
                        )

            self._logger.info(
                f"[TAG_SEARCH] Found {len(endpoints)} endpoint(s) for tag '{tag}', "
                f"sending to {client_addr}"
            )
            await self._send_message(
                writer,
                MSG_RESULT,
                {"hostname": self.plugin_core.hostname, "endpoints": endpoints},
            )

        except Exception as e:
            self._logger.exception(
                f"[TAG_SEARCH] Exception in _handle_find_tagged_endpoints from {client_addr}: {e}"
            )
            await self._send_error(writer, str(e))

    # ── PR3 Stage C handlers + helpers (locked #1, #13, #15, #17) ──

    def _safe_peer_ip(self, writer: asyncio.StreamWriter) -> Optional[str]:
        """Best-effort extract of TCP peer IP from a StreamWriter."""
        try:
            peer = writer.get_extra_info("peername")
            if peer and isinstance(peer, tuple) and len(peer) >= 1:
                return peer[0]
        except Exception:
            pass
        return None

    def _hosts_match(
        self,
        hosts: Union[str, list, None],
        blocked_hosts: Union[str, list, None],
        peer_hostname: str,
    ) -> bool:
        """Networking-layer peer-host filter (PR3 PLAN F step 5a).

        Mirrors PluginCore's nested ``_matches_remote_node`` /
        ``_is_remote_node_blocked`` predicates without re-importing them
        (they're closures inside find_endpoint).
        """
        def _matches(val):
            if isinstance(val, str):
                return val in ("remote", "any") or val == peer_hostname
            if isinstance(val, list):
                return peer_hostname in val
            return False

        def _blocked(val):
            if val is None:
                return False
            if isinstance(val, str):
                return val in ("remote", "any") or val == peer_hostname
            if isinstance(val, list):
                return peer_hostname in val
            return False

        return _matches(hosts) and not _blocked(blocked_hosts)

    def _should_advertise_sub_to_peer(self, sub, peer_hostname: str) -> bool:
        """PR3 PLAN H. True iff this LOCAL sub should be advertised to a
        peer with the given hostname. Lookup ORDER:
          1. enabled=False → False
          2. hosts="local" → False (sub explicitly opts out of remote)
          3. hosts predicate (positive accept)
          4. blocked_hosts subtract
        """
        if peer_hostname is None:
            return False
        if not getattr(sub, "enabled", True):
            return False

        sub_hosts = getattr(sub, "hosts", None)
        if sub_hosts == "local":
            return False

        if sub_hosts in ("any", "remote"):
            accepts = True
        elif isinstance(sub_hosts, str):
            accepts = (sub_hosts == peer_hostname)
        elif isinstance(sub_hosts, list):
            accepts = peer_hostname in sub_hosts
        elif sub_hosts is None:
            accepts = True
        else:
            return False

        if not accepts:
            return False

        sub_blocked = getattr(sub, "blocked_hosts", None)
        if sub_blocked is None:
            return True
        if isinstance(sub_blocked, str):
            if sub_blocked in ("any", "remote") or sub_blocked == peer_hostname:
                return False
        elif isinstance(sub_blocked, list):
            if (
                peer_hostname in sub_blocked
                or "any" in sub_blocked
                or "remote" in sub_blocked
            ):
                return False

        return True

    def _filter_inbound_advert(self, advert: dict) -> bool:
        """Typed-validation gate for a single inbound advert dict. Drops
        entries missing required keys / wrong types. Trust filtering
        happens OUTBOUND-side (per PLAN H)."""
        if not isinstance(advert, dict):
            return False
        sub_uuid = advert.get("sub_uuid")
        topic = advert.get("topic")
        if not isinstance(sub_uuid, str) or not sub_uuid:
            return False
        if not isinstance(topic, str) or not topic:
            return False
        return True

    def _serialize_local_sub_for_peer(self, sub) -> dict:
        """Project a Subscription to wire-payload dict. Drops receiver-only
        fields (target_plugin, target_access_name) that the peer does not need.
        """
        return {
            "sub_uuid": sub.sub_uuid,
            "topic": sub.topic_pattern,
            "hosts": sub.hosts,
            "blocked_hosts": sub.blocked_hosts,
            "authors": sub.authors,
            "blocked_authors": sub.blocked_authors,
        }

    def _self_impersonation_check(
        self,
        author_host: Optional[str],
        writer: asyncio.StreamWriter,
        msg_label: str,
    ) -> bool:
        """Locked #15: reject payloads claiming our own hostname. Returns
        True if the gate FIRED (caller should bail). Passive log + no
        state cleanup — would clear our own state if we acted on it.
        """
        if author_host is None:
            return False
        if author_host == self.plugin_core.hostname:
            try:
                peer = writer.get_extra_info("peername")
            except Exception:
                peer = None
            self._logger.warning(
                "self-impersonation rejected: peer %s claimed our hostname %s on %s",
                peer, author_host, msg_label,
            )
            return True
        return False

    async def _maybe_reciprocal_exchange(
        self,
        author_host: Optional[str],
        writer: asyncio.StreamWriter,
    ) -> None:
        """Reciprocal advert trigger (locked #7 + #10). Idempotent via
        ``_snapshot_sent`` fast-path; authoritative gate inside
        ``_initial_advert_exchange``."""
        if not author_host or author_host == self.plugin_core.hostname:
            return
        if author_host in self._snapshot_sent:
            return
        node = next(
            (n for n in list(self.nodes) if n.hostname == author_host),
            None,
        )
        if node is not None:
            asyncio.create_task(self._spawn_initial_exchange(node))
            return
        # Fallback: client-only peer not yet in node table.
        peer_ip = self._safe_peer_ip(writer)
        if peer_ip:
            asyncio.create_task(
                self._spawn_initial_exchange_for_ip(peer_ip, author_host)
            )

    async def _handle_publish_event(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data: dict,
        conn_context: Dict[str, Any],
    ) -> None:
        """MSG_PUBLISH_EVENT: receive, gate, fan-out (PR3 PLAN F)."""
        try:
            payload_dict = data if isinstance(data, dict) else {}
            author_host = payload_dict.get("author_host")

            # Self-impersonation gate (locked #15).
            if self._self_impersonation_check(
                author_host, writer, "MSG_PUBLISH_EVENT"
            ):
                return

            # locked #17: record peer hostname on first sight.
            if author_host:
                conn_context.setdefault("peer_hostname", author_host)

            # Reciprocal advert trigger (locked #7 + #10).
            await self._maybe_reciprocal_exchange(author_host, writer)

            topic = payload_dict.get("topic")
            payload = payload_dict.get("payload")
            author = payload_dict.get("author", "remote")
            author_id = payload_dict.get("author_id", "remote")
            timestamp = payload_dict.get("timestamp")
            if not isinstance(topic, str) or not topic:
                self._logger.warning(
                    "[PUBLISH_EVENT] missing/invalid topic from author_host=%s",
                    author_host,
                )
                return

            # B-018b GUARD (split, K-5) — see _apply_b018b_guard.
            author, author_id, _denied = await self._apply_b018b_guard(
                author, author_id, conn_context, writer, "[PUBLISH_EVENT]"
            )
            if _denied:
                return

            self._logger.debug(
                "[PUBLISH_EVENT] topic=%r author=%s author_host=%s",
                topic, author, author_host,
            )

            # Resolve local subs.
            try:
                all_subs = await self.plugin_core.topic_registry.find_all(topic)
            except Exception:
                self._logger.exception(
                    "[PUBLISH_EVENT] find_all failed for topic %r", topic
                )
                return

            for sub in all_subs:
                if sub.plugin_uuid not in self.plugin_core.plugins_by_uuid:
                    continue
                if not self.plugin_core._sub_accepts_remote_publisher(
                    sub, author_host, author
                ):
                    continue
                if not self.plugin_core._sub_accepts_author(sub, author):
                    continue
                try:
                    await self.plugin_core._fanout_sub(
                        sub=sub,
                        publisher=None,
                        resolved_topic=topic,
                        payload=payload,
                        kind="publish_event",
                        timestamp=timestamp if isinstance(timestamp, (int, float)) else 0.0,
                        timeout=None,
                        caller_chain=None,
                        remote_publisher_name=author,
                        remote_publisher_uuid=author_id,
                        remote_publisher_host=author_host,
                        remote_verbose=False,
                    )
                except Exception:
                    self._logger.exception(
                        "[PUBLISH_EVENT] fanout failed for sub %s", sub.sub_uuid
                    )

        except Exception:
            self._logger.exception("[PUBLISH_EVENT] handler crashed")

    async def _handle_request_event(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data: dict,
        conn_context: Dict[str, Any],
    ) -> None:
        """MSG_REQUEST_EVENT: receive, gate, fan-out, return result (PR3
        PLAN G)."""
        try:
            payload_dict = data if isinstance(data, dict) else {}
            author_host = payload_dict.get("author_host")

            if self._self_impersonation_check(
                author_host, writer, "MSG_REQUEST_EVENT"
            ):
                # F1 fix: send error frame instead of silently returning.
                # request_event_remote's `while True: _receive_message`
                # has no client-side timeout — silent return blocks the
                # caller until TCP keepalive fires (minutes). Mirror the
                # missing-topic error pattern below.
                await self._send_error_pickled(
                    writer,
                    NetworkRequestException("self-impersonation rejected"),
                )
                return

            if author_host:
                conn_context.setdefault("peer_hostname", author_host)

            await self._maybe_reciprocal_exchange(author_host, writer)

            topic = payload_dict.get("topic")
            payload = payload_dict.get("payload")
            author = payload_dict.get("author", "remote")
            author_id = payload_dict.get("author_id", "remote")
            timestamp = payload_dict.get("timestamp")
            timeout = payload_dict.get("timeout")
            if not isinstance(topic, str) or not topic:
                await self._send_error_pickled(
                    writer,
                    NetworkRequestException("missing/invalid topic"),
                )
                return

            # B-018b GUARD (split, K-5) — see _apply_b018b_guard.
            author, author_id, _denied = await self._apply_b018b_guard(
                author, author_id, conn_context, writer, "[REQUEST_EVENT]"
            )
            if _denied:
                return

            try:
                all_subs = await self.plugin_core.topic_registry.find_all(topic)
            except Exception as exc:
                self._logger.exception(
                    "[REQUEST_EVENT] find_all failed for topic %r", topic
                )
                await self._send_error_pickled(writer, NetworkRequestException(str(exc)))
                return

            local_match = None
            for sub in all_subs:
                if sub.plugin_uuid not in self.plugin_core.plugins_by_uuid:
                    continue
                if not self.plugin_core._sub_accepts_remote_publisher(
                    sub, author_host, author
                ):
                    continue
                if not self.plugin_core._sub_accepts_author(sub, author):
                    continue
                local_match = sub
                break

            if local_match is None:
                # locked #13: no-local-sub path emits NoLocalSubException.
                await self._send_error_pickled(
                    writer,
                    NoLocalSubException(
                        f"no local subscriber for topic {topic!r}"
                    ),
                )
                return

            try:
                request = await self.plugin_core._fanout_sub(
                    sub=local_match,
                    publisher=None,
                    resolved_topic=topic,
                    payload=payload,
                    kind="request_event",
                    timestamp=timestamp if isinstance(timestamp, (int, float)) else 0.0,
                    timeout=timeout,
                    caller_chain=None,
                    remote_publisher_name=author,
                    remote_publisher_uuid=author_id,
                    remote_publisher_host=author_host,
                    remote_verbose=False,
                )
                if request is None:
                    # Defense-in-depth gate inside _fanout_sub fired —
                    # treat as no-handler.
                    await self._send_error_pickled(
                        writer,
                        NoLocalSubException("fan-out gate rejected fan-out"),
                    )
                    return
                result, error, _ = await request.wait_for_result_async()
                try:
                    await request.set_collected()
                except Exception:
                    pass
                if error:
                    if isinstance(result, BaseException):
                        await self._send_error_pickled(writer, result)
                    else:
                        await self._send_error_pickled(
                            writer, RequestException(str(result))
                        )
                    return
            except RequestException as exc:
                await self._send_error_pickled(writer, exc)
                return
            except Exception as exc:
                self._logger.exception("[REQUEST_EVENT] handler crashed")
                await self._send_error_pickled(
                    writer, RequestException(str(exc))
                )
                return

            # Send result as a single MSG_STREAM_CHUNK + MSG_END_STREAM
            # (PLAN b15 / locked #6). The previous hand-rolled chunk-split
            # was framing-asymmetric vs. the client's _receive_message
            # decoder, which pickle.loads each frame; a 64KB slice of a
            # pickled blob is not itself valid pickle, so the second chunk
            # always failed. _send_message handles the MAX_MESSAGE_SIZE
            # (100MB) cap natively.
            try:
                await self._send_message(writer, MSG_STREAM_CHUNK, result)
                await self._send_message(writer, MSG_END_STREAM, None)
            except Exception:
                self._logger.exception("[REQUEST_EVENT] result send failed")

        except Exception as exc:
            self._logger.exception("[REQUEST_EVENT] handler crashed")
            try:
                await self._send_error_pickled(
                    writer, NetworkRequestException(str(exc))
                )
            except Exception:
                pass

    async def _handle_request_event_stream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data: dict,
        conn_context: Dict[str, Any],
    ) -> None:
        """MSG_REQUEST_EVENT_STREAM: streaming variant. First chunk wraps
        in Event metadata (locked #2 SERVER-SIDE wrap). Cannot reuse
        ``_fanout_sub`` (kind="request_event_stream" rejected by Stage B
        path). Dedicated streaming receiver path."""
        try:
            payload_dict = data if isinstance(data, dict) else {}
            author_host = payload_dict.get("author_host")

            if self._self_impersonation_check(
                author_host, writer, "MSG_REQUEST_EVENT_STREAM"
            ):
                # F1 fix (mirror of _handle_request_event): send error
                # frame so request_event_stream_remote doesn't deadlock
                # on its `while True: _receive_message` loop.
                await self._send_error_pickled(
                    writer,
                    NetworkRequestException("self-impersonation rejected"),
                )
                return

            if author_host:
                conn_context.setdefault("peer_hostname", author_host)

            await self._maybe_reciprocal_exchange(author_host, writer)

            topic = payload_dict.get("topic")
            req_payload = payload_dict.get("payload")
            author = payload_dict.get("author", "remote")
            author_id = payload_dict.get("author_id", "remote")
            timestamp = payload_dict.get("timestamp")
            timeout = payload_dict.get("timeout")
            if not isinstance(topic, str) or not topic:
                await self._send_error_pickled(
                    writer, NetworkRequestException("missing/invalid topic")
                )
                return

            # B-018b GUARD (split, K-5) — see _apply_b018b_guard.
            author, author_id, _denied = await self._apply_b018b_guard(
                author, author_id, conn_context, writer, "[REQUEST_EVENT_STREAM]"
            )
            if _denied:
                return

            try:
                all_subs = await self.plugin_core.topic_registry.find_all(topic)
            except Exception as exc:
                self._logger.exception(
                    "[REQUEST_EVENT_STREAM] find_all failed for topic %r", topic
                )
                await self._send_error_pickled(
                    writer, NetworkRequestException(str(exc))
                )
                return

            local_match = None
            for sub in all_subs:
                if sub.plugin_uuid not in self.plugin_core.plugins_by_uuid:
                    continue
                if not self.plugin_core._sub_accepts_remote_publisher(
                    sub, author_host, author
                ):
                    continue
                if not self.plugin_core._sub_accepts_author(sub, author):
                    continue
                local_match = sub
                break

            if local_match is None:
                await self._send_error_pickled(
                    writer,
                    NoLocalSubException(
                        f"no local subscriber for topic {topic!r}"
                    ),
                )
                return

            # Resolve bound async/sync gen method via find_endpoint
            # (C18 access check). `endpoint["func"]` is unbound — we need
            # the BOUND method via getattr on the target plugin.
            try:
                target_plugin, endpoint, _ = await self.plugin_core.find_endpoint(
                    access_name=local_match.target_access_name,
                    hosts="local",
                    plugin_uuid=local_match.target_plugin_uuid,
                    requester_id=local_match.plugin_uuid,
                    target_plugin=local_match.target_plugin,
                )
            except Exception as exc:
                self._logger.exception(
                    "[REQUEST_EVENT_STREAM] find_endpoint failed"
                )
                await self._send_error_pickled(
                    writer, RequestException(str(exc))
                )
                return

            if target_plugin is None or endpoint is None:
                await self._send_error_pickled(
                    writer,
                    RequestException(
                        f"target endpoint {local_match.target_access_name!r} "
                        f"not found on {local_match.target_plugin!r}"
                    ),
                )
                return

            internal = endpoint.get("internal_name") or local_match.target_access_name
            func = getattr(target_plugin, internal, None)
            if func is None or not (
                inspect.isasyncgenfunction(func)
                or inspect.isgeneratorfunction(func)
            ):
                await self._send_error_pickled(
                    writer,
                    RequestException("handler is not a generator function"),
                )
                return

            from utils import Event as _Event  # local import to avoid cycle

            ts = timestamp if isinstance(timestamp, (int, float)) else 0.0
            sub_id_for_event = (
                local_match.declared_id
                if local_match.declared_id is not None
                else local_match.sub_uuid
            )

            # Build Event from wire metadata (NOT via Event.from_request).
            # First chunk wraps; subsequent chunks raw.
            async def _iterate_and_send():
                first = True
                if inspect.isasyncgenfunction(func):
                    # async-gen path — we still need to feed an Event to
                    # the handler on its first call so it sees publisher
                    # metadata. Build a sentinel Event with payload=None
                    # for the first arg; the handler's first yield is
                    # what we wrap.
                    placeholder = _Event(
                        topic=topic,
                        payload=req_payload,
                        author=author,
                        author_id=author_id,
                        author_host=author_host,
                        subscription_id=sub_id_for_event,
                        timestamp=ts,
                    )
                    ait = func(placeholder).__aiter__()
                    try:
                        while True:
                            try:
                                chunk = await ait.__anext__()
                            except StopAsyncIteration:
                                break
                            if first:
                                first = False
                                wrapped = _Event(
                                    topic=topic,
                                    payload=chunk,
                                    author=author,
                                    author_id=author_id,
                                    author_host=author_host,
                                    subscription_id=sub_id_for_event,
                                    timestamp=ts,
                                )
                                await self._send_message(
                                    writer, MSG_STREAM_CHUNK, wrapped
                                )
                            else:
                                await self._send_message(
                                    writer, MSG_STREAM_CHUNK, chunk
                                )
                            await self._send_message(
                                writer, MSG_STREAM_ITEM_END, None
                            )
                    finally:
                        with contextlib.suppress(Exception):
                            await ait.aclose()
                else:
                    # Sync generator — run on the SyncDispatcher executor
                    # (Q17 + C3 isolation).
                    placeholder = _Event(
                        topic=topic,
                        payload=req_payload,
                        author=author,
                        author_id=author_id,
                        author_host=author_host,
                        subscription_id=sub_id_for_event,
                        timestamp=ts,
                    )
                    sentinel = object()
                    gen = func(placeholder)
                    loop = asyncio.get_running_loop()
                    try:
                        while True:
                            fut = loop.run_in_executor(
                                self.plugin_core.sync_dispatcher.executor,
                                lambda g=gen, s=sentinel: next(g, s),
                            )
                            chunk = await fut
                            if chunk is sentinel:
                                break
                            if first:
                                first = False
                                wrapped = _Event(
                                    topic=topic,
                                    payload=chunk,
                                    author=author,
                                    author_id=author_id,
                                    author_host=author_host,
                                    subscription_id=sub_id_for_event,
                                    timestamp=ts,
                                )
                                await self._send_message(
                                    writer, MSG_STREAM_CHUNK, wrapped
                                )
                            else:
                                await self._send_message(
                                    writer, MSG_STREAM_CHUNK, chunk
                                )
                            await self._send_message(
                                writer, MSG_STREAM_ITEM_END, None
                            )
                    finally:
                        with contextlib.suppress(Exception):
                            gen.close()

            try:
                if timeout is not None:
                    await asyncio.wait_for(_iterate_and_send(), timeout=timeout)
                else:
                    await _iterate_and_send()
                await self._send_end_stream(writer)
            except asyncio.TimeoutError:
                await self._send_error_pickled(
                    writer,
                    RequestException(
                        f"request_event_stream timed out after {timeout}s"
                    ),
                )
            except RequestException as exc:
                await self._send_error_pickled(writer, exc)
            except Exception as exc:
                self._logger.exception("[REQUEST_EVENT_STREAM] iteration crashed")
                await self._send_error_pickled(
                    writer, RequestException(str(exc))
                )

        except Exception as exc:
            self._logger.exception("[REQUEST_EVENT_STREAM] handler crashed")
            try:
                await self._send_error_pickled(
                    writer, NetworkRequestException(str(exc))
                )
            except Exception:
                pass

    async def _handle_sub_advertise(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data: dict,
        conn_context: Dict[str, Any],
    ) -> None:
        """MSG_SUB_ADVERTISE: full snapshot replace (locked #1, #5)."""
        try:
            payload_dict = data if isinstance(data, dict) else {}
            author_host = payload_dict.get("author_host")

            if self._self_impersonation_check(
                author_host, writer, "MSG_SUB_ADVERTISE"
            ):
                return

            if not isinstance(author_host, str) or not author_host:
                self._logger.warning(
                    "[SUB_ADVERTISE] invalid author_host=%r", author_host
                )
                return

            conn_context.setdefault("peer_hostname", author_host)

            # Soft anti-spoof check.
            peer_ip = self._safe_peer_ip(writer)
            if peer_ip:
                node_for_ip = next(
                    (n for n in list(self.nodes) if n.IP == peer_ip),
                    None,
                )
                if (
                    node_for_ip is not None
                    and node_for_ip.hostname
                    and node_for_ip.hostname != author_host
                ):
                    self._logger.warning(
                        "[SUB_ADVERTISE] anti-spoof: TCP peer IP %s known as %s "
                        "but wire claimed author_host=%s — accepting wire claim "
                        "(locked #1)",
                        peer_ip, node_for_ip.hostname, author_host,
                    )

            subs_payload = payload_dict.get("subscriptions")
            if not isinstance(subs_payload, list):
                self._logger.warning(
                    "[SUB_ADVERTISE] subscriptions not a list from %s", author_host
                )
                return

            # Cycle-6 fix: cap entries before acquiring the global advert
            # lock. Without this cap, an authenticated-but-misbehaving peer
            # could send a single MSG_SUB_ADVERTISE with millions of entries
            # and stall every concurrent advert-protocol operation cluster-
            # wide for the duration of the loop below. MAX_MESSAGE_SIZE
            # already bounds the wire payload, but the per-entry processing
            # cost (two dict insertions + AdvertSub construction) compounds.
            if len(subs_payload) > MAX_ADVERT_SUBS_PER_PEER:
                self._logger.warning(
                    "[SUB_ADVERTISE] rejecting oversized advert from %s: "
                    "%d entries exceeds cap %d",
                    author_host, len(subs_payload), MAX_ADVERT_SUBS_PER_PEER,
                )
                return

            # Atomic purge + reinsert (per locked #5: empty list → {}).
            async with self._adverts_struct_lock:
                self._inbound_adverts[author_host] = {}
                self._inbound_global_order = {
                    k: v
                    for k, v in self._inbound_global_order.items()
                    if k[0] != author_host
                }
                for entry in subs_payload:
                    if not self._filter_inbound_advert(entry):
                        continue
                    sub = AdvertSub(
                        sub_uuid=entry["sub_uuid"],
                        topic_pattern=entry["topic"],
                        hosts=entry.get("hosts"),
                        blocked_hosts=entry.get("blocked_hosts"),
                        authors=entry.get("authors"),
                        blocked_authors=entry.get("blocked_authors"),
                    )
                    self._inbound_adverts[author_host][sub.sub_uuid] = sub
                    self._inbound_global_order[(author_host, sub.sub_uuid)] = sub

            self._logger.debug(
                "[SUB_ADVERTISE] recorded %d subs from %s",
                len(subs_payload), author_host,
            )

            # Reciprocal: if we haven't yet advertised to this peer,
            # send our snapshot back (locked #7).
            await self._maybe_reciprocal_exchange(author_host, writer)

        except Exception:
            self._logger.exception("[SUB_ADVERTISE] handler crashed")

    async def _handle_sub_delta(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data: dict,
        conn_context: Dict[str, Any],
    ) -> None:
        """MSG_SUB_DELTA: single add or remove (locked #1)."""
        try:
            payload_dict = data if isinstance(data, dict) else {}
            author_host = payload_dict.get("author_host")

            if self._self_impersonation_check(
                author_host, writer, "MSG_SUB_DELTA"
            ):
                return

            if not isinstance(author_host, str) or not author_host:
                self._logger.warning(
                    "[SUB_DELTA] invalid author_host=%r", author_host
                )
                return

            conn_context.setdefault("peer_hostname", author_host)

            kind = payload_dict.get("kind")
            if kind not in ("add", "remove"):
                self._logger.warning(
                    "[SUB_DELTA] invalid kind=%r from %s", kind, author_host
                )
                return

            subs_payload = payload_dict.get("subscriptions")
            if not isinstance(subs_payload, list) or not subs_payload:
                self._logger.debug(
                    "[SUB_DELTA] empty subscriptions from %s", author_host
                )
                return
            # Cycle-2 B-F6 fix: handler processes only subs_payload[0]
            # below (sender always sends 1-entry deltas). Warn if a peer
            # sends a multi-entry delta so the silent truncation surfaces.
            if len(subs_payload) > 1:
                self._logger.warning(
                    "[SUB_DELTA] received multi-entry delta from %s "
                    "(len=%d); only the first entry is processed — "
                    "remaining %d entries dropped",
                    author_host, len(subs_payload), len(subs_payload) - 1,
                )

            # Soft anti-spoof check (same as advertise).
            peer_ip = self._safe_peer_ip(writer)
            if peer_ip:
                node_for_ip = next(
                    (n for n in list(self.nodes) if n.IP == peer_ip),
                    None,
                )
                if (
                    node_for_ip is not None
                    and node_for_ip.hostname
                    and node_for_ip.hostname != author_host
                ):
                    self._logger.warning(
                        "[SUB_DELTA] anti-spoof: TCP peer IP %s known as %s but "
                        "wire claimed author_host=%s — accepting (locked #1)",
                        peer_ip, node_for_ip.hostname, author_host,
                    )

            entry = subs_payload[0]
            async with self._adverts_struct_lock:
                if kind == "add":
                    if not self._filter_inbound_advert(entry):
                        self._logger.warning(
                            "[SUB_DELTA] add: malformed entry from %s", author_host
                        )
                        return
                    sub = AdvertSub(
                        sub_uuid=entry["sub_uuid"],
                        topic_pattern=entry["topic"],
                        hosts=entry.get("hosts"),
                        blocked_hosts=entry.get("blocked_hosts"),
                        authors=entry.get("authors"),
                        blocked_authors=entry.get("blocked_authors"),
                    )
                    self._inbound_adverts.setdefault(author_host, {})[sub.sub_uuid] = sub
                    self._inbound_global_order[(author_host, sub.sub_uuid)] = sub
                else:
                    sub_uuid = entry.get("sub_uuid") if isinstance(entry, dict) else None
                    if not isinstance(sub_uuid, str) or not sub_uuid:
                        self._logger.warning(
                            "[SUB_DELTA] remove: missing sub_uuid from %s",
                            author_host,
                        )
                        return
                    per_peer = self._inbound_adverts.get(author_host)
                    if per_peer is None or sub_uuid not in per_peer:
                        # Idempotent skip — fall through to reciprocal-exchange
                        # check (locked #7); the early-return in Cycle 6 review
                        # was inconsistent with sister handlers.
                        pass
                    else:
                        per_peer.pop(sub_uuid, None)
                        self._inbound_global_order.pop((author_host, sub_uuid), None)

            await self._maybe_reciprocal_exchange(author_host, writer)

        except Exception:
            self._logger.exception("[SUB_DELTA] handler crashed")

    # ── PR3 Stage C client methods (publish/request/advertise/delta) ──

    async def publish_event_remote(
        self,
        IP: str,
        topic: str,
        payload: Any,
        author: str,
        author_id: str,
        author_host: str,
        timestamp: float,
        request_uuid: str,
    ) -> None:
        """Fire-and-forget MSG_PUBLISH_EVENT to a remote peer. NO response
        read — server doesn't send one."""
        reader = None
        writer = None
        send_ok = False
        try:
            reader, writer = await self._get_connection(IP)
            request_data = {
                "topic": topic,
                "payload": payload,
                "author": author,
                "author_id": author_id,
                "author_host": author_host,
                "timestamp": timestamp,
                "request_uuid": request_uuid,
            }
            await self._send_message(writer, MSG_PUBLISH_EVENT, request_data)
            send_ok = True
        except Exception:
            # Send failure (broken pipe, peer reset, etc.) — do NOT return
            # a possibly-broken writer to the pool. Mirror the exception
            # cleanup convention used by request_event_remote /
            # request_event_stream_remote and Stage A's execute_remote.
            self._logger.debug(
                "[PUBLISH_EVENT_REMOTE] failed to send to %s", IP, exc_info=True
            )
        finally:
            if reader and writer:
                if send_ok:
                    try:
                        await self._return_connection(IP, reader, writer)
                    except Exception:
                        try:
                            writer.close()
                            await writer.wait_closed()
                        except Exception:
                            pass
                else:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

    async def request_event_remote(
        self,
        IP: str,
        topic: str,
        payload: Any,
        author: str,
        author_id: str,
        author_host: str,
        timestamp: float,
        request_uuid: str,
        timeout: Optional[float] = None,
    ) -> Any:
        """Request-by-event on a remote peer; returns chunked result.

        Decoder uses ``_receive_message`` (locked #13: pickled exception
        INSTANCE on MSG_ERROR; defensive bare-string fallback retained).
        """
        reader = None
        writer = None
        connection_returned = False
        try:
            reader, writer = await self._get_connection(IP)
            request_data = {
                "topic": topic,
                "payload": payload,
                "author": author,
                "author_id": author_id,
                "author_host": author_host,
                "timestamp": timestamp,
                "request_uuid": request_uuid,
                "timeout": timeout,
            }
            await self._send_message(writer, MSG_REQUEST_EVENT, request_data)

            # Server now ships result as a single MSG_STREAM_CHUNK (already
            # unpickled by _receive_message) terminated by MSG_END_STREAM.
            # MSG_STREAM_ITEM_END is defensive no-op: should not appear on
            # this path, but a pooled connection may carry a stray frame
            # from a prior streaming request — ignore rather than crash.
            result: Any = None
            have_result = False
            while True:
                try:
                    msg_type, chunk = await self._receive_message(reader)
                except (TimeoutError, ConnectionError) as e:
                    raise NetworkRequestException(str(e))
                if msg_type == MSG_STREAM_CHUNK:
                    result = chunk
                    have_result = True
                    continue
                if msg_type == MSG_STREAM_ITEM_END:
                    # Defensive: not emitted by _handle_request_event after
                    # the framing fix, but tolerate it for pooled-connection
                    # robustness (PLAN b15).
                    continue
                if msg_type == MSG_END_STREAM:
                    break
                if msg_type == MSG_ERROR:
                    decoded = chunk
                    if not isinstance(decoded, BaseException):
                        decoded = NetworkRequestException(
                            str(decoded) if decoded is not None else ""
                        )
                    raise decoded
                raise NetworkRequestException(
                    f"Unexpected message type: {msg_type}"
                )

            await self._return_connection(IP, reader, writer)
            connection_returned = True

            if not have_result:
                return None
            return result
        except RequestException:
            raise
        except Exception as e:
            self._logger.debug(
                "[REQUEST_EVENT_REMOTE] error from %s: %s", IP, e
            )
            raise NetworkRequestException(str(e))
        finally:
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    async def request_event_stream_remote(
        self,
        IP: str,
        topic: str,
        payload: Any,
        author: str,
        author_id: str,
        author_host: str,
        timestamp: float,
        request_uuid: str,
        timeout: Optional[float] = None,
    ):
        """Streaming request-by-event on a remote peer; yields chunks.

        Framing invariant: server emits MSG_STREAM_CHUNK + MSG_STREAM_ITEM_END
        per yield, terminated by MSG_END_STREAM.
        """
        reader = None
        writer = None
        connection_returned = False
        try:
            reader, writer = await self._get_connection(IP)
            request_data = {
                "topic": topic,
                "payload": payload,
                "author": author,
                "author_id": author_id,
                "author_host": author_host,
                "timestamp": timestamp,
                "request_uuid": request_uuid,
                "timeout": timeout,
            }
            await self._send_message(writer, MSG_REQUEST_EVENT_STREAM, request_data)

            pending = None
            have_pending = False
            while True:
                try:
                    msg_type, chunk = await self._receive_message(reader)
                except (TimeoutError, ConnectionError) as e:
                    raise NetworkRequestException(str(e))
                if msg_type == MSG_STREAM_CHUNK:
                    pending = chunk
                    have_pending = True
                    continue
                if msg_type == MSG_STREAM_ITEM_END:
                    if have_pending:
                        yield pending
                        pending = None
                        have_pending = False
                    continue
                if msg_type == MSG_END_STREAM:
                    break
                if msg_type == MSG_ERROR:
                    decoded = chunk
                    if not isinstance(decoded, BaseException):
                        decoded = NetworkRequestException(
                            str(decoded) if decoded is not None else ""
                        )
                    raise decoded
                raise NetworkRequestException(
                    f"Unexpected message type: {msg_type}"
                )

            await self._return_connection(IP, reader, writer)
            connection_returned = True
        except RequestException:
            raise
        except Exception as e:
            self._logger.debug(
                "[REQUEST_EVENT_STREAM_REMOTE] error from %s: %s", IP, e
            )
            raise NetworkRequestException(str(e))
        finally:
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    async def advertise_subs_remote(self, peer_ip: str, peer_hostname: str) -> None:
        """Send full snapshot to a peer (Tree 2 step 13 — content built
        INSIDE per-peer lock so snapshot vs delta serialise per locked #9).

        Lock order (locked #9): list_local_subs() acquires
        topic_registry._lock; the subscribe/unsubscribe broadcast hooks
        in PluginCore acquire topic_registry._lock first then reach
        _advert_locks[peer] via send_sub_delta_remote. To avoid a cycle,
        snapshot the local subs list BEFORE acquiring _advert_locks[peer].
        """
        try:
            subs = await self.plugin_core.topic_registry.list_local_subs()
        except Exception:
            self._logger.exception(
                "[ADVERTISE] list_local_subs failed for peer %s", peer_hostname
            )
            return

        lock = self._advert_locks.setdefault(peer_hostname, asyncio.Lock())
        async with lock:
            async with self._adverts_struct_lock:
                filtered = [
                    s for s in subs
                    if self._should_advertise_sub_to_peer(s, peer_hostname)
                ]
                projected = {
                    s.sub_uuid: AdvertSub(
                        sub_uuid=s.sub_uuid,
                        topic_pattern=s.topic_pattern,
                        hosts=s.hosts,
                        blocked_hosts=s.blocked_hosts,
                        authors=s.authors,
                        blocked_authors=s.blocked_authors,
                    )
                    for s in filtered
                }
                self._outbound_adverts[peer_hostname] = projected

            wire_payload = {
                "author_host": self.plugin_core.hostname,
                "kind": "snapshot",
                "subscriptions": [
                    self._serialize_local_sub_for_peer(s) for s in filtered
                ],
            }

            reader = None
            writer = None
            send_ok = False
            try:
                reader, writer = await self._get_connection(peer_ip)
                await self._send_message(writer, MSG_SUB_ADVERTISE, wire_payload)
                send_ok = True
            except Exception:
                self._logger.debug(
                    "[ADVERTISE] failed to send snapshot to %s", peer_hostname,
                    exc_info=True,
                )
                # Wipe outbound on send-failure so a retry actually
                # rebuilds + resends.
                async with self._adverts_struct_lock:
                    self._outbound_adverts.pop(peer_hostname, None)
                raise
            finally:
                # Cycle-4 fix: only pool on confirmed send success. Mirrors
                # send_sub_delta_remote pattern. Without send_ok, a partial
                # MSG_SUB_ADVERTISE would pool a framing-corrupt writer that
                # the next caller's PING health-check then has to evict.
                if reader and writer:
                    if send_ok:
                        try:
                            await self._return_connection(peer_ip, reader, writer)
                        except Exception:
                            try:
                                writer.close()
                                await writer.wait_closed()
                            except Exception:
                                pass
                    else:
                        try:
                            writer.close()
                            await writer.wait_closed()
                        except Exception:
                            pass

    async def send_sub_delta_remote(
        self,
        peer_ip: str,
        peer_hostname: str,
        kind: str,
        sub,
    ) -> None:
        """Send single delta (add/remove) to a peer. Same lock-order
        pattern as advertise_subs_remote."""
        if kind not in ("add", "remove"):
            self._logger.warning(
                "[DELTA] invalid kind=%r for peer %s", kind, peer_hostname
            )
            return
        lock = self._advert_locks.setdefault(peer_hostname, asyncio.Lock())
        async with lock:
            async with self._adverts_struct_lock:
                outbound_for_peer = self._outbound_adverts.get(peer_hostname, {})
                if kind == "add":
                    if sub.sub_uuid in outbound_for_peer:
                        return  # already advertised
                    outbound_for_peer = self._outbound_adverts.setdefault(
                        peer_hostname, {}
                    )
                    outbound_for_peer[sub.sub_uuid] = AdvertSub(
                        sub_uuid=sub.sub_uuid,
                        topic_pattern=sub.topic_pattern,
                        hosts=sub.hosts,
                        blocked_hosts=sub.blocked_hosts,
                        authors=sub.authors,
                        blocked_authors=sub.blocked_authors,
                    )
                else:
                    if sub.sub_uuid not in outbound_for_peer:
                        return  # never advertised
                    del self._outbound_adverts[peer_hostname][sub.sub_uuid]

            if kind == "add":
                wire_subs = [self._serialize_local_sub_for_peer(sub)]
            else:
                wire_subs = [{"sub_uuid": sub.sub_uuid}]

            wire_payload = {
                "author_host": self.plugin_core.hostname,
                "kind": kind,
                "subscriptions": wire_subs,
            }

            reader = None
            writer = None
            send_ok = False
            try:
                reader, writer = await self._get_connection(peer_ip)
                await self._send_message(writer, MSG_SUB_DELTA, wire_payload)
                send_ok = True
            except Exception:
                self._logger.debug(
                    "[DELTA] failed to send %s to %s", kind, peer_hostname,
                    exc_info=True,
                )
                # Roll back outbound bookkeeping so retry / future-snapshot
                # path can re-emit. Without this, peer state is recorded as
                # "already advertised" / "already removed" even though no
                # bytes hit the wire — silent staleness until reconnect.
                async with self._adverts_struct_lock:
                    outbound_now = self._outbound_adverts.get(peer_hostname)
                    if outbound_now is not None:
                        if kind == "add":
                            outbound_now.pop(sub.sub_uuid, None)
                        # For "remove" rollback we'd need the prior AdvertSub —
                        # not preserved before delete. Acceptable: peer either
                        # already had remove applied (idempotent) or our state
                        # diverged briefly until next snapshot/reconnect.
            finally:
                if reader and writer:
                    if send_ok:
                        try:
                            await self._return_connection(peer_ip, reader, writer)
                        except Exception:
                            try:
                                writer.close()
                                await writer.wait_closed()
                            except Exception:
                                pass
                    else:
                        # Send failed — never return broken writer to pool.
                        try:
                            writer.close()
                            await writer.wait_closed()
                        except Exception:
                            pass

    # ── Sub-broadcast helpers (called from PluginCore subscribe/unsubscribe) ──

    async def broadcast_local_sub_added(self, sub) -> None:
        """Filter peers + send add-delta. No-op when not ready."""
        if not getattr(self, "is_ready", False):
            return
        for node in list(self.nodes):
            if node.hostname is None:
                continue
            if node.hostname == self.plugin_core.hostname:
                continue
            try:
                if not (node.enabled and await node.is_alive()):
                    continue
            except Exception:
                continue
            if not self._should_advertise_sub_to_peer(sub, node.hostname):
                continue
            try:
                await self.send_sub_delta_remote(
                    node.IP, node.hostname, "add", sub
                )
            except Exception:
                self._logger.debug(
                    "broadcast_local_sub_added: send to %s failed",
                    node.hostname, exc_info=True,
                )

    async def broadcast_local_sub_removed(self, sub) -> None:
        """Filter peers + send remove-delta. Only sends to peers we have
        actually advertised this sub to (outbound table is authority)."""
        if not getattr(self, "is_ready", False):
            return
        for node in list(self.nodes):
            if node.hostname is None:
                continue
            if node.hostname == self.plugin_core.hostname:
                continue
            try:
                if not (node.enabled and await node.is_alive()):
                    continue
            except Exception:
                continue
            # Authority: only re-advertise removes for subs we sent.
            outbound = self._outbound_adverts.get(node.hostname, {})
            if sub.sub_uuid not in outbound:
                continue
            try:
                await self.send_sub_delta_remote(
                    node.IP, node.hostname, "remove", sub
                )
            except Exception:
                self._logger.debug(
                    "broadcast_local_sub_removed: send to %s failed",
                    node.hostname, exc_info=True,
                )

    # ── Initial-exchange + disconnect cleanup helpers ──

    async def _spawn_initial_exchange(self, node) -> None:
        """Schedule (or skip) initial advert exchange to a Node. Idempotent
        via in-flight task table + ``_snapshot_sent`` guard inside the
        inner task body (locked #7)."""
        host = getattr(node, "hostname", None)
        if not host:
            return
        async with self._adverts_struct_lock:
            prev = self._initial_exchange_tasks.get(host)
            if prev is not None and not prev.done():
                return
            inner = asyncio.create_task(self._initial_advert_exchange(node))
            self._initial_exchange_tasks[host] = inner

        def _deregister(_t, h=host):
            async def _drop():
                async with self._adverts_struct_lock:
                    cur = self._initial_exchange_tasks.get(h)
                    if cur is _t:
                        self._initial_exchange_tasks.pop(h, None)
            try:
                asyncio.create_task(_drop())
            except RuntimeError:
                # Loop closed during shutdown — drop silently.
                pass

        inner.add_done_callback(_deregister)

    async def _initial_advert_exchange(self, node) -> None:
        """Authoritative check-then-set under struct_lock. If a second
        concurrent trigger arrived, bail."""
        host = getattr(node, "hostname", None)
        if not host:
            return
        async with self._adverts_struct_lock:
            if host in self._snapshot_sent:
                return
            self._snapshot_sent.add(host)
        try:
            await self.advertise_subs_remote(node.IP, host)
        except Exception:
            async with self._adverts_struct_lock:
                self._snapshot_sent.discard(host)
            self._logger.warning(
                "initial advert exchange to %s failed", host
            )
            raise

    async def _spawn_initial_exchange_for_ip(
        self, peer_ip: str, host: str
    ) -> None:
        """Variant for client-only peers without a Node entry yet."""
        if not host:
            return
        async with self._adverts_struct_lock:
            prev = self._initial_exchange_tasks.get(host)
            if prev is not None and not prev.done():
                return

            async def _exchange():
                async with self._adverts_struct_lock:
                    if host in self._snapshot_sent:
                        return
                    self._snapshot_sent.add(host)
                try:
                    await self.advertise_subs_remote(peer_ip, host)
                except Exception:
                    async with self._adverts_struct_lock:
                        self._snapshot_sent.discard(host)
                    raise

            t = asyncio.create_task(_exchange())
            self._initial_exchange_tasks[host] = t

        def _dereg(_t, h=host):
            async def _drop():
                async with self._adverts_struct_lock:
                    cur = self._initial_exchange_tasks.get(h)
                    if cur is _t:
                        self._initial_exchange_tasks.pop(h, None)
            try:
                asyncio.create_task(_drop())
            except RuntimeError:
                pass

        t.add_done_callback(_dereg)

    async def _drop_peer_advert_state(self, peer_hostname: str) -> None:
        """Clear all advert state for a peer + cancel in-flight tasks
        targeting it (locked #4 + #17)."""
        if not peer_hostname:
            return
        async with self._adverts_struct_lock:
            self._inbound_adverts.pop(peer_hostname, None)
            self._inbound_global_order = {
                k: v
                for k, v in self._inbound_global_order.items()
                if k[0] != peer_hostname
            }
            self._outbound_adverts.pop(peer_hostname, None)
            self._snapshot_sent.discard(peer_hostname)
            tasks = self._inflight_publishes.pop(peer_hostname, set())
            ex_task = self._initial_exchange_tasks.pop(peer_hostname, None)

        for t in tasks:
            try:
                t.cancel()
            except Exception:
                pass
        if tasks:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except Exception:
                pass
        if ex_task is not None and not ex_task.done():
            try:
                ex_task.cancel()
                try:
                    await asyncio.wait_for(ex_task, timeout=1.0)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
            except Exception:
                pass

        # Pop the per-peer Lock entry. Orphaning a held lock is harmless
        # — GC'd when its task completes.
        self._advert_locks.pop(peer_hostname, None)

    async def _mark_node_dead(self, node) -> None:
        """Centralised node-dead helper. Idempotent."""
        if not node.enabled:
            return
        node.enabled = False
        host = getattr(node, "hostname", None)
        if host:
            await self._drop_peer_advert_state(host)

    # ── Remote-dispatch helper for publish_event step 18 ──

    async def _build_remote_dispatch(
        self,
        topic: str,
        payload: Any,
        author: str,
        author_id: str,
        author_host: str,
        timestamp: float,
        request_uuid: str,
        eff_hosts: Union[str, list, None],
        eff_blocked_hosts: Union[str, list, None],
    ) -> Dict[str, List[AdvertSub]]:
        """Build per-peer list of advertised subs that match the publish
        event after applying peer-level + sub-level filters. Returns dict
        keyed by peer_hostname → list of surviving AdvertSub instances.
        """
        from notifier import TopicRegistry as _TR  # local import: cycle

        out: Dict[str, List[AdvertSub]] = {}
        async with self._adverts_struct_lock:
            per_peer_snap = {
                host: list(adverts.values())
                for host, adverts in self._inbound_adverts.items()
            }

        for node in list(self.nodes):
            if node.hostname == self.plugin_core.hostname:
                continue
            # Cycle-3 fresh-F3 fix: skip nodes whose hostname hasn't been
            # discovered yet (newly-created Node before first INFO/discovery
            # response). Without this guard, _hosts_match(..., None) returns
            # False silently, so publish events never reach the peer until
            # discovery completes. Make the skip explicit and traceable.
            if node.hostname is None:
                self._logger.debug(
                    "[REMOTE_DISPATCH] skipping node %s — hostname not yet "
                    "discovered",
                    node.IP,
                )
                continue
            try:
                if not (node.enabled and await node.is_alive()):
                    continue
            except Exception:
                continue
            if not self._hosts_match(eff_hosts, eff_blocked_hosts, node.hostname):
                continue
            peer_subs = per_peer_snap.get(node.hostname, [])
            surviving: List[AdvertSub] = []
            for advert in peer_subs:
                if not _TR._topic_matches(advert.topic_pattern, topic):
                    continue
                if not self.plugin_core._sub_accepts_remote_publisher(
                    advert, self.plugin_core.hostname, author
                ):
                    continue
                if not self.plugin_core._sub_accepts_author(advert, author):
                    continue
                surviving.append(advert)
            if surviving:
                out[node.hostname] = surviving
        return out

    async def _handle_ping(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data: dict,
        conn_context: Optional[Dict[str, Any]] = None,
    ):
        """Handle PING message."""
        conn_context = conn_context or {}
        try:
            await self._send_message(writer, MSG_RESULT, {"status": "ok"})
        except Exception as e:
            self._logger.exception("Exception in _handle_ping")
            await self._send_error(writer, str(e))

    async def _handle_info(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data: dict,
        conn_context: Optional[Dict[str, Any]] = None,
    ):
        """Handle INFO message."""
        conn_context = conn_context or {}
        try:
            hostname = data.get("hostname")
            discover_nodes_info = data.get("discover_nodes_info")
            # Sender's listener port (tells us where to call them back).
            # Optional for forward-compat with senders that don't include it.
            client_listener_port = data.get("listener_port")

            self._logger.debug(
                f"[INFO] Received INFO request: hostname={hostname}, "
                f"discover_nodes_info={discover_nodes_info}, "
                f"listener_port={client_listener_port}"
            )

            if not isinstance(hostname, str):
                await self._send_error(writer, "hostname must be str")
                return

            if not isinstance(discover_nodes_info, bool):
                await self._send_error(writer, "discover_nodes_info must be bool")
                return

            if not self.direct_discoverable:
                await self._send_error(writer, "Host is not discoverable (418)")
                return

            client_addr = writer.get_extra_info("peername")
            if client_addr:
                client_ip = client_addr[0]
                # Use the sender-provided listener_port if valid; fall back to
                # None (→ resolves to cluster default). Without listener_port
                # we'd record (client_ip, None), and on same-machine setups
                # _resolve_port returns OUR self.port → connect-back hits our
                # own server (self-loop).
                resolved_port: Optional[int]
                # Cycle-4 fix: cap upper bound on wire-supplied
                # listener_port. Previously only `> 0` was checked; an
                # authenticated peer could advertise listener_port=70000
                # which gets stored on Node.port, then asyncio.open_connection
                # raises ValueError("port out of range 0-65535") forever
                # afterward — the peer is permanently DoS'd in our routing.
                if (
                    isinstance(client_listener_port, int)
                    and 0 < client_listener_port <= 65535
                ):
                    resolved_port = client_listener_port
                else:
                    resolved_port = None
                client_entry = (client_ip, resolved_port)
                if self.discover_nodes:
                    # Dedupe: drop existing (client_ip, None) when we now
                    # have an explicit port; otherwise dedupe by exact tuple.
                    if resolved_port is not None:
                        self.node_ips = [
                            e
                            for e in self.node_ips
                            if not (e[0] == client_ip and e[1] is None)
                        ]
                        # Also patch any existing Node for this IP that was
                        # created before we knew its listener port. Without
                        # this, _resolve_port walks self.nodes first and
                        # finds the stale port=None Node — masking the
                        # corrected node_ips entry forever.
                        for n in self.nodes:
                            if n.IP == client_ip and n.port is None:
                                n.port = resolved_port
                                if n.hostname is None and hostname:
                                    n.hostname = hostname
                    if client_entry not in self.node_ips:
                        self.node_ips.append(client_entry)

            response = {
                "hostname": self.plugin_core.hostname,
                "auto_discoverable": self.auto_discoverable,
                "nodes": [
                    await node._to_tuple()
                    for node in self.nodes
                    if node.auto_discoverable
                    and not node.hostname == hostname
                    and discover_nodes_info
                    and node.enabled
                    and await node.is_alive()
                ],
            }

            self._logger.debug(
                f"[INFO] Returning nodes info: count={len(response['nodes'])}, hostname={response['hostname']}"
            )
            await self._send_message(writer, MSG_RESULT, response)

        except Exception as e:
            self._logger.exception("Exception in _handle_info")
            await self._send_error(writer, str(e))

    # Client-side Connection Pool Management

    async def _create_connection(
        self, IP: str
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Create a new TLS connection to a node, then verify the peer's
        SPKI fingerprint as the FIRST act after the handshake (B-066).

        Resolves the per-node port via _resolve_port(IP). Connections to two
        peers on the same IP with different ports are tracked separately.
        """
        port = self._resolve_port(IP)
        self._logger.debug(f"[CONNECTION] Creating new TLS connection to {IP}:{port}")
        ssl_context = self._create_client_ssl_context()

        try:
            reader, writer = await asyncio.open_connection(
                IP,
                port,
                ssl=ssl_context,
            )
            self._logger.debug(
                f"[CONNECTION] TLS connection established to {IP}:{port}"
            )
        except Exception as e:
            self._logger.warning(
                f"[CONNECTION] Failed to establish connection to {IP}:{port}: {e}"
            )
            raise

        # === FIRST ACT — pin check, before any _send_message / _receive_message ===
        try:
            peer_fp = self._extract_peer_fingerprint(writer)
        except Exception as e:
            writer.close()
            try: await writer.wait_closed()
            except Exception: pass
            raise ConnectionError(f"Server pin extract failed for {IP}:{port}: {e}")

        peer_cfg = self.peers_by_endpoint.get((IP, port))
        if peer_cfg is None or peer_cfg.fingerprint != peer_fp:
            writer.close()
            try: await writer.wait_closed()
            except Exception: pass
            raise ConnectionError(
                f"Server fingerprint {peer_fp} for {IP}:{port} not in peers config"
            )

        self._logger.debug(
            f"[CONNECTION] Pinned connection established to {IP}:{port} "
            f"hostname={peer_cfg.hostname} fp={peer_fp}"
        )
        return reader, writer

    async def revoke_peer(self, fingerprint: str) -> int:
        """Force-close all pooled connections to a peer with the given
        fingerprint. Removes the peer from peers_by_fingerprint /
        peers_by_endpoint / self.peers BEFORE draining the pool so any
        concurrent _create_connection mid-await fails its own pin check
        rather than completing and pooling a now-revoked connection.
        Returns count of connections closed.
        """
        closed = 0
        spec = next((p for p in self.peers if p.fingerprint == fingerprint), None)
        if spec is None:
            return 0
        self.peers_by_endpoint.pop((spec.ip, spec.port), None)
        self.peers_by_fingerprint.pop(spec.fingerprint, None)
        self.peers = [p for p in self.peers if p.fingerprint != fingerprint]
        # Security review LOW fix: warn loudly when revoke leaves zero peers.
        # Subsequent _create_pinned_ssl_context calls will skip
        # load_verify_locations and outgoing connections fail with an
        # opaque OpenSSL "certificate verify failed" message.
        if not self.peers:
            self._logger.warning(
                "[NETWORKING] revoke_peer left peers list empty. All future "
                "outgoing connections will fail with an opaque OpenSSL error "
                "until a new peer is added (currently restart-required)."
            )
        pool_key = (spec.ip, spec.port)
        pool = self.connection_pools.pop(pool_key, None)
        if pool is None:
            return 0
        while True:
            try:
                _reader, writer = pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            except Exception as e:
                self._logger.warning("revoke_peer drain unexpected error: %s", e)
                break
            try:
                writer.close()
                await writer.wait_closed()
                closed += 1
            except Exception as e:
                self._logger.warning(
                    "revoke_peer close error for %s:%s: %s — continuing drain",
                    spec.ip, spec.port, e,
                )
        return closed

    async def _get_connection(
        self, IP: str
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Get a connection from pool or create new one.

        Pool is keyed by (IP, port) so two peers on the same IP but different
        ports do not share the same connection slots.
        """
        key = self._pool_key(IP)
        if key not in self.connection_pools:
            self.connection_pools[key] = asyncio.Queue(maxsize=self.pool_size)
            self._logger.debug(
                f"[CONNECTION] Created new connection pool for {key[0]}:{key[1]}"
            )

        pool = self.connection_pools[key]
        pool_size = pool.qsize()
        self._logger.debug(
            f"[CONNECTION] Pool for {IP}: size={pool_size}/{self.pool_size}, "
            f"empty={pool.empty()}"
        )

        # Try to get from pool
        if not pool.empty():
            try:
                self._logger.debug(
                    f"[CONNECTION] Attempting to get connection from pool for {IP}"
                )
                reader, writer = await asyncio.wait_for(pool.get(), timeout=0.1)
                self._logger.debug(
                    f"[CONNECTION] Retrieved connection from pool for {IP}, performing health check"
                )
                # Health check - try a ping
                try:
                    await self._send_message(writer, MSG_PING, {})
                    msg_type, _ = await asyncio.wait_for(
                        self._receive_message(reader), timeout=2.0
                    )
                    if msg_type == MSG_RESULT:
                        self._logger.debug(
                            f"[CONNECTION] Pooled connection to {IP} is healthy"
                        )
                        return reader, writer
                    else:
                        # Connection is bad, close it
                        self._logger.warning(
                            f"[CONNECTION] Pooled connection to {IP} failed health check "
                            f"(msg_type={msg_type}), closing"
                        )
                        writer.close()
                        await writer.wait_closed()
                except Exception as e:
                    # Connection is bad, close it and create new
                    self._logger.warning(
                        f"[CONNECTION] Pooled connection to {IP} failed health check: {e}, closing"
                    )
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                self._logger.debug(
                    f"[CONNECTION] Timeout getting connection from pool for {IP}"
                )
                pass

        # Create new connection
        self._logger.debug(
            f"[CONNECTION] Creating new connection to {IP} (pool empty or health check failed)"
        )
        return await self._create_connection(IP)

    async def _return_connection(
        self, IP: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Return a connection to the pool (keyed by (IP, port))."""
        key = self._pool_key(IP)
        if key not in self.connection_pools:
            self.connection_pools[key] = asyncio.Queue(maxsize=self.pool_size)

        pool = self.connection_pools[key]
        pool_size_before = pool.qsize()

        try:
            pool.put_nowait((reader, writer))
            self._logger.debug(
                f"[CONNECTION] Returned connection to pool for {IP}: "
                f"pool_size={pool_size_before} -> {pool.qsize()}"
            )
        except asyncio.QueueFull:
            # Pool is full, close connection
            self._logger.debug(
                f"[CONNECTION] Pool for {IP} is full ({pool.qsize()}/{self.pool_size}), "
                f"closing connection"
            )
            writer.close()
            await writer.wait_closed()

    # Client-side Remote Execution Methods

    @async_handle_errors(None)
    async def execute_remote(
        self,
        IP: str,
        plugin: str,
        method: str,
        args=None,
        plugin_uuid="",
        author="remote",
        author_id="remote",
        timeout: tuple = None,
        author_host: str = None,
        request_id: str = None,
    ):
        """Execute a plugin method on a remote node."""
        reader = None
        writer = None
        connection_returned = False

        try:
            self._logger.info(
                f"[REMOTE] execute_remote: ip={IP}, plugin={plugin}, method={method}, "
                f"plugin_uuid={plugin_uuid}, author_id={author_id}, request_id={request_id}"
            )
            self._logger.debug(f"[REMOTE] Getting connection for {IP}")
            reader, writer = await self._get_connection(IP)
            self._logger.debug(f"[REMOTE] Connection acquired for {IP}")

            # Prepare request
            request_data = {
                "plugin": plugin,
                "method": method,
                "args": args or [],
                "plugin_uuid": plugin_uuid,
                "author": author,
                "author_id": author_id,
                "timeout": timeout,
                "author_host": author_host or self.plugin_core.hostname,
                "request_id": request_id,
            }

            # Send execute request
            self._logger.debug(
                f"[REMOTE] Sending EXECUTE to {IP}: plugin={plugin}, method={method}, "
                f"args_type={type(request_data['args']).__name__}"
            )
            await self._send_message(writer, MSG_EXECUTE, request_data)

            # Receive response (may be chunked if large)
            result_chunks_bytes = []
            total_bytes = 0
            chunks = 0
            while True:
                # Read raw message to get pickled bytes (don't unpickle yet for chunks)
                length_bytes = await reader.readexactly(4)
                msg_length = struct.unpack(">I", length_bytes)[0]

                if msg_length > MAX_MESSAGE_SIZE:
                    raise NetworkRequestException(
                        f"Message length {msg_length} exceeds maximum {MAX_MESSAGE_SIZE}"
                    )

                msg_type_byte = await reader.readexactly(1)
                msg_type = msg_type_byte[0]

                payload_length = msg_length - 1
                if payload_length > 0:
                    payload = await reader.readexactly(payload_length)

                    if msg_type == MSG_STREAM_CHUNK:
                        # Collect raw pickled bytes
                        result_chunks_bytes.append(payload)
                        chunks += 1
                        total_bytes += len(payload)
                        if chunks % 10 == 0:
                            self._logger.debug(
                                f"[REMOTE] Receiving chunks from {IP}: count={chunks}, total_bytes={total_bytes}"
                            )
                    elif msg_type == MSG_END_STREAM:
                        self._logger.debug(
                            f"[REMOTE] Received END_STREAM from {IP}: chunks={chunks}, total_bytes={total_bytes}"
                        )
                        break
                    elif msg_type == MSG_ERROR:
                        # K-6 (B-066): wrap UnpicklingError so plugin
                        # exceptions that didn't inherit Serializable
                        # surface as a clean NetworkRequestException
                        # rather than a confusing "Disallowed class".
                        try:
                            error_data = safe_loads(payload)
                        except pickle.UnpicklingError as _e:
                            raise NetworkRequestException(
                                f"Remote node {IP} sent an exception class this node "
                                f"does not recognize: {_e}. Plugin authors: make custom "
                                f"exceptions inherit from serialization.SerializableException."
                            )
                        if (
                            isinstance(error_data, tuple)
                            and len(error_data) == 2
                            and error_data[0] == "__STREAM_ERROR__"
                        ):
                            self._logger.warning(
                                f"[REMOTE] EXECUTE error from {IP}: {error_data[1]}"
                            )
                            raise NetworkRequestException(error_data[1])
                        self._logger.warning(
                            f"[REMOTE] EXECUTE error from {IP}: {str(error_data)}"
                        )
                        raise NetworkRequestException(str(error_data))
                    else:
                        raise NetworkRequestException(
                            f"Unexpected message type: {msg_type}"
                        )
                elif msg_type == MSG_END_STREAM:
                    self._logger.debug(
                        f"[REMOTE] Received END_STREAM (no payload) from {IP}: chunks={chunks}, total_bytes={total_bytes}"
                    )
                    break
                else:
                    raise NetworkRequestException(
                        f"Unexpected message type: {msg_type}"
                    )

            # Reconstruct and unpickle result from chunks
            if result_chunks_bytes:
                # Concatenate all pickled chunks and unpickle
                full_pickled = b"".join(result_chunks_bytes)
                result = safe_loads(full_pickled)
                try:
                    result_type = type(result).__name__
                except Exception:
                    result_type = "unknown"
                self._logger.info(
                    f"[REMOTE] EXECUTE complete from {IP}: chunks={chunks}, total_bytes={total_bytes}, result_type={result_type}"
                )
                return result
            else:
                self._logger.info(
                    f"[REMOTE] EXECUTE complete from {IP}: no result returned"
                )
                return None

        except Exception as e:
            self._logger.exception(f"Error in execute_remote to {IP}")
            # Cycle-2 B-F2 fix: on exception, close (don't return) the
            # connection. Mid-stream errors leave stale bytes in the reader
            # buffer; pooling a framing-corrupted connection forces a wasted
            # ping-then-discard cycle on the next caller. Mirror the
            # request_event_remote pattern (line 2101+).
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                connection_returned = True  # prevent finally from also touching it
            raise NetworkRequestException(f"Remote execution failed: {e}")
        finally:
            # Return connection to pool ONLY on success (success path sets
            # connection_returned=True via _return_connection). On exception
            # the except-block above closed it and set connection_returned=True
            # to prevent double-touch here.
            if reader and writer and not connection_returned:
                try:
                    self._logger.debug(f"[REMOTE] Returning connection for {IP}")
                    await self._return_connection(IP, reader, writer)
                    connection_returned = True
                except Exception:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

    @async_gen_log_errors
    async def execute_remote_stream(
        self,
        IP: str,
        plugin: str,
        method: str,
        args=None,
        plugin_uuid: str = "",
        author: str = "remote",
        author_id: str = "remote",
        timeout: tuple = None,
        author_host: str = None,
        request_id: str = None,
    ):
        """Execute a streaming plugin method on a remote node."""
        reader = None
        writer = None
        connection_returned = False

        try:
            self._logger.info(
                f"[REMOTE_STREAM] start: ip={IP}, plugin={plugin}, method={method}, "
                f"plugin_uuid={plugin_uuid}, author_id={author_id}, request_id={request_id}"
            )
            self._logger.debug(f"[REMOTE_STREAM] Getting connection for {IP}")
            reader, writer = await self._get_connection(IP)
            self._logger.debug(f"[REMOTE_STREAM] Connection acquired for {IP}")

            # Prepare request
            request_data = {
                "plugin": plugin,
                "method": method,
                "args": args or [],
                "plugin_uuid": plugin_uuid,
                "author": author,
                "author_id": author_id,
                "timeout": timeout,
                "author_host": author_host or self.plugin_core.hostname,
                "request_id": request_id,
            }

            # Send execute_stream request
            self._logger.debug(
                f"[REMOTE_STREAM] Sending EXECUTE_STREAM to {IP}: plugin={plugin}, method={method}"
            )
            await self._send_message(writer, MSG_EXECUTE_STREAM, request_data)

            # Stream results - collect chunks until we have a complete item
            current_item_chunks = []
            items_yielded = 0
            item_bytes = 0
            item_chunks = 0
            while True:
                # Read raw message
                length_bytes = await reader.readexactly(4)
                msg_length = struct.unpack(">I", length_bytes)[0]

                if msg_length > MAX_MESSAGE_SIZE:
                    raise NetworkRequestException(
                        f"Message length {msg_length} exceeds maximum {MAX_MESSAGE_SIZE}"
                    )

                msg_type_byte = await reader.readexactly(1)
                msg_type = msg_type_byte[0]

                payload_length = msg_length - 1
                if payload_length > 0:
                    payload = await reader.readexactly(payload_length)

                    if msg_type == MSG_STREAM_CHUNK:
                        current_item_chunks.append(payload)
                        item_chunks += 1
                        item_bytes += len(payload)

                    elif msg_type == MSG_STREAM_ITEM_END:
                        # Item boundary — reconstruct and yield accumulated chunks.
                        # Cycle-2 V3 fix: align both sentinel paths to raise
                        # NetworkRequestException so _process_request_stream's
                        # except handler marks the request errored and the
                        # B-044 chain propagates as RequestException to the
                        # local consumer. Previously __STREAM_ERROR__ yielded
                        # a sentinel that was put on the queue as data.
                        # NOTE: this with-payload branch is structurally
                        # unreachable today (server always sends ITEM_END
                        # with payload_length=0), but kept aligned with the
                        # empty-payload path below for defense-in-depth.
                        if current_item_chunks:
                            full_pickled = b"".join(current_item_chunks)
                            try:
                                item = safe_loads(full_pickled)
                                if isinstance(item, tuple) and len(item) == 2:
                                    if item[0] == "__STREAM_ERROR__":
                                        self._logger.exception(
                                            f"Stream error from {IP}: {item[1]}"
                                        )
                                        raise NetworkRequestException(item[1])
                                    elif item[0] == "__STREAM_EXCEPTION__":
                                        self._logger.exception(
                                            f"Stream exception from {IP}: {item[1]}"
                                        )
                                        raise NetworkRequestException(item[1])
                                items_yielded += 1
                                try:
                                    item_type = type(item).__name__
                                except Exception:
                                    item_type = "unknown"
                                self._logger.info(
                                    f"[REMOTE_STREAM] yielded item #{items_yielded} from {IP}: "
                                    f"chunks={item_chunks}, bytes={item_bytes}, type={item_type}"
                                )
                                yield item
                            except NetworkRequestException:
                                # Cycle-2 V3 fix: let our own raise propagate
                                # (don't re-catch it as a "decode error").
                                raise
                            except Exception as e:
                                self._logger.exception(
                                    f"Failed to unpickle stream item from {IP}"
                                )
                                yield ("__REMOTE_STREAM_DECODE_ERROR__", str(e))
                            current_item_chunks = []
                            item_chunks = 0
                            item_bytes = 0

                    elif msg_type == MSG_END_STREAM:
                        # End of entire stream — yield any remaining chunks as final item
                        if current_item_chunks:
                            full_pickled = b"".join(current_item_chunks)
                            try:
                                item = safe_loads(full_pickled)
                                items_yielded += 1
                                yield item
                            except Exception as e:
                                self._logger.exception(
                                    f"Failed to unpickle final stream item from {IP}"
                                )
                        break
                    elif msg_type == MSG_ERROR:
                        # K-6 (B-066): wrap UnpicklingError on the MSG_ERROR
                        # decode path so plugin exceptions that didn't inherit
                        # Serializable still surface as NetworkRequestException.
                        try:
                            error_data = safe_loads(payload)
                        except pickle.UnpicklingError as _e:
                            raise NetworkRequestException(
                                f"Remote node {IP} sent an exception class this node "
                                f"does not recognize: {_e}. Plugin authors: make custom "
                                f"exceptions inherit from serialization.SerializableException."
                            )
                        error_msg = str(error_data)
                        if isinstance(error_data, tuple) and len(error_data) == 2:
                            error_msg = error_data[1]
                        self._logger.warning(
                            f"[REMOTE_STREAM] Node {IP} returned ERROR: {error_msg}"
                        )
                        raise NetworkRequestException(error_msg)
                    else:
                        raise NetworkRequestException(
                            f"Unexpected message type: {msg_type}"
                        )

                elif msg_type == MSG_STREAM_ITEM_END:
                    # Item boundary with no payload — same handling.
                    # F5 fix: must check for __STREAM_ERROR__ /
                    # __STREAM_EXCEPTION__ sentinels in this path too —
                    # MSG_STREAM_ITEM_END is normally sent with no payload
                    # (chunk_length=1, payload_length=0), so this is the
                    # path that actually fires on every item boundary. The
                    # parallel with-payload branch above (line 3154-3194)
                    # has the same sentinel check; both paths must agree.
                    if current_item_chunks:
                        full_pickled = b"".join(current_item_chunks)
                        try:
                            item = safe_loads(full_pickled)
                            if isinstance(item, tuple) and len(item) == 2:
                                if item[0] == "__STREAM_ERROR__":
                                    # Cycle-2 V3 fix: align with __STREAM_EXCEPTION__
                                    # path — raise instead of yielding sentinel,
                                    # so _process_request_stream marks the
                                    # request errored and the local consumer
                                    # sees RequestException via B-044 chain.
                                    self._logger.exception(
                                        f"Stream error from {IP}: {item[1]}"
                                    )
                                    raise NetworkRequestException(item[1])
                                elif item[0] == "__STREAM_EXCEPTION__":
                                    self._logger.exception(
                                        f"Stream exception from {IP}: {item[1]}"
                                    )
                                    raise NetworkRequestException(item[1])
                            items_yielded += 1
                            yield item
                        except NetworkRequestException:
                            raise
                        except Exception as e:
                            self._logger.exception(
                                f"Failed to unpickle stream item from {IP}"
                            )
                            yield ("__REMOTE_STREAM_DECODE_ERROR__", str(e))
                        current_item_chunks = []
                        item_chunks = 0
                        item_bytes = 0

                elif msg_type == MSG_END_STREAM:
                    # End of stream with no payload
                    if current_item_chunks:
                        full_pickled = b"".join(current_item_chunks)
                        try:
                            item = safe_loads(full_pickled)
                            items_yielded += 1
                            yield item
                        except Exception as e:
                            self._logger.exception(
                                f"Failed to unpickle final stream item from {IP}"
                            )
                    break
                else:
                    raise NetworkRequestException(
                        f"Unexpected message type: {msg_type}"
                    )

        except NetworkRequestException:
            # F5 fix: NetworkRequestException raised from the decoder's
            # sentinel-detection path means the remote handler reported
            # a real error. Re-raise so _process_request_stream's outer
            # except (PluginCore.py) handles it (sets request error,
            # propagates as RequestException to local consumer via B-044
            # path). Previously this fell to the generic "yield sentinel"
            # branch below and the error was tunneled as data.
            #
            # Cycle-3 fix: also close the connection here. After raising
            # on a sentinel (CHUNK + ITEM_END seen), the wire still has
            # an unread MSG_END_STREAM frame. Pooling the connection
            # leaves stale bytes for the next caller; close instead.
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                connection_returned = True
            raise
        except Exception as e:
            self._logger.exception(f"Error in execute_remote_stream to {IP}")
            # Cycle-2 B-F7 fix: on exception, close the connection rather
            # than returning it to the pool. Mid-stream errors (especially
            # from break-after-sentinel paths) leave stale frames in the
            # reader buffer; pooling a framing-corrupted connection forces
            # a wasted ping-then-discard cycle on the next caller.
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                connection_returned = True
            # Same fix as above — re-raise the underlying error rather
            # than yielding a sentinel, so the caller's `_process_request_
            # stream` can mark the request errored.
            raise NetworkRequestException(
                f"Remote stream error from {IP}: {e}"
            ) from e
        finally:
            # Success-path: return to pool. Exception path already closed
            # (sets connection_returned=True above).
            if reader and writer and not connection_returned:
                try:
                    self._logger.debug(f"[REMOTE_STREAM] Returning connection for {IP}")
                    await self._return_connection(IP, reader, writer)
                    connection_returned = True
                except Exception:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

    # async def execute_remote(
    #    self,
    #    IP: str,
    #    plugin: str,
    #    method: str,
    #    timeout: tuple,
    #    request_id: str,
    #    args=None,
    #    plugin_uuid="",
    #    author="remote",
    #    author_id="remote",
    # ):
    #    url = f"http://{IP}:{self.port}/execute"
    #    args = args or []
    #
    #    payload = pickle.dumps(args)
    #    b64 = base64.b64encode(payload)
    #
    #    async with httpx.AsyncClient(
    #        timeout=timeout[0] if timeout[0] is not 0.0 else 7200.0
    #    ) as client:  # verify='./cert.pem',  #FIXME Is the timeout needed here and its def not implemented correctly
    #        response = await client.post(
    #            url,
    #            json={
    #                "plugin": plugin,
    #                "method": method,
    #                "args": b64,
    #                "plugin_uuid": plugin_uuid,
    #                "author": author,
    #                "author_id": author_id,
    #                "timeout": timeout,
    #                "author_host": self.plugin_core.hostname,
    #                "request_id": request_id,
    #            },
    #        )
    #        b = base64.b64decode(response.content)
    #        item = pickle.loads(b)
    #        return item

    # async def execute_remote_stream(
    #    self,
    #    IP: str,
    #    plugin: str,
    #    method: str,
    #    timeout: tuple,
    #    request_id: str,
    #    args=None,
    #    plugin_uuid: str = "",
    #    author: str = "remote",
    #    author_id: str = "remote",
    # ):
    #    url = f"http://{IP}:{self.port}/execute_stream"
    #    args = args or []
    #    timeout_val = timeout[0] if timeout[0] != 0.0 else 7200.0
    #
    #    payload = pickle.dumps(args)
    #    b64 = base64.b64encode(payload)
    #
    #    async with httpx.AsyncClient(
    #        timeout=timeout_val
    #    ) as client:  # verify='./cert.pem',
    #        try:
    #            async with client.stream(
    #                "POST",
    #                url,
    #                json={
    #                    "plugin": plugin,
    #                    "method": method,
    #                    "args": args,
    #                    "plugin_uuid": plugin_uuid,
    #                    "author": author,
    #                    "author_id": author_id,
    #                    "timeout": timeout,
    #                    "author_host": self.plugin_core.hostname,
    #                    "request_id": request_id,
    #                },
    #            ) as response:
    #                response.raise_for_status()
    #                async for raw_line in response.aiter_lines():
    #                    if not raw_line:
    #                        continue
    #                    try:
    #                        b = base64.b64decode(raw_line)
    #                        item = pickle.loads(b)
    #                        yield item
    #                    except Exception as e:
    #                        # yield an error tuple or raise depending on your design choice
    #                        self._logger.exception(
    #                            "Failed to decode/deserialize remote stream line"
    #                        )
    #                        yield ("__REMOTE_STREAM_DECODE_ERROR__", str(e))
    #        except Exception as e:
    #            self._logger.exception("execute_remote_stream failed")
    #            yield ("__REMOTE_STREAM_ERROR__", str(e))

    #    def execute_remote_sync(self, host: str, plugin: str, method: str, args=None, plugin_uuid="", author="remote", author_id="remote", timeout=5):
    #        url = f"http://{host}:{self.port}/execute"
    #        with httpx.Client(timeout=timeout) as client:
    #            response = client.post(url, json={
    #                "plugin": plugin,
    #                "method": method,
    #                "args": args,
    #                "plugin_uuid": plugin_uuid,
    #                "author": author,
    #                "author_id": author_id,
    #                "timeout": timeout
    #            })
    #            return response.json()

    #    async def discover_nodes(self, cidr_range=None):
    #        if not cidr_range:
    #            hostname = socket.gethostname()
    #            local_ip = socket.gethostbyname(hostname)
    #            cidr_range = ipaddress.ip_network(local_ip + '/24', strict=False)
    #
    #        sem = asyncio.Semaphore(20)  # Limit to 20 requests at a time for testing
    #
    #        async def probe(ip):
    #            async with sem:
    #                try:
    #                    async with httpx.AsyncClient(timeout=1.0) as client:
    #                        response = await client.get(f"http://{ip}:{self.port}/plugins")
    #                        if response.status_code == 200:
    #                            return str(ip)
    #                except:
    #                    return None
    #
    #        results = await asyncio.gather(*(probe(ip) for ip in cidr_range.hosts()))
    #        self.nodes = [ip for ip in results if ip]
    #        return self.nodes

    @async_handle_errors(None)
    async def update_all_nodes(
        self,
        additional_IP_list: Optional[
            list
        ] = None,  # entries: str | "IP:PORT" | dict | (ip, port)
        timeout: int = 5,
        ignore_enabled_status: bool = False,
        concurrency: int = 20,
    ) -> List[Node]:

        self._logger.info(
            f"[DISCOVERY] update_all_nodes start: existing_ips={len(self.node_ips)}, additional={len(additional_IP_list) if additional_IP_list else 0}, concurrency={concurrency}"
        )
        # Merge and deduplicate endpoints — additional may contain raw strings
        # ("IP" / "IP:PORT") or dicts; normalise all to (ip, port) tuples.
        if additional_IP_list:
            for entry in additional_IP_list:
                self.node_ips.append(self._parse_endpoint(entry))
        self.node_ips = list(dict.fromkeys(self.node_ips))

        # Ensure Node objects exist
        await self._create_nodes(self.node_ips)

        sem = asyncio.Semaphore(max(1, int(concurrency)))
        update_tasks = []

        async def _guarded_update(ip: str):
            async with sem:
                node = await self._get_node(ip)
                if node and (node.enabled or ignore_enabled_status):
                    await self.update_single(ip, timeout)

        for endpoint in self.node_ips:
            ip, _port = endpoint
            update_tasks.append(asyncio.create_task(_guarded_update(ip)))

        if update_tasks:
            await asyncio.gather(*update_tasks, return_exceptions=True)

        self._logger.info(
            f"[DISCOVERY] update_all_nodes done: nodes={len(self.nodes)}, ips={len(self.node_ips)}"
        )
        return self.nodes

    @async_log_errors
    async def update_single(self, IP: str, timeout: int = 5):

        self._logger.debug(
            f"[DISCOVERY] update_single start for {IP} (timeout={timeout})"
        )
        await self._create_new_node(IP)

        try:
            response = await self._get_ip_info(IP, timeout=timeout)
            self._logger.debug(
                f"[DISCOVERY] _get_ip_info response for {IP}: type={type(response).__name__}"
            )

            if not response:
                node = await self._get_node(IP)
                if node:
                    # PR3 Stage C step 17 path #6.
                    await self._mark_node_dead(node)
                raise NetworkRequestException("Couldnt reach host")

            # Check if it's a MockResponse (418 error)
            if hasattr(response, "status_code") and response.status_code == 418:
                node = await self._get_node(IP)
                if node:
                    await self._mark_node_dead(node)
                raise NodeException("Host is not discoverable")

            # Response is now a dict, not an httpx.Response
            if not isinstance(response, dict):
                node = await self._get_node(IP)
                if node:
                    await self._mark_node_dead(node)
                raise NetworkRequestException(f"Invalid response format from {IP}")

            # Update node info and mark enabled
            node = await self._get_node(IP)
            if node:
                node.enabled = True
                await node.update(response, self.plugin_core.hostname)
                # PR3 Stage C: peer-connect lifecycle hook (Site A —
                # locked #7). Symmetric initial-exchange — fire-and-
                # forget; idempotent via `_snapshot_sent`.
                if (
                    getattr(self.plugin_core, "networking_enabled", False)
                    and node.hostname
                    and node.hostname != self.plugin_core.hostname
                    and node.hostname not in self._snapshot_sent
                ):
                    asyncio.create_task(self._spawn_initial_exchange(node))

            # Cascade discovery for returned auto_discoverable nodes.
            # Wire format: 3-tuple (IP, port, hostname).
            followups = []
            for sub_node in response.get("nodes", []):
                if len(sub_node) < 2:
                    continue
                sub_ip = sub_node[0]
                if len(sub_node) >= 3:
                    sub_port, sub_hostname = sub_node[1], sub_node[2]
                else:
                    # Defensive: a peer without port in the tuple. Fall back
                    # to None (→ cluster default).
                    sub_port, sub_hostname = None, sub_node[1]

                if sub_hostname == self.plugin_core.hostname:
                    self._logger.info(
                        f"[DISCOVERY] Found own node at {sub_ip} (skipping)"
                    )
                    continue

                # Look for an existing Node for this peer (matching IP and
                # hostname, or IP-only if hostname not yet known).
                existing = next(
                    (
                        n
                        for n in self.nodes
                        if n.IP == sub_ip
                        and (n.hostname == sub_hostname or n.hostname is None)
                    ),
                    None,
                )

                if existing is not None:
                    # Update Node.port if the cascade just told us a port we
                    # didn't have. Fix node_ips dedupe at the same time so
                    # (IP, None) and (IP, port) don't both linger.
                    if existing.port is None and sub_port is not None:
                        existing.port = sub_port
                        self.node_ips = [
                            e
                            for e in self.node_ips
                            if not (e[0] == sub_ip and e[1] is None)
                        ]
                        new_entry = (sub_ip, sub_port)
                        if new_entry not in self.node_ips:
                            self.node_ips.append(new_entry)
                    if existing.hostname is None and sub_hostname:
                        existing.hostname = sub_hostname
                    self._logger.info(
                        f"[DISCOVERY] Updated existing node {sub_ip}"
                        + (f":{sub_port}" if sub_port is not None else "")
                    )
                    continue

                # New peer — add and schedule a follow-up update.
                await self._add_ip(sub_ip, port=sub_port)
                await self._create_new_node(
                    sub_ip,
                    hostname=sub_hostname,
                    port=sub_port,
                )
                followups.append(self.update_single(sub_ip))
                self._logger.info(
                    f"[DISCOVERY] Node found at {sub_ip}"
                    + (f":{sub_port}" if sub_port is not None else "")
                )

            if followups:
                self._logger.debug(
                    f"[DISCOVERY] Scheduling follow-up updates: count={len(followups)}"
                )
                await asyncio.gather(*followups, return_exceptions=True)

        except Exception as e:
            self._logger.debug(f"[DISCOVERY] Failed to reach {IP}: {e}")

    @async_log_errors
    async def _add_ip(self, IP, port: Optional[int] = None):
        """Add an endpoint to node_ips. Accepts a string IP, an "IP:PORT"
        string, a dict, or an explicit (IP, port) — all normalized via
        _parse_endpoint. Idempotent: dedupes on (ip, port).
        """
        if port is not None:
            entry = (str(IP), int(port))
        else:
            entry = self._parse_endpoint(IP)
        if entry not in self.node_ips:
            self._logger.debug(
                f"[DISCOVERY] Adding endpoint to list: {entry[0]}:{entry[1] or self.port}"
            )
            self.node_ips.append(entry)

    @async_log_errors
    async def _create_nodes(self, endpoints: list):
        """Create Node objects for each (ip, port) endpoint."""
        self._logger.debug(
            f"[DISCOVERY] Creating Node objects for {len(endpoints)} endpoints"
        )
        for entry in endpoints:
            ip, port = self._parse_endpoint(entry)
            await self._create_new_node(ip, port=port)

    @async_log_errors
    async def _create_new_node(
        self,
        IP: str,
        hostname: Union[str, None] = None,
        port: Optional[int] = None,
    ):
        if not await self.node_exists(IP):
            self._logger.debug(
                f"[DISCOVERY] Creating new Node: ip={IP}, port={port}, hostname={hostname}"
            )
            self.nodes.append(
                Node(
                    IP=IP,
                    hostname=hostname,
                    enabled=True,
                    auto_discoverable=False,
                    port=port,
                )
            )

    @async_handle_errors(None)
    async def _get_ip_info(
        self, IP: str, timeout: Union[int, float] = 5
    ) -> Optional[dict]:
        """Get node info via socket connection."""
        reader = None
        writer = None
        connection_returned = False

        try:
            self._logger.debug(f"[GET_INFO] Connecting to {IP} (timeout={timeout})")
            reader, writer = await self._get_connection(IP)

            request_data = {
                "hostname": self.plugin_core.hostname,
                "discover_nodes_info": self.discover_nodes,
                # Tell the receiver which port WE listen on. The TCP source
                # port of an inbound connection is ephemeral, so the receiver
                # cannot determine our listener-port from the socket alone.
                # Without this, a peer would record our IP with port=None
                # and try to connect back on its own self.port — a self-loop
                # in same-machine setups.
                "listener_port": self.port,
            }

            self._logger.debug(f"[GET_INFO] Sending INFO to {IP}: {request_data}")
            await self._send_message(writer, MSG_INFO, request_data)

            msg_type, data = await self._receive_message(reader)
            self._logger.debug(
                f"[GET_INFO] Received message from {IP}: type={msg_type}, data_type={type(data).__name__}"
            )

            if msg_type == MSG_RESULT:
                self._logger.debug(
                    f"[GET_INFO] Result from {IP}: keys={list(data.keys()) if isinstance(data, dict) else 'N/A'}"
                )
                return data
            elif msg_type == MSG_ERROR:
                # Check for 418 error (not discoverable)
                if "418" in str(data) or "not discoverable" in str(data).lower():
                    # Return a mock response object with status_code attribute for compatibility
                    class MockResponse:
                        def __init__(self):
                            self.status_code = 418

                    return MockResponse()
                return None
            else:
                # Cycle-4 fix: unexpected msg_type — close rather than pool,
                # wire state indeterminate.
                if reader and writer and not connection_returned:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                    connection_returned = True
                return None

        except Exception as e:
            self._logger.debug(f"[GET_INFO] Failed to reach {IP}: {e}")
            # Cycle-4 fix: transport error — close rather than pool.
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                connection_returned = True
            return None
        finally:
            # Defensive: only reached on clean MSG_RESULT / MSG_ERROR exits.
            if reader and writer and not connection_returned:
                try:
                    self._logger.debug(f"[GET_INFO] Returning connection for {IP}")
                    await self._return_connection(IP, reader, writer)
                    connection_returned = True
                except Exception:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

    @async_log_errors
    async def _delete_node(self, IP: str):
        self._logger.info(f"[NODE] Deleting node {IP}")
        node = await self._get_node(IP)
        if node:
            self.nodes.remove(node)
        else:
            self._logger.warning(f"[NODE] Cannot delete node {IP}: not found")

    @async_log_errors
    async def _enable_node(self, IP: str):
        self._logger.info(f"[NODE] Enabling node {IP}")
        (await self._get_node(IP)).enabled = True

    @async_log_errors
    async def _disable_node(self, IP: str):
        self._logger.info(f"[NODE] Disabling node {IP}")
        node = await self._get_node(IP)
        if node is not None:
            # PR3 Stage C step 17 path #7.
            await self._mark_node_dead(node)

    @async_log_errors
    async def node_exists(self, IP: str):  # FIXME: Add search for hostname
        for node in self.nodes:
            if node.IP == IP:
                return True

        return False

    @async_log_errors
    async def _get_node(
        self, IP: str, hostname: Union[str, None] = None, autogenerate: bool = False
    ) -> Node:  # FIXME: Get Node only by hostname if theres no duplicate?

        if autogenerate:
            await self._create_new_node(IP=IP, hostname=hostname)

        for node in self.nodes:
            if node.IP == IP:
                if node.hostname == hostname or hostname is None:
                    return node

        self._logger.warning(f'A node with IP "{IP}" doesnt exist!')
        return None

    @async_log_errors
    async def _remoteplugin_from_dict(self, plugin_data: dict):
        return RemotePlugin(
            name=plugin_data["plugin_name"],
            version=plugin_data["version"],
            uuid=plugin_data["plugin_uuid"],
            enabled=plugin_data["enabled"],
            remote=plugin_data["remote"],
            description=plugin_data["description"],
            arguments=plugin_data.get("arguments", []),
            hostname=plugin_data.get("hostname", "unknown"),
        )

    async def heartbeat_node(self, node: Node, timeout=5):
        """Ping a node to check if it's alive."""
        reader = None
        writer = None
        connection_returned = False

        try:
            self._logger.debug(
                f"[HEARTBEAT] Pinging node {node.IP} (timeout={timeout})"
            )
            reader, writer = await self._get_connection(node.IP)

            await self._send_message(writer, MSG_PING, {})

            msg_type, data = await asyncio.wait_for(
                self._receive_message(reader), timeout=timeout
            )

            if msg_type == MSG_RESULT and data.get("status") == "ok":
                await node.heartbeat()
                self._logger.debug(f"[HEARTBEAT] Node {node.IP} is alive")
                # Cycle-3 fresh-F1 fix: ONLY return to pool on confirmed
                # success. On any non-success exit (return False / except),
                # the connection's reader buffer may carry a stale
                # MSG_RESULT frame from a delayed ping response, which
                # would corrupt the next caller's framing.
                if reader and writer and not connection_returned:
                    try:
                        await self._return_connection(node.IP, reader, writer)
                        connection_returned = True
                    except Exception:
                        try:
                            writer.close()
                            await writer.wait_closed()
                        except Exception:
                            pass
                return True
            # Unexpected msg_type — close the connection (don't pool a
            # framing-suspicious connection).
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                connection_returned = True
            return False

        except Exception as e:
            self._logger.debug(
                f"Pinging Node with IP {node.IP} was not successful: {e}"
            )
            # Cycle-3 fresh-F1 fix: any exception path means the connection
            # is in an indeterminate state — close it.
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                connection_returned = True
            return False
        finally:
            # Defensive: if for some reason connection_returned is still
            # False (early exit before the success/error branches), close
            # the writer rather than pooling a connection of unknown state.
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    @async_handle_errors(None)
    async def node_has_endpoint(
        self, IP, access_name, plugin_uuid=None, requester_id=None, target_plugin=None
    ) -> Optional[dict]:
        """Check if a node has a specific endpoint (consolidates plugin + endpoint check)."""
        reader = None
        writer = None
        connection_returned = False

        self._logger.info(
            f"[ENDPOINT] Checking endpoint on node {IP}: access_name='{access_name}', "
            f"plugin_uuid={plugin_uuid}, requester_id={requester_id}, "
            f"target_plugin={target_plugin}"
        )

        try:
            self._logger.debug(f"[ENDPOINT] Getting connection to {IP}")
            reader, writer = await self._get_connection(IP)
            self._logger.debug(f"[ENDPOINT] Connection acquired to {IP}")

            request_data = {
                "access_name": access_name,
                "plugin_uuid": plugin_uuid,
                "requester_id": requester_id,
                "target_plugin": target_plugin,
            }

            self._logger.debug(
                f"[ENDPOINT] Sending HAS_ENDPOINT message to {IP}: {request_data}"
            )
            await self._send_message(writer, MSG_HAS_ENDPOINT, request_data)
            self._logger.debug(f"[ENDPOINT] HAS_ENDPOINT message sent to {IP}")

            self._logger.debug(f"[ENDPOINT] Waiting for response from {IP}")
            msg_type, data = await self._receive_message(reader)
            self._logger.debug(
                f"[ENDPOINT] Received message from {IP}: type={msg_type}, "
                f"data_keys={list(data.keys()) if isinstance(data, dict) else 'N/A'}"
            )

            if msg_type == MSG_RESULT:
                available = data.get("available", False)
                self._logger.info(
                    f"[ENDPOINT] Endpoint check result from {IP}: available={available}, "
                    f"hostname={data.get('hostname')}, "
                    f"plugin_info={data.get('plugin_info')}, "
                    f"endpoint={data.get('endpoint')}"
                )
                return data
            elif msg_type == MSG_ERROR:
                self._logger.warning(
                    f"[ENDPOINT] Node {IP} returned error for has_endpoint: {data}"
                )
                return None
            else:
                self._logger.warning(
                    f"[ENDPOINT] Unexpected message type {msg_type} from {IP}"
                )
                # Cycle-4 fix: unexpected msg_type means the wire state is
                # indeterminate (server may still write more frames). Close
                # rather than pool — pooling would corrupt the next caller's
                # framing.
                if reader and writer and not connection_returned:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                    connection_returned = True
                return None

        except Exception as e:
            self._logger.exception(f"[ENDPOINT] Error checking endpoint on {IP}: {e}")
            # Cycle-4 fix: transport error mid-exchange — close rather than
            # pool a writer in unknown state. Mirrors execute_remote /
            # heartbeat_node patterns from cycles 2-3.
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                connection_returned = True
            return None
        finally:
            # Defensive: only reached on clean MSG_RESULT / MSG_ERROR exits
            # (which left framing intact). Unexpected-type and exception
            # paths already closed above.
            if reader and writer and not connection_returned:
                try:
                    self._logger.debug(
                        f"[ENDPOINT] Returning connection to pool for {IP}"
                    )
                    await self._return_connection(IP, reader, writer)
                    connection_returned = True
                except Exception:
                    try:
                        self._logger.debug(
                            f"[ENDPOINT] Closing connection to {IP} (pool return failed)"
                        )
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

    @async_handle_errors(None)
    async def node_get_tagged_endpoints(self, IP: str, tag: str):
        """Ask a remote node for all endpoints matching a tag.

        Args:
            IP: The node's IP address.
            tag: The tag to search for.

        Returns:
            List of tuples (RemotePlugin, endpoint_dict, description, arguments)
            matching the format used by PluginCore.find_endpoints_by_tag,
            or None on error.
        """
        reader = None
        writer = None
        connection_returned = False

        self._logger.info(f"[TAG_SEARCH] Querying node {IP} for tag '{tag}'")

        try:
            reader, writer = await self._get_connection(IP)

            await self._send_message(writer, MSG_FIND_TAGGED_ENDPOINTS, {"tag": tag})

            msg_type, data = await self._receive_message(reader)

            if msg_type == MSG_RESULT:
                remote_endpoints = []
                hostname = data.get("hostname", IP)
                for entry in data.get("endpoints", []):
                    rp = RemotePlugin(
                        name=entry["plugin_name"],
                        version=entry.get("plugin_version", "unknown"),
                        uuid=entry["plugin_uuid"],
                        enabled=True,
                        remote=True,
                        description=entry.get("plugin_description", ""),
                        arguments=[],
                        hostname=hostname,
                    )
                    ep = entry["endpoint"]
                    remote_endpoints.append(
                        (rp, ep, ep.get("description"), ep.get("arguments"))
                    )

                self._logger.info(
                    f"[TAG_SEARCH] Node {IP} returned {len(remote_endpoints)} endpoint(s) for tag '{tag}'"
                )
                return remote_endpoints

            elif msg_type == MSG_ERROR:
                self._logger.warning(f"[TAG_SEARCH] Node {IP} returned error: {data}")
                return None

            # Cycle-4 fix: any other msg_type is unexpected — close rather
            # than pool, since wire state is indeterminate.
            self._logger.warning(
                f"[TAG_SEARCH] Unexpected message type {msg_type} from {IP}"
            )
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                connection_returned = True
            return None

        except Exception as e:
            self._logger.exception(
                f"[TAG_SEARCH] Error querying node {IP} for tag '{tag}': {e}"
            )
            # Cycle-4 fix: transport error — close rather than pool.
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                connection_returned = True
            return None
        finally:
            # Defensive: only reached on clean MSG_RESULT / MSG_ERROR exits.
            if reader and writer and not connection_returned:
                try:
                    await self._return_connection(IP, reader, writer)
                    connection_returned = True
                except Exception:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

    @async_handle_errors(None)
    async def node_has_plugin(
        self,
        IP: str,
        plugin_name: str,
        plugin_uuid: Union[str, None] = None,
        timeout: float = 3.0,
    ) -> Optional[dict]:
        """
        Ask a node if it has the specified plugin.
        DEPRECATED: Use node_has_endpoint instead.
        """
        raise NotImplementedError(
            "node_has_plugin is deprecated. Use node_has_endpoint instead."
        )
        # For backward compatibility, use node_has_endpoint with access_name=None
        # This will check plugin existence but not endpoint
        result = await self.node_has_endpoint(
            IP=IP,
            access_name=None,  # Just check plugin, not endpoint
            plugin_uuid=plugin_uuid,
            target_plugin=plugin_name,
        )

        if result and result.get("plugin_info"):
            # Format response to match old API
            return {
                "available": result.get("available", False),
                "remote": True,  # Remote plugins are always remote
                "hostname": result.get("hostname"),
                "plugin_uuid": (
                    result.get("plugin_info", {}).get("uuid")
                    if result.get("plugin_info")
                    else None
                ),
            }
        return None
