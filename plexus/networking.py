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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Set, Union, Optional, Tuple
from uuid import uuid4
from .decorators import async_log_errors, async_handle_errors, async_gen_handle_errors, async_gen_log_errors
from .exceptions import (
    NetworkRequestException,
    NodeException,
    NoLocalSubException,
    RequestException,
)
from .networking_classes import Node
from .networking_classes import RemotePlugin
from .serialization import safe_loads, FINGERPRINT_CLI_CMD, generate_keypair


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
    # Session 4 (v0.27.0): outbound-side ack tracking. Receiver-side
    # AdvertSub instances leave these as defaults — only sender-side
    # _outbound_adverts entries populate sent_at / acked_at / state /
    # retry_count. Not wire-serialized (see _serialize_local_sub_for_peer).
    sent_at: Optional[float] = None
    acked_at: Optional[float] = None
    state: Literal["pending", "acked", "ack_timeout"] = "pending"
    retry_count: int = 0


# B-066 peer config entry. cert_pem is required (resolved from cert_file
# at config-load time if needed). fingerprint is derived from cert_pem
# at parse time and used as the post-handshake identity gate.
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

# MSG types 7-9 reserved (legacy MSG_NOTIFY / MSG_TOPIC_REQUEST /
# MSG_TOPIC_REQUEST_STREAM, removed when notify/request_topic were
# replaced by publish_event/request_event). Do not reuse these
# numbers for new MSG types.

MSG_STREAM_ITEM_END = 14  # Marks end of one item in a streaming response

# Event protocol message types.
MSG_PUBLISH_EVENT = 15
MSG_REQUEST_EVENT = 16
MSG_REQUEST_EVENT_STREAM = 17
# C-122: advert protocol — receiver maintains a per-peer subscription
# table from these two messages. MSG_SUB_ADVERTISE carries the full
# snapshot replace ("subscriptions": [...] + "kind": "snapshot");
# MSG_SUB_DELTA carries a single add or remove ("kind": "add" |
# "remove"). Ack-tracked via MSG_SUB_ADVERTISE_ACK; sender resends on
# ack-timeout per heartbeat_interval scan.
MSG_SUB_ADVERTISE = 18
MSG_SUB_DELTA = 19

# MSG_AUTH = 20 was removed alongside the B-066 SPKI-pin mTLS rework
# (replaced shared-secret auth). The message-type number is reserved
# and must not be reused for new message types.

# Session 4 (v0.27.0): receiver-side acknowledgement of MSG_SUB_ADVERTISE
# / MSG_SUB_DELTA. Async-delivered via the receiver's outbound connection
# back to the sender; sender does NOT block on ack arrival. Sender's
# heartbeat loop scans _outbound_adverts for entries past 2 *
# heartbeat_interval and resends once; second timeout marks state =
# 'ack_timeout'.
MSG_SUB_ADVERTISE_ACK = 21

CHUNK_SIZE = 64 * 1024  # 64KB chunks for streaming
MAX_MESSAGE_SIZE = 100 * 1024 * 1024  # 100MB max message size
MAX_ADVERT_SUBS_PER_PEER = 100_000  # Cap MSG_SUB_ADVERTISE entries to bound _adverts_struct_lock hold time


# Default heartbeat tick interval (seconds). NetworkManager iterates the
# node list every tick and pings each peer; on failure the peer is marked
# dead via _mark_node_dead. Configurable via networking.heartbeat_interval
# in config.yml; tests may override self.heartbeat_interval directly.
DEFAULT_HEARTBEAT_INTERVAL: float = 10.0

# C-109: periodic full-snapshot resync interval. Every interval, the
# sender re-advertises its full sub set to every connected peer. Scrubs
# ghost subs that survived a race between an in-flight resend and a
# delta-remove (the ghost's sub_uuid stays on the peer's _inbound_adverts
# forever until peer drop/reconnect, since re-subscribe creates a NEW
# sub_uuid that doesn't touch the ghost). 5 min default keeps wasted
# bandwidth bounded: O(N_peers * N_subs * 5min_interval). Configurable
# via networking.resync_interval in config.yml; tests override
# self.resync_interval directly.
DEFAULT_RESYNC_INTERVAL: float = 300.0

# Default discovery / node-lookup loop interval (seconds). Periodic
# update_all_nodes loop tick that re-resolves peer addresses and reaps
# unreachable nodes. Configurable via networking.lookup_interval in
# config.yml; tests may override self.lookup_interval directly.
DEFAULT_LOOKUP_INTERVAL: float = 60.0

# Default liveness timeout (seconds). A peer whose last successful
# heartbeat is older than this is considered dead and dropped from
# advert state. Should be >= heartbeat_interval; in practice 2-3x is
# typical. Configurable via networking.liveness_timeout in config.yml;
# tests may override self.liveness_timeout directly.
DEFAULT_LIVENESS_TIMEOUT: float = 30.0

# R2-LL-5: per-probe budget (seconds) for a single heartbeat ping.
# Distinct from ``liveness_timeout`` (the deadline a peer can be silent
# before it's marked dead) — this caps how long ONE probe waits for a
# reply. With ``liveness_timeout`` >> ``heartbeat_interval`` a catatonic
# peer used to delay the ack-timeout scan by up to ``liveness_timeout``
# seconds because the heartbeat loop blocked on its own probe. Defaulted
# to ``min(heartbeat_interval, liveness_timeout)`` if unset by the
# operator. Configurable via networking.probe_timeout in config.yml.
DEFAULT_PROBE_TIMEOUT: Optional[float] = None


class NetworkManager:
    def __init__(
        self,
        plexus,
        logger: Logger,
        node_ips: list,
        discover_nodes: bool,
        direct_discoverable: bool,
        auto_discoverable: bool,
        port=2510,
        # C-029 + C-030: ``secret`` / ``cert_file`` / ``key_file`` kwargs
        # removed — they were dead post-K-3 (mTLS pinning replaced
        # shared-secret auth and inline cert file paths). Operators
        # whose configs still carry these keys see them silently
        # ignored at the yaml level; the kwarg path no longer exists.
        pool_size: int = 5,
        networking_config: Optional[dict] = None,
        config_dir: Optional[Path] = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        lookup_interval: float = DEFAULT_LOOKUP_INTERVAL,
        liveness_timeout: float = DEFAULT_LIVENESS_TIMEOUT,
        resync_interval: float = DEFAULT_RESYNC_INTERVAL,
        # R2-LL-5: per-probe budget for a single heartbeat ping. ``None``
        # defaults to ``min(heartbeat_interval, liveness_timeout)`` (set
        # below after both fields are resolved).
        probe_timeout: Optional[float] = DEFAULT_PROBE_TIMEOUT,
    ):
        self.plexus = plexus
        self._logger = logger

        # B-066 hard error on legacy node_ips schema. Operators must
        # migrate to the peers: schema. Fires BEFORE any other init so
        # a misconfigured node fails fast with an actionable message.
        nw_cfg = networking_config or {}
        if "node_ips" in nw_cfg:
            raise RuntimeError(
                "node_ips schema removed in the B-066 SPKI-pin rework. Migrate to:\n"
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
        #
        # C-006/C-031: self.nodes and self.node_ips follow an
        # immutable-snapshot pattern. Treat both as read-only tuples
        # from the reader's perspective. Mutations build a NEW tuple
        # and atomically rebind the attribute (single STORE_ATTR
        # bytecode op in CPython — readers always see either the old
        # or the new tuple, never a mid-update list). NEVER call
        # .append() / .remove() / .extend() etc. on these attributes —
        # those break the immutability invariant and reintroduce the
        # concurrent-read race. New nodes are added at runtime via
        # the build-tuple-then-rebind helpers below.
        self.node_ips: Tuple[Tuple[str, Optional[int]], ...] = tuple(
            dict.fromkeys(
                self._parse_endpoint(e) for e in (node_ips or [])
            )
        )

        self.discover_nodes = discover_nodes
        self.direct_discoverable = direct_discoverable
        self.auto_discoverable = auto_discoverable

        self.port = port
        self.nodes: Tuple[Node, ...] = ()

        # B-066 mTLS identity + peer config. SPKI-pinned mTLS
        # replaced the legacy shared-secret + cert_file / key_file
        # config; identity now lives on disk under keys_dir.
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

        # B-066: seed self.node_ips from peers so the existing discovery
        # flow (update_all_nodes / heartbeat) can find peers via the new
        # schema without rewriting discovery itself. Build the combined
        # tuple locally then rebind once (C-006 immutable-snapshot).
        seeded: List[Tuple[str, Optional[int]]] = list(self.node_ips)
        for spec in self.peers:
            entry = (spec.ip, spec.port)
            if entry not in seeded:
                seeded.append(entry)
        self.node_ips = tuple(seeded)

        # C-029 + C-030: legacy `self.secret`, `self.cert_file`,
        # `self.key_file`, and `self._temp_ssl_files` deleted —
        # shared-secret auth was replaced by mTLS-pinned peers in K-3
        # and the inline cert-file path was replaced by per-peer cert
        # PEMs in the `peers:` schema. The `NETWORKING_SECRET` env-var
        # read is also removed; if you need it set during transition,
        # ignore it (the framework does not use it).
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
        # C-029: `self._temp_ssl_files` deleted along with the
        # `_create_ssl_context` method that produced temp PEM files.
        # mTLS pin context is built from disk cert paths directly.

        # Loop intervals and timeouts. Defaults are
        # DEFAULT_HEARTBEAT_INTERVAL / DEFAULT_LOOKUP_INTERVAL /
        # DEFAULT_LIVENESS_TIMEOUT; override via networking.heartbeat_interval
        # / networking.lookup_interval / networking.liveness_timeout in
        # config.yml (parsed by ConfigUtil.apply_configvalues and passed in
        # by Plexus at NetworkManager construction).
        self.heartbeat_interval: float = heartbeat_interval
        self.lookup_interval: float = lookup_interval
        self.liveness_timeout: float = liveness_timeout
        self.resync_interval: float = resync_interval
        # R2-LL-5: when the operator hasn't pinned ``probe_timeout``
        # explicitly, default it to ``min(heartbeat_interval,
        # liveness_timeout)``. This keeps one catatonic peer from
        # delaying the ack-timeout scan by up to ``liveness_timeout``
        # seconds (the old probe budget) while still respecting any
        # tighter heartbeat cadence the operator has configured.
        self.probe_timeout: float = (
            float(probe_timeout)
            if probe_timeout is not None and float(probe_timeout) > 0
            else min(heartbeat_interval, liveness_timeout)
        )
        # C-109: monotonic timestamp of the most recent periodic
        # full-snapshot resync sweep. Compared against
        # time.monotonic() inside heartbeat_loop to decide when the
        # next sweep fires. Initialise to time.monotonic() (not 0.0)
        # so the first sweep fires a FULL resync_interval after NM
        # construction, not immediately after a hot-reload on a
        # long-running process where monotonic() is already huge.
        self._last_resync_ts: float = time.monotonic()

        # C-044: N-strikes heartbeat. A single missed heartbeat (transient
        # network blip, brief peer overload, GC pause on the other side)
        # used to mark a node dead immediately and flap peers. Now we
        # tolerate N-1 consecutive misses and only mark dead on the Nth
        # miss. Per-hostname counter; reset on any successful heartbeat.
        # Default 3 strikes (~3*heartbeat_interval = 30s grace). Configurable
        # via networking.heartbeat_strikes in config.yml. Override possible
        # by setting self.heartbeat_strikes on the instance for tests.
        self.heartbeat_strikes: int = 3
        self._heartbeat_misses: Dict[str, int] = {}

        # C-043: track checked-out writers so stop() can close ALL open
        # connections (not just pooled ones). Without this, a request
        # coroutine holding a writer at shutdown leaks the underlying
        # socket fd. Populated in _get_connection success paths and
        # _create_connection; removed in _return_connection on return
        # to pool.
        self._checked_out_writers: Set[asyncio.StreamWriter] = set()

        # C-115: per-peer resend-task table keyed by peer hostname. The
        # ack-timeout scan spawns at most one in-flight resend per peer;
        # _drop_peer_advert_state cancels it on revoke / peer-dead so a
        # stale resend cannot fire against torn-down advert tables. Init
        # here (not lazily in the scan helper) so _drop_peer_advert_state
        # never has to gate on `hasattr`.
        self._resend_tasks: Dict[str, asyncio.Task] = {}

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

        # C-110: per-NM session_id, regenerated at construction (so a
        # process restart yields a new id even when the hostname stays
        # the same). Sent with outbound advert + delta payloads so
        # receivers can detect a peer restart and re-arm their
        # snapshot-sent gate without waiting for heartbeat-dead.
        self.session_id: str = uuid4().hex

        # C-110: track the last session_id each peer announced. When a
        # peer sends a payload with a session_id != the one we have on
        # record, treat that as a restart-or-reconnect signal and
        # clear our outbound-snapshot-sent entry for that peer so the
        # next reciprocal-exchange trigger re-sends. Pre-restart this
        # check could only fire on heartbeat-strikes (after several
        # missed pings) so fast TCP reconnects within the heartbeat
        # window left stale advert state on both sides.
        self._peer_session_ids: Dict[str, str] = {}

        # C-110: snapshot-sent gate is now keyed by hostname; value is
        # the peer's session_id we observed when we sent the snapshot,
        # OR ``None`` as a sentinel for "snapshot was sent but the
        # peer's session_id wasn't known yet" (first contact — the
        # first inbound payload's session_id is adopted into this
        # slot by ``_detect_peer_restart``'s first-contact branch).
        # Mismatch between stored value and the peer's currently-
        # announced session_id means the peer restarted; treat as
        # not-sent and re-send. Hostname-only keying (the pre-C-110
        # design) made a same-hostname reconnect after a clean
        # restart indistinguishable from a continuous session.
        # Mutated: set after first successful _initial_advert_exchange;
        # cleared in _drop_peer_advert_state so reconnects re-arm;
        # cleared on session-id mismatch detection in the
        # MSG_SUB_ADVERTISE / MSG_SUB_DELTA handlers.
        self._snapshot_sent: Dict[str, Optional[str]] = {}

        # Per-peer in-flight initial-exchange task (cancellable on
        # disconnect via _drop_peer_advert_state).
        self._initial_exchange_tasks: Dict[str, asyncio.Task] = {}

        # Networking-ready flag. True after start() finishes wiring
        # background tasks; False at top of stop(). Used by add/remove
        # broadcast hooks to no-op until peers can be reached.
        self.is_ready: bool = False

        # B-071: per-peer wire counters. Keyed by peer hostname; values are
        # flat dicts {bytes_sent, bytes_recv, msgs_sent, msgs_recv}. Entry
        # is pre-created at handshake-time stamp sites in _create_connection
        # / _handle_client. Increment helpers (_count_sent / _count_recv) use
        # dict.get + None-skip so a late frame after _drop_peer_advert_state
        # popped the entry won't recreate stale state. Counters reset when
        # peer is declared dead by heartbeat (_drop_peer_advert_state path),
        # NOT on TCP-session close — persistent peers accumulate across pool
        # churn. Per O7 (current-session only).
        self.peer_stats: Dict[str, Dict[str, int]] = {}

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

        Walks self.peers_by_endpoint first (authoritative for the
        mTLS-pinned regime). Falls back to the self.nodes / self.node_ips
        lookup so existing tests that still scaffold via node_ips
        continue to work.

        R2-CC-7: rejects duplicate-IP peers. Two peers sharing an IP
        but using different ports yields an ambiguous endpoint lookup —
        the first match would silently win and route all traffic away
        from the second peer. We raise here rather than silently
        resolving wrong, because the ambiguity has no defensible
        resolution policy at the connection layer. The peers list is
        unique by (ip, port) per _parse_one_peer; this guards against
        the same IP appearing twice with different ports.
        """
        matches = [
            (peer_ip, peer_port)
            for peer_ip, peer_port in self.peers_by_endpoint.keys()
            if peer_ip == IP
        ]
        if len(matches) > 1:
            # raise on duplicate ip — ambiguous outbound routing.
            raise RuntimeError(
                f"[CONFIG] duplicate ip {IP!r} across peers_by_endpoint "
                f"with distinct ports {[p for _, p in matches]}; "
                f"_resolve_port cannot pick one. Peers must have unique "
                f"IPs (a single host running multiple peers needs a "
                f"reverse-proxy or distinct interface bindings)."
            )
        if matches:
            return matches[0][1]
        for node in self.nodes:
            if node.IP == IP:
                return node.port if node.port is not None else self.port
        for ip, port in self.node_ips:
            if ip == IP and port is not None:
                return port
        return self.port

    # ── B-066 / B-018b — split helper ──────────────────────────────

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
            author_id in self.plexus.plugins_by_uuid
            or author_id == self.plexus.hostname
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

    # ── B-066 — peer parsing + identity helpers ─────────────────────

    def _parse_peers(self, raw_peers) -> List[PeerSpec]:
        """Parse the peers config list, resolve cert_file → cert_pem,
        derive SPKI fingerprint, validate uniqueness.

        Accepts None (bare YAML key with no value) and treats as empty.
        """
        if raw_peers is None:
            raw_peers = []

        peers: List[PeerSpec] = []
        seen_fps: Set[str] = set()
        seen_endpoints: Set[Tuple[str, int]] = set()

        for entry in raw_peers:
            peers.append(NetworkManager._parse_one_peer(
                self._logger,
                entry,
                port_default=self.port,
                keys_dir=self.keys_dir,
                seen_fps=seen_fps,
                seen_endpoints=seen_endpoints,
            ))

        if not peers:
            self._logger.debug("[NETWORKING] _parse_peers returned empty list")

        return peers

    @staticmethod
    def _parse_peers_dryrun(
        logger,
        raw_peers,
        *,
        port_default: int,
        keys_dir: Path,
    ) -> None:
        """Dry-run validation of a peers list — no side effects, no
        instance state mutation, no SSL context, no socket binds.

        Re-uses ``_parse_one_peer`` parse logic with throwaway
        uniqueness sets. Raises ``RuntimeError`` on the first
        malformed peer entry (cert PEM bad, IPv6 bracket missing,
        fingerprint mismatch, duplicate fp/endpoint). Returns None
        on success.

        Static so callers without a live ``NetworkManager`` (e.g. a
        transition from ``networking_enabled=False`` to ``True``
        during hot reload) can validate before construction. Per
        Commit 2b cycle 3 — the inline validation pattern would
        otherwise need a temporary NetworkManager instance, defeating
        the "no side effects" guarantee of pre-validation.
        """
        if raw_peers is None:
            raw_peers = []
        seen_fps: Set[str] = set()
        seen_endpoints: Set[Tuple[str, int]] = set()
        for entry in raw_peers:
            NetworkManager._parse_one_peer(
                logger,
                entry,
                port_default=port_default,
                keys_dir=keys_dir,
                seen_fps=seen_fps,
                seen_endpoints=seen_endpoints,
            )

    @staticmethod
    def _parse_one_peer(
        logger,
        entry: dict,
        *,
        port_default: int,
        keys_dir: Path,
        seen_fps: Set[str],
        seen_endpoints: Set[Tuple[str, int]],
    ) -> PeerSpec:
        """Parse a single peer entry into a PeerSpec.

        Mutates ``seen_fps`` / ``seen_endpoints`` in-place to enforce
        cross-entry uniqueness within a single ``_parse_peers`` pass.
        ``logger`` is used only for ``logger.warning(...)`` on the
        bare-IPv6 fallback branch — passed in as a parameter so the
        Commit 2b dry-run path (``_parse_peers_dryrun``) can call this
        without a live ``NetworkManager`` instance.
        """
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization as _ser

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
                cf_path = (keys_dir.parent / cf_path).resolve()
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
        # defaults to port_default).
        if address.startswith("["):
            end_bracket = address.find("]")
            if end_bracket == -1:
                raise RuntimeError(
                    f"Peer {hostname} address {address!r}: opening bracket "
                    "without closing bracket. Use [ipv6]:port form."
                )
            ip = address[1:end_bracket]
            if not ip:
                # Cycle 2 verifier MED fix: reject empty bracket "[]:port"
                # at config-load time instead of letting it propagate to
                # PeerSpec(ip="") and surface later as a cryptic
                # socket.gaierror at connect time.
                raise RuntimeError(
                    f"Peer {hostname} address {address!r}: empty bracket. "
                    "Provide an IPv6 address inside the brackets, e.g. [::1]:2511."
                )
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
                port = port_default
            else:
                raise RuntimeError(
                    f"Peer {hostname} address {address!r}: unexpected suffix "
                    f"{rest!r} after closing bracket."
                )
        elif address.count(":") > 1:
            # Bare IPv6 — treat the whole string as the IP, port defaults.
            ip = address
            port = port_default
            logger.warning(
                "[CONFIG] Peer %s address %r is bare IPv6; using default port "
                "%d. To specify a non-default port, use [%s]:port form.",
                hostname, address, port, address,
            )
        else:
            ip, _, port_str = address.partition(":")
            port = int(port_str) if port_str else port_default
        endpoint = (ip, port)
        if endpoint in seen_endpoints:
            raise RuntimeError(
                f"Duplicate peer endpoint across config: {ip}:{port}"
            )
        seen_endpoints.add(endpoint)

        return PeerSpec(
            hostname=hostname, ip=ip, port=port,
            cert_pem=cert_pem, fingerprint=derived_fp,
            system_caller=entry.get("system_caller", False),
        )

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

        # W4-N2: Python 3.12+ rejects multi-dot suffixes in `with_suffix`; use
        # safe name concatenation for identical filesystem semantics.
        cert_tmp = self.cert_path.parent / (self.cert_path.name + ".tmp")
        key_tmp = self.key_path.parent / (self.key_path.name + ".tmp")
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

        # C-096: sanity check — own_fingerprint must NOT appear in our
        # peers[] list. Operators sometimes mis-paste their own cert
        # PEM into their OWN config thinking it's the peer's; the
        # phantom self-as-peer entry then routes outbound publishes
        # back to this node and confuses the advert protocol. Hard
        # error at boot is friendlier than mysterious dispatch loops.
        if self.own_fingerprint in self.peers_by_fingerprint:
            raise RuntimeError(
                f"[NETWORKING] Misconfiguration: own_fingerprint "
                f"{self.own_fingerprint} appears in this node's peers[] "
                f"list. A node cannot list itself as a peer — that would "
                f"create a self-loop in the dispatch graph. Remove the "
                f"matching peers[] entry from config.yml (it almost "
                f"certainly carries this node's own cert.pem, not a "
                f"peer's)."
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
        # W2-E1: fail fast on empty peers. revoke_peer can drain the
        # trust list to zero at runtime; without this guard we silently
        # build a CERT_REQUIRED context with no trusted CAs, and every
        # subsequent TLS handshake fails with an opaque verification
        # error far from the actual misconfiguration.
        if not self.peers:
            raise RuntimeError(
                "_create_pinned_ssl_context: self.peers is empty; "
                "no trusted CAs available (likely all peers revoked)"
            )
        context = ssl.SSLContext(protocol)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        # Order matters in Python 3.10+: verify_mode must be set BEFORE
        # check_hostname=False, otherwise check_hostname=False raises ValueError.
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = False
        context.load_cert_chain(str(self.cert_path), str(self.key_path))
        # R3-PP-5: the hard raise above on empty self.peers already guarantees
        # self.peers is non-empty here; the previous `if self.peers:` guard
        # was unreachable-False dead code that suggested a non-existent
        # else branch. Call load_verify_locations unconditionally.
        cadata = "\n".join(p.cert_pem for p in self.peers)
        context.load_verify_locations(cadata=cadata)
        return context

    def _pool_key(self, IP: str) -> tuple[str, int]:
        """Connection-pool key for an IP. Always (IP, port) — two peers on
        the same IP but different ports get separate pools.
        """
        return (IP, self._resolve_port(IP))

    # Message Protocol Utilities

    def _count_sent(
        self, writer: asyncio.StreamWriter, total_bytes: int
    ) -> None:
        """B-071: increment per-peer counters for one fully-drained frame.

        ``total_bytes`` = header + payload sizes summed by caller. Counted
        only on successfully drained writes — partial-frame bytes left in
        the socket buffer when ``writer.drain()`` raises are NOT counted
        (matches "current-session live-peer state" semantic).

        No-op when:
        - writer was never stamped (test fixtures bypassing pin-check), or
        - peer entry was popped from peer_stats by _drop_peer_advert_state
          (race with heartbeat-declared-dead — the late frame is correctly
          omitted instead of recreating stale state for a now-dead peer).
        """
        hostname = getattr(writer, "_aio_peer_hostname", None)
        if hostname is None:
            return
        stats = self.peer_stats.get(hostname)
        if stats is None:
            return
        stats["bytes_sent"] += total_bytes
        stats["msgs_sent"] += 1

    def _count_recv(
        self, reader: asyncio.StreamReader, total_bytes: int
    ) -> None:
        """B-071: increment per-peer counters for one fully-received frame.

        Counted only on full-frame success — if any of the readexactly
        calls in the receiving function raises IncompleteReadError mid-frame,
        the bytes already off the socket are NOT counted (acceptable per
        O7 'current-session only').
        """
        hostname = getattr(reader, "_aio_peer_hostname", None)
        if hostname is None:
            return
        stats = self.peer_stats.get(hostname)
        if stats is None:
            return
        stats["bytes_recv"] += total_bytes
        stats["msgs_recv"] += 1

    async def _send_message(
        self, writer: asyncio.StreamWriter, msg_type: int, data: any
    ) -> None:
        """Serialize and send a message with length prefix."""
        try:
            payload = pickle.dumps(data)
            # R2-CC-4: sender/receiver parity. Receiver paths use `>=` so the
            # sender must also reject at exactly MAX (the boundary value is
            # not a usable wire size if the peer will reject it).
            if len(payload) >= MAX_MESSAGE_SIZE:
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
            # R2-CC-2: bounded drain — a slow/stuck peer must not wedge the
            # sender forever. TODO: surface as networking.send_drain_timeout config knob.
            await asyncio.wait_for(writer.drain(), timeout=30.0)
            self._count_sent(writer, len(header) + len(payload))
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
                MSG_SUB_ADVERTISE_ACK: "SUB_ADVERTISE_ACK",
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

            # R2-CC-4: sender/receiver parity — both reject at exactly MAX.
            if msg_length >= MAX_MESSAGE_SIZE:
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

            # B-071: per-peer recv counter. 4-byte length header + 1-byte
            # type + payload bytes = total wire bytes for this frame.
            self._count_recv(reader, 4 + 1 + payload_length)

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
        except pickle.UnpicklingError:
            # Cycle 3 fresh-eyes LOW fix: re-raise without ERROR-level
            # traceback. _handle_client's inner loop catches this and
            # logs at WARNING with peer context. Letting the bare except
            # below run would double-log every disallowed-class event.
            raise
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
                    # R2-CC-2: bounded drain. TODO: surface as config knob.
                    await asyncio.wait_for(writer.drain(), timeout=30.0)
                    self._count_sent(writer, len(header) + len(chunk_data))
                    offset += CHUNK_SIZE
            else:
                # Small chunk, send directly
                chunk_length = len(payload) + 1
                header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                writer.write(header + payload)
                # R2-CC-2: bounded drain. TODO: surface as config knob.
                await asyncio.wait_for(writer.drain(), timeout=30.0)
                self._count_sent(writer, len(header) + len(payload))
        except Exception as e:
            self._logger.exception("Error sending stream chunk")
            raise

    async def _send_end_stream(self, writer: asyncio.StreamWriter) -> None:
        """Send end of stream marker."""
        try:
            header = struct.pack(">IB", 1, MSG_END_STREAM)
            writer.write(header)
            # R2-CC-2: bounded drain. TODO: surface as config knob.
            await asyncio.wait_for(writer.drain(), timeout=30.0)
            self._count_sent(writer, len(header))
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

    # C-029: `_create_ssl_context` removed. The K-3 mTLS pinning path uses
    # `_create_pinned_ssl_context` (server) and `_create_client_ssl_context`
    # (client) directly from `self.cert_path` / `self.key_path` on disk;
    # `_load_or_generate_identity` writes those at boot. The old
    # `_create_ssl_context` had no callers and produced temp PEM files from
    # the legacy `cert_file` / `key_file` kwargs that were removed alongside.

    async def start(self):
        """Starts socket server without blocking the main loop.

        Bootstrap order (B-068 fix): identity is loaded or generated FIRST,
        then peers is checked. This lets a fresh-cluster operator boot once
        with empty peers, get a clear error, AND walk away with a valid
        cert.pem / key.pem on disk plus the fingerprint and cert PEM logged
        at INFO. The operator can then share the fingerprint with peer
        nodes, populate networking.peers in config, and restart.

        K-3 (B-066): peers must still be configured non-empty (else the
        mTLS trust store would be empty and OpenSSL would reject every
        connection with an opaque error — fail fast with an actionable
        message instead).
        """
        self._load_or_generate_identity()
        if not self.peers:
            raise RuntimeError(
                "[NETWORKING] Cannot start with empty peers list. The mTLS "
                "trust store would be empty, causing every incoming and outgoing "
                "connection to fail with an opaque OpenSSL error.\n"
                f"This node's identity has been loaded or generated under "
                f"{self.keys_dir}. The fingerprint and cert PEM are in the "
                "log at INFO level (search for '[NETWORKING] Identity ready'). "
                "You can also re-print the fingerprint at any time with "
                "`python -m networking_cli show-fingerprint --config <config.yml>`. "
                "Either:\n"
                "  - Add at least one peer to networking.peers in your config "
                "(use the cert PEM and fingerprint other nodes have logged), OR\n"
                "  - Disable networking entirely by removing the networking section."
            )
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

        async def _do_one_heartbeat(node, probe_timeout):
            """C-091 helper: per-node heartbeat with C-044 strike
            handling. All exceptions absorbed here so gather() never
            sees a raise. Used by heartbeat_loop to parallelize.

            R2-LL-5: the probe budget is now ``probe_timeout`` (a
            dedicated knob) rather than the much larger
            ``liveness_timeout``. A catatonic peer no longer pins the
            heartbeat tick to its full liveness window — the probe
            fails fast and the ack-timeout scan stays on schedule.
            """
            try:
                if not node.enabled:
                    return
                ok = await self.heartbeat_node(node, timeout=probe_timeout)
                if ok:
                    # C-044: reset miss counter on any success.
                    self._heartbeat_misses.pop(
                        getattr(node, "hostname", None) or "", None
                    )
                else:
                    # C-044: N-strikes heartbeat. Tolerates transient
                    # network blips without flapping peers.
                    if await self._record_heartbeat_miss(node):
                        await self._mark_node_dead(node)
            except Exception:
                # C-044: exception path also increments the counter so a
                # peer whose every heartbeat raises is eventually marked
                # dead, but a single intermittent exception doesn't.
                try:
                    if await self._record_heartbeat_miss(node):
                        await self._mark_node_dead(node)
                except Exception:
                    pass

        async def heartbeat_loop():
            while True:
                # C-091: snapshot interval/timeout at tick start so a
                # mid-tick mutation can't cause one node to use the old
                # liveness_timeout while another sees the new one. The
                # adopted interval also drives this tick's sleep, so a
                # bumped heartbeat_interval takes effect on the NEXT
                # tick — consistent semantics regardless of where in
                # the loop the mutation lands.
                # R2-LL-5: snapshot ``probe_timeout`` too — same tick-
                # stability rationale. The dedicated probe budget
                # replaces the prior use of ``liveness_timeout`` as
                # both deadline AND per-probe wait, which let one
                # catatonic peer stall the ack-timeout scan by the
                # full liveness window.
                heartbeat_interval = self.heartbeat_interval
                probe_timeout = self.probe_timeout
                try:
                    # C-091: parallelize per-peer heartbeats so one slow
                    # peer can't delay all the others. Each per-node
                    # coroutine catches its own exceptions; gather sees
                    # only successful Nones and never raises.
                    nodes_snapshot = list(self.nodes)
                    if nodes_snapshot:
                        await asyncio.gather(
                            *(
                                _do_one_heartbeat(n, probe_timeout)
                                for n in nodes_snapshot
                            ),
                            return_exceptions=True,
                        )
                except Exception:
                    self._logger.debug("Heartbeat iteration failed")

                # Session 4 (v0.27.0): scan outbound adverts for missing
                # acks. The scan itself is fast (single struct_lock pass);
                # per-peer resends are spawned as detached tasks so the
                # heartbeat tick stays on schedule.
                try:
                    await self._check_advert_ack_timeouts()
                except Exception:
                    self._logger.debug(
                        "Advert ack timeout scan failed", exc_info=True,
                    )

                # C-109: periodic full-snapshot resync. Scrubs ghost
                # subs that survived the add-then-remove-vs-resend race
                # — the ghost's sub_uuid lives in the peer's
                # _inbound_adverts until peer drop/reconnect because
                # re-subscribe creates a new sub_uuid that doesn't
                # touch the ghost. A periodic full-snapshot replace
                # rewrites the peer's per-peer inbound table from our
                # current local sub list (snapshot semantics in
                # _handle_sub_advertise), purging anything we no
                # longer hold. Spawn per-peer as detached tasks so
                # the heartbeat tick stays on schedule.
                #
                # ``_last_resync_ts`` is stamped BEFORE the spawn so a
                # broken spawn cannot tight-loop the resync. The
                # tradeoff is that a persistently-failing spawn is
                # silently retried every resync_interval. Log the
                # failure at WARNING (not DEBUG) so operators with
                # default-level logging still see the breakage.
                try:
                    now_mono = time.monotonic()
                    if (
                        self.resync_interval > 0
                        and (now_mono - self._last_resync_ts)
                        >= self.resync_interval
                    ):
                        self._last_resync_ts = now_mono
                        await self._spawn_periodic_resync()
                except Exception:
                    self._logger.warning(
                        "Periodic resync scheduling failed; next attempt "
                        "in %.0fs",
                        self.resync_interval,
                        exc_info=True,
                    )

                await asyncio.sleep(heartbeat_interval)

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
                except Exception as e:
                    # R4-VV-2: heartbeat / discovery task may carry a stored
                    # exception (e.g. OSError from socket during shutdown).
                    # Awaiting re-raises it; without this branch the
                    # exception would propagate out of stop() and skip the
                    # pool drain + _checked_out_writers cleanup below.
                    # Mirrors the R3-RR-7 pattern used for server_task.
                    self._logger.warning(
                        "background task on %s:%d raised non-CancelledError during stop(): %s",
                        getattr(self, "hostname", "?"),
                        getattr(self, "port", 0),
                        e,
                        exc_info=True,
                    )

        # PR3 Stage C step 17 path #3: drop advert state for every peer
        # we've ever known about, BEFORE closing pooled connections.
        # C-124: include _advert_locks and _initial_exchange_tasks in
        # the peer-host union. A peer can have a lock entry (from a
        # past attempted advertise) or an in-flight initial-exchange
        # task without yet having any _inbound/_outbound/snapshot_sent
        # bookkeeping — those peers were previously missed by stop()
        # and their resend/initial-exchange tasks ran past shutdown.
        try:
            async with self._adverts_struct_lock:
                hosts = list(
                    set(self._inbound_adverts.keys())
                    | set(self._outbound_adverts.keys())
                    | set(self._snapshot_sent)
                    | set(self._advert_locks.keys())
                    | set(self._initial_exchange_tasks.keys())
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
            except Exception as e:
                # R3-RR-7: server task may raise OSError / SSLError on accept
                # or handshake failure during shutdown. Suppress so the pool
                # drain and _checked_out_writers cleanup below always run.
                self._logger.warning(
                    "server_task on %s:%d raised non-CancelledError during stop(): %s",
                    getattr(self, "hostname", "?"),
                    getattr(self, "port", 0),
                    e,
                    exc_info=True,
                )

        # Close all pooled connections (key is (ip, port))
        for key, pool in self.connection_pools.items():
            closed = 0
            while not pool.empty():
                # R4-VV-9: separate pool.get() failures (-> break out of
                # this pool's drain) from per-writer close failures
                # (-> ``continue`` to the next writer). Previously a
                # single broad ``except: break`` abandoned the rest of
                # the queue on the first writer close error (e.g.
                # ConnectionResetError from wait_closed on Windows),
                # leaking the remaining writers' fds.
                try:
                    reader, writer = await asyncio.wait_for(pool.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    # Queue effectively empty under contention — stop
                    # draining this pool.
                    break
                except Exception:
                    # pool.get() raised something unexpected; bail on
                    # this pool but keep going with the next.
                    break
                try:
                    writer.close()
                    await writer.wait_closed()
                    closed += 1
                except Exception as e:
                    self._logger.warning(
                        "stop(): pool drain close for %r failed: %s — continuing drain",
                        key, e,
                    )
                    continue
            if closed:
                ip_, port_ = key
                self._logger.debug(
                    f"[CONNECTION] Closed {closed} pooled connections for {ip_}:{port_}"
                )

        # C-043: drain checked-out writers (held by request-path
        # coroutines that hadn't yet returned them to the pool). The
        # pool drain above only covers pooled connections; any in-flight
        # request still holding a writer at shutdown would leak the
        # underlying socket fd without this step. We snapshot the set
        # first so a late return-to-pool concurrent with this drain
        # doesn't trip dict-mutation-during-iteration.
        checked_out = list(self._checked_out_writers)
        self._checked_out_writers.clear()
        if checked_out:
            self._logger.info(
                "[CONNECTION] Closing %d checked-out connection(s) on stop()",
                len(checked_out),
            )
        for writer in checked_out:
            try:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
                except (asyncio.TimeoutError, Exception):
                    pass
            except Exception:
                pass

        # C-029: legacy temp SSL file cleanup removed alongside the
        # `_create_ssl_context` deletion. Identity files written by
        # `_load_or_generate_identity` live on disk under `self.keys_dir`
        # and persist across restarts by design.

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

        # B-071: stamp peer hostname on reader+writer for wire-counter
        # accounting (mirrors _create_connection's outbound stamp). Server
        # responses go through the same writer carrying the inbound peer's
        # hostname, so peer_stats[hostname]['bytes_sent'] = bytes this node
        # sent TO that peer (symmetric with the peer's bytes_recv).
        writer._aio_peer_hostname = peer_cfg.hostname
        reader._aio_peer_hostname = peer_cfg.hostname
        self.peer_stats.setdefault(peer_cfg.hostname, {
            "bytes_sent": 0, "bytes_recv": 0,
            "msgs_sent": 0, "msgs_recv": 0,
        })

        self._logger.info(
            "[NETWORKING] Pinned connection from %s hostname=%s system_caller=%s",
            client_addr, peer_cfg.hostname, peer_cfg.system_caller,
        )

        # B-073 Step 8 emit: peer connected. Pre-pin-check failure paths
        # return at lines above BEFORE this point, so port scanners do
        # not produce spurious connect events. ``port`` is the TCP source
        # port from the connecting client (typically ephemeral OS-assigned),
        # NOT the peer's listener port — useful for connection tracing.
        self.plexus._internal_emit(
            "_core/peer/connected",
            hostname=peer_cfg.hostname,
            ip=client_addr[0],
            port=client_addr[1],
            ts=time.time(),
        )

        # B-073 Step 8: track disconnect reason for the finally emit.
        # Each except clause below mutates this; defaults to "normal" on
        # clean loop exit.
        disconnect_reason: str = "normal"

        try:
            # Process requests
            while True:
                try:
                    msg_type, data = await self._receive_message(reader)
                except (ConnectionError, ConnectionResetError):
                    self._logger.debug(
                        f"Connection lost while handling client {client_addr}"
                    )
                    disconnect_reason = "connection_error"
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
                    disconnect_reason = "rce_attempt"
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
                elif msg_type == MSG_SUB_ADVERTISE_ACK:
                    await self._handle_sub_advertise_ack(
                        reader, writer, data, conn_context
                    )
                else:
                    # C-097: distinguish removed/reserved wire IDs from
                    # truly-unknown ones. Reserved IDs 7/8/9/20 were
                    # used by the pre-PR3 notify/request_topic protocol;
                    # a peer sending them is almost certainly running
                    # an older Plexus version — surface that diagnosis
                    # instead of lumping into the generic "unknown"
                    # bucket. Anything outside both sets is real
                    # garbage on the socket (or a peer running a future
                    # version we haven't seen).
                    _REMOVED_RESERVED_MSG_IDS = {7, 8, 9, 20}
                    if msg_type in _REMOVED_RESERVED_MSG_IDS:
                        self._logger.warning(
                            "[MESSAGE] removed-reserved message type %d "
                            "from %s — peer is likely on a pre-PR3 "
                            "Plexus version (legacy notify/request_topic "
                            "wire protocol). Drop the connection.",
                            msg_type, client_addr,
                        )
                        await self._send_error(
                            writer,
                            f"Wire protocol mismatch: message type "
                            f"{msg_type} was removed in PR3 Stage D. "
                            f"Update the sender to publish_event / "
                            f"request_event.",
                        )
                    else:
                        self._logger.warning(
                            f"[MESSAGE] Unknown message type {msg_type} from {client_addr}"
                        )
                        await self._send_error(
                            writer, f"Unknown message type: {msg_type}"
                        )
                    break

        except ConnectionError:
            self._logger.debug(f"Client {client_addr} disconnected")
            disconnect_reason = "connection_error"
        except Exception as e:
            self._logger.exception(f"Error handling client {client_addr}")
            disconnect_reason = "error"
            try:
                # W2-E4: opaque sentinel on the wire. The real exception
                # detail is in the server-side log above; leaking it on
                # the wire helps a pinned-but-compromised peer fingerprint
                # internal state.
                await self._send_error(writer, "internal error")
            except Exception:
                pass
        finally:
            # PR3 Stage C (locked #17): drop advert state on connection
            # close ONLY IF heartbeat has also marked the peer dead.
            # Heartbeat is the source-of-truth — pooled-connection-recycle
            # would over-eagerly drop on every transient pool churn.
            peer_hostname = conn_context.get("peer_hostname")
            try:
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

            # B-073 Step 8 emit: peer disconnected. Only emit when pin
            # check succeeded (peer_hostname set). disconnect_reason was
            # mutated by the inner+outer except handlers above; defaults
            # to "normal" on clean loop exit.
            if peer_hostname:
                self.plexus._internal_emit(
                    "_core/peer/disconnected",
                    hostname=peer_hostname,
                    reason=disconnect_reason,
                    ts=time.time(),
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

            # R2-CC-3: self-impersonation gate (locked #15). Sibling
            # handlers (_handle_publish_event, _handle_request_event,
            # _handle_sub_advertise, _handle_sub_delta) all check this;
            # _handle_execute previously did not, allowing a pinned peer
            # to attribute calls to our own hostname.
            if self._self_impersonation_check(
                author_host, writer, "MSG_EXECUTE"
            ):
                return

            # R2-CC-3: pin-vs-wire identity check (C-106). If the cert-pin
            # already set peer_hostname, the wire-claimed author_host must
            # match. Without this, a compromised pinned peer can spoof
            # author_host to a third-party node's hostname.
            if author_host:
                pinned = conn_context.get("peer_hostname")
                if pinned and pinned != author_host:
                    self._logger.warning(
                        "[EXECUTE] anti-spoof: pinned peer %r vs "
                        "wire-claimed author_host %r — drop",
                        pinned, author_host,
                    )
                    return
                conn_context.setdefault("peer_hostname", author_host)

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
            # Pass args as a single object — Plexus.execute unpacks internally
            if isinstance(args, list):
                args = tuple(args)
            result = await self.plexus.execute(
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
                    # R2-CC-2: bounded drain. TODO: surface as config knob.
                    await asyncio.wait_for(writer.drain(), timeout=30.0)
                    self._count_sent(writer, len(header) + len(chunk_data))
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
                # R2-CC-2: bounded drain. TODO: surface as config knob.
                await asyncio.wait_for(writer.drain(), timeout=30.0)
                self._count_sent(writer, len(header) + len(payload))

            try:
                result_type = type(result).__name__
            except Exception:
                result_type = "unknown"

            await self._send_end_stream(writer)
            self._logger.info(
                f"[EXECUTE] Completed: result_type={result_type}, size_bytes={len(payload)}"
            )

        except RequestException as e:
            # W1-A2: preserve RequestException (and its subclass
            # NetworkRequestException) identity across the wire. Plugin
            # authors catching specific RequestException subclasses on the
            # caller side previously saw a NetworkRequestException(str(...))
            # wrap; pickling the original instance fixes that.
            #
            # R3-MM-4 review follow-up: guard the error-frame drain. If the
            # peer is stuck, _send_error_pickled raises TimeoutError from
            # its internal wait_for(writer.drain(), 30s). Without this
            # guard the TimeoutError escapes to _handle_client's outer
            # except, which would attempt yet another error frame on the
            # same stuck writer (a third 30s wait). Close best-effort and
            # swallow on timeout.
            try:
                await self._send_error_pickled(writer, e)
            except asyncio.TimeoutError:
                try:
                    writer.close()
                except Exception:
                    pass
        except Exception as e:
            self._logger.exception("Exception in _handle_execute")
            # R3-MM-4: if the caught exception is asyncio.TimeoutError from
            # one of the bounded wait_for(writer.drain(), timeout=30.0) calls
            # above, the peer is already stuck. Calling _send_error_pickled
            # here would route through _send_message -> another
            # wait_for(writer.drain(), timeout=30.0) on the SAME stuck writer,
            # incurring a second 30-second hang (60s total per stuck peer).
            # Close the writer locally instead and bail out.
            if isinstance(e, asyncio.TimeoutError):
                # R3-MM-4: drain timed out on a stuck peer. Do NOT call
                # _send_error_pickled (it would invoke _send_message, which
                # awaits another wait_for(writer.drain(), 30s) on the same
                # stuck writer — doubling the hang to 60s). Close the
                # writer best-effort without awaiting wait_closed(), since
                # wait_closed() would hang waiting for the same stuck
                # FIN/ACK that already timed out on drain. Kernel-side TCP
                # close completes asynchronously.
                try:
                    writer.close()
                except Exception:
                    pass
                return
            await self._send_error_pickled(
                writer, NetworkRequestException(str(e))
            )

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

            # R2-CC-3: self-impersonation gate (locked #15). Mirrors the
            # check in sibling handlers (publish/request/advert/delta).
            if self._self_impersonation_check(
                author_host, writer, "MSG_EXECUTE_STREAM"
            ):
                return

            # R2-CC-3: pin-vs-wire identity check (C-106). Reject when the
            # cert-pinned hostname disagrees with the wire-claimed
            # author_host.
            if author_host:
                pinned = conn_context.get("peer_hostname")
                if pinned and pinned != author_host:
                    self._logger.warning(
                        "[EXECUTE_STREAM] anti-spoof: pinned peer %r vs "
                        "wire-claimed author_host %r — drop",
                        pinned, author_host,
                    )
                    return
                conn_context.setdefault("peer_hostname", author_host)

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
            # Pass args as a single object — Plexus.execute_stream unpacks internally
            if isinstance(args, list):
                args = tuple(args)
            agen = self.plexus.execute_stream(
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
                            # R2-CC-2: bounded drain. TODO: surface as config knob.
                            await asyncio.wait_for(writer.drain(), timeout=30.0)
                            self._count_sent(writer, len(header) + len(chunk_data))
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
                        # R2-CC-2: bounded drain. TODO: surface as config knob.
                        await asyncio.wait_for(writer.drain(), timeout=30.0)
                        self._count_sent(writer, len(header) + len(payload))
                    # Mark end of this item so receiver knows where item boundaries are
                    item_end_header = struct.pack(">IB", 1, MSG_STREAM_ITEM_END)
                    writer.write(item_end_header)
                    # R2-CC-2: bounded drain. TODO: surface as config knob.
                    await asyncio.wait_for(writer.drain(), timeout=30.0)
                    self._count_sent(writer, len(item_end_header))
                    sent_items += 1
                except Exception as e:
                    self._logger.exception("Failed to send stream chunk")
                    # R3-MM-4 (mirror of _handle_execute fix): if the failure
                    # itself was a drain timeout on this writer, retrying
                    # another drain via the error-frame send path would just
                    # block another 30s on the same stuck peer (and a third
                    # 30s in the outer except's _send_stream_chunk). Close
                    # the writer best-effort and bail out — wait_closed()
                    # would also hang on the stuck FIN/ACK.
                    if isinstance(e, asyncio.TimeoutError):
                        try:
                            writer.close()
                        except Exception:
                            pass
                        return
                    err_obj = ("__STREAM_ERROR__", str(e))
                    err_payload = pickle.dumps(err_obj)
                    chunk_length = len(err_payload) + 1
                    header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                    writer.write(header + err_payload)
                    # R2-CC-2: bounded drain. TODO: surface as config knob.
                    await asyncio.wait_for(writer.drain(), timeout=30.0)
                    self._count_sent(writer, len(header) + len(err_payload))
                    # F5 fix: MUST send MSG_STREAM_ITEM_END after the error
                    # chunk so the client decoder's sentinel check in the
                    # MSG_STREAM_ITEM_END branch fires (lines 3083-3105 of
                    # this file). Without ITEM_END, the chunk gets buffered
                    # and yielded as a normal final item via MSG_END_STREAM
                    # path, with the client's caller seeing the error tuple
                    # as data and no exception.
                    item_end_header = struct.pack(">IB", 1, MSG_STREAM_ITEM_END)
                    writer.write(item_end_header)
                    # R2-CC-2: bounded drain. TODO: surface as config knob.
                    await asyncio.wait_for(writer.drain(), timeout=30.0)
                    self._count_sent(writer, len(item_end_header))
                    break

            await self._send_end_stream(writer)
            self._logger.info(f"[EXECUTE_STREAM] Completed: items_sent={sent_items}")

        except Exception as e:
            self._logger.exception("Exception while streaming")
            # R3-MM-4 (mirror of _handle_execute fix): if the streaming
            # body failed because of a drain timeout, the writer is
            # stuck. Trying to deliver the __STREAM_EXCEPTION__ frame
            # would invoke another wait_for(writer.drain(), 30s) chain
            # in _send_stream_chunk / _send_end_stream. Close best-effort
            # and bail.
            if isinstance(e, asyncio.TimeoutError):
                try:
                    writer.close()
                except Exception:
                    pass
                return
            try:
                err_obj = ("__STREAM_EXCEPTION__", str(e))
                await self._send_stream_chunk(writer, err_obj)
                # F5 fix: MSG_STREAM_ITEM_END before MSG_END_STREAM so the
                # client decoder's sentinel check fires (line 3223+
                # empty-payload ITEM_END branch).
                item_end_header = struct.pack(">IB", 1, MSG_STREAM_ITEM_END)
                writer.write(item_end_header)
                # R2-CC-2: bounded drain. TODO: surface as config knob.
                await asyncio.wait_for(writer.drain(), timeout=30.0)
                self._count_sent(writer, len(item_end_header))
                await self._send_end_stream(writer)
            except Exception:
                # On secondary failure, close the writer so the OS
                # socket doesn't linger until GC.
                try:
                    writer.close()
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
                requester_id == self.plexus.hostname
                or requester_id in self.plexus.plugins_by_uuid
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
            plugin, endpoint, node = await self.plexus.find_endpoint(
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
                "hostname": self.plexus.hostname,
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
            # gate (core.py:2104-2107). Tag-search bypassed it
            # entirely before this guard.
            for plugin in self.plexus.plugins.values():
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
                {"hostname": self.plexus.hostname, "endpoints": endpoints},
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

        Mirrors Plexus's nested ``_matches_remote_node`` /
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
        happens OUTBOUND-side (per PLAN H).

        R2-KK-3: validates the four filter fields (hosts, blocked_hosts,
        authors, blocked_authors) as well — each must be None, a str,
        or a list of str. A peer sending ``hosts=42`` or
        ``blocked_hosts={}`` used to pass this gate and reach the
        filter logic where it would either raise unexpectedly or
        silently misclassify the advert.
        """
        if not isinstance(advert, dict):
            return False
        sub_uuid = advert.get("sub_uuid")
        topic = advert.get("topic")
        if not isinstance(sub_uuid, str) or not sub_uuid:
            return False
        if not isinstance(topic, str) or not topic:
            return False

        # Filter-field type validation. Accept None / str / list-of-str.
        hosts = advert.get("hosts")
        if not (hosts is None or isinstance(hosts, str)
                or (isinstance(hosts, list)
                    and all(isinstance(x, str) for x in hosts))):
            return False
        blocked_hosts = advert.get("blocked_hosts")
        if not (blocked_hosts is None or isinstance(blocked_hosts, str)
                or (isinstance(blocked_hosts, list)
                    and all(isinstance(x, str) for x in blocked_hosts))):
            return False
        authors = advert.get("authors")
        if not (authors is None or isinstance(authors, str)
                or (isinstance(authors, list)
                    and all(isinstance(x, str) for x in authors))):
            return False
        blocked_authors = advert.get("blocked_authors")
        if not (blocked_authors is None or isinstance(blocked_authors, str)
                or (isinstance(blocked_authors, list)
                    and all(isinstance(x, str) for x in blocked_authors))):
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

    async def _detect_peer_restart(
        self, peer_hostname: str, inbound_session_id: str
    ) -> None:
        """C-110: surfaces a peer restart so we re-arm our snapshot
        gate.  Called by the inbound advert / delta handlers with
        the peer's announced session_id. If the peer's session_id
        differs from what we last saw, the peer restarted between
        contacts; we clear ``_snapshot_sent[peer]`` so the next
        reciprocal-exchange trigger re-sends our snapshot. Without
        this, a fast TCP reconnect within heartbeat_interval (no
        heartbeat-strikes timeout, no _drop_peer_advert_state)
        would leave a stale "snapshot already sent" gate and the
        restarted peer would never receive our subscription set.

        First-contact handling: when we have no recorded
        ``_peer_session_ids`` entry for the peer (prev_session is
        None), it's our first inbound payload from this peer.
        Adopt the session_id, AND patch any None-sentinel entry in
        ``_snapshot_sent`` (left by ``_perform_initial_exchange``
        that sent BEFORE we learned the peer's session_id) to the
        now-known value. Without that patch, the next inbound
        payload would mismatch a None-sentinel against a real
        session_id and spuriously re-arm the gate even though no
        restart actually happened.
        """
        restart_detected = False
        async with self._adverts_struct_lock:
            prev_session = self._peer_session_ids.get(peer_hostname)
            if prev_session == inbound_session_id:
                return
            self._peer_session_ids[peer_hostname] = inbound_session_id
            if prev_session is None:
                # First contact. Adopt the session_id into any
                # None-sentinel snapshot_sent entry so subsequent
                # inbound payloads with the same session_id
                # short-circuit at the equality check above.
                if (
                    peer_hostname in self._snapshot_sent
                    and self._snapshot_sent[peer_hostname] is None
                ):
                    self._snapshot_sent[peer_hostname] = inbound_session_id
                return
            # Session changed: peer restarted. Drop our snapshot-sent
            # gate so the next reciprocal-exchange (triggered by the
            # incoming payload itself, via _maybe_reciprocal_exchange)
            # actually re-sends instead of short-circuiting.
            self._snapshot_sent.pop(peer_hostname, None)
            restart_detected = True
        if restart_detected:
            self._logger.info(
                "[SESSION] peer %s session_id changed (%s -> %s) — "
                "re-arming snapshot-sent gate",
                peer_hostname,
                prev_session,
                inbound_session_id,
            )

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
        if author_host == self.plexus.hostname:
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
        if not author_host or author_host == self.plexus.hostname:
            return
        if author_host in self._snapshot_sent:
            return
        node = next(
            (n for n in list(self.nodes) if n.hostname == author_host),
            None,
        )
        if node is not None:
            # C-116: force=True bypasses the lexicographic tiebreak —
            # the peer already sent to us so the duplicate-snapshot
            # race the tiebreak was preventing cannot happen here.
            self.plexus._spawn_fire_and_forget(
                self._spawn_initial_exchange(node, force=True),
                name=f"spawn_init_exch<-{author_host}",
            )
            return
        # Fallback: client-only peer not yet in node table.
        peer_ip = self._safe_peer_ip(writer)
        if peer_ip:
            self.plexus._spawn_fire_and_forget(
                self._spawn_initial_exchange_for_ip(
                    peer_ip, author_host, force=True
                ),
                name=f"spawn_init_exch_ip<-{author_host}",
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
            # C-106: pin-vs-wire identity check. If the cert-pin already
            # set peer_hostname, the wire-claimed author_host must match.
            if author_host:
                pinned = conn_context.get("peer_hostname")
                if pinned and pinned != author_host:
                    self._logger.warning(
                        "[PUBLISH_EVENT] anti-spoof: pinned peer %r vs "
                        "wire-claimed author_host %r — drop",
                        pinned, author_host,
                    )
                    return
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
                all_subs = await self.plexus.topic_registry.find_all(topic)
            except Exception:
                self._logger.exception(
                    "[PUBLISH_EVENT] find_all failed for topic %r", topic
                )
                return

            for sub in all_subs:
                if sub.plugin_uuid not in self.plexus.plugins_by_uuid:
                    continue
                if not self.plexus._sub_accepts_remote_publisher(
                    sub, author_host, author
                ):
                    continue
                if not self.plexus._sub_accepts_author(sub, author):
                    continue
                try:
                    await self.plexus._fanout_sub(
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
                # C-106: pin-vs-wire identity check.
                pinned = conn_context.get("peer_hostname")
                if pinned and pinned != author_host:
                    self._logger.warning(
                        "[REQUEST_EVENT] anti-spoof: pinned peer %r vs "
                        "wire-claimed author_host %r — drop",
                        pinned, author_host,
                    )
                    await self._send_error_pickled(
                        writer,
                        NetworkRequestException(
                            "anti-spoof: author_host mismatch with pinned peer"
                        ),
                    )
                    return
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
                all_subs = await self.plexus.topic_registry.find_all(topic)
            except Exception as exc:
                self._logger.exception(
                    "[REQUEST_EVENT] find_all failed for topic %r", topic
                )
                await self._send_error_pickled(writer, NetworkRequestException(str(exc)))
                return

            local_match = None
            for sub in all_subs:
                if sub.plugin_uuid not in self.plexus.plugins_by_uuid:
                    continue
                if not self.plexus._sub_accepts_remote_publisher(
                    sub, author_host, author
                ):
                    continue
                if not self.plexus._sub_accepts_author(sub, author):
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
                request = await self.plexus._fanout_sub(
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
                # B-073 Session 2 Step 3: done-callback eviction. Was
                # ``await request.set_collected()`` (which set a flag for
                # the now-removed cleanup_requests reap). Migrated to
                # direct sync pop. Idempotent under ``pop(key, None)``;
                # the producer's finally in ``_process_request`` also
                # pops on completion. Try/except dropped — sync ``pop``
                # cannot raise (the only failure mode of the old async
                # path was an event-loop scheduling issue, gone now).
                self.plexus.requests.pop(request.id, None)
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
                # C-106: pin-vs-wire identity check.
                pinned = conn_context.get("peer_hostname")
                if pinned and pinned != author_host:
                    self._logger.warning(
                        "[REQUEST_EVENT_STREAM] anti-spoof: pinned peer %r "
                        "vs wire-claimed author_host %r — drop",
                        pinned, author_host,
                    )
                    await self._send_error_pickled(
                        writer,
                        NetworkRequestException(
                            "anti-spoof: author_host mismatch with pinned peer"
                        ),
                    )
                    return
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
                all_subs = await self.plexus.topic_registry.find_all(topic)
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
                if sub.plugin_uuid not in self.plexus.plugins_by_uuid:
                    continue
                if not self.plexus._sub_accepts_remote_publisher(
                    sub, author_host, author
                ):
                    continue
                if not self.plexus._sub_accepts_author(sub, author):
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
                target_plugin, endpoint, _ = await self.plexus.find_endpoint(
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

            from .utils import Event as _Event  # local import to avoid cycle

            ts = timestamp if isinstance(timestamp, (int, float)) else 0.0
            sub_id_for_event = (
                local_match.declared_id
                if local_match.declared_id is not None
                else local_match.sub_uuid
            )

            # Build Event from wire metadata (NOT via Event.from_request).
            # First chunk wraps; subsequent chunks raw.
            #
            # W2-F4: chunks_sent tracks whether ANY MSG_STREAM_CHUNK frame
            # has been written. Outer except clauses (TimeoutError /
            # RequestException / Exception) read it to decide whether to
            # send MSG_ERROR (legal pre-stream) or close the writer (the
            # only legal action after MSG_STREAM_CHUNK without an
            # intervening MSG_END_STREAM). Using a 1-element list because
            # nonlocal across nested async function and outer except
            # would otherwise need an explicit declaration.
            chunks_sent = [False]

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
                            # W2-F4: mark chunk emission AFTER the
                            # send completes so the outer except
                            # clauses know a chunk landed on the wire.
                            chunks_sent[0] = True
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
                                self.plexus.sync_dispatcher.executor,
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
                            # W2-F4: mark chunk emission AFTER the
                            # send completes so the outer except
                            # clauses know a chunk landed on the wire.
                            chunks_sent[0] = True
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
                # W2-F4: if any MSG_STREAM_CHUNK frame has already gone
                # out, the framing contract forbids MSG_ERROR after
                # chunks without an intervening MSG_END_STREAM. Closing
                # the writer is the only legal action for the
                # mid-stream-timeout case; pooling a partially-written
                # connection corrupts the next caller. Pre-stream
                # timeouts (no chunks yet) still get a proper
                # MSG_ERROR.
                if chunks_sent[0]:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                else:
                    await self._send_error_pickled(
                        writer,
                        RequestException(
                            f"request_event_stream timed out after {timeout}s"
                        ),
                    )
            except RequestException as exc:
                # W2-F4: same framing concern for explicit
                # RequestException raises from the handler.
                if chunks_sent[0]:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                else:
                    await self._send_error_pickled(writer, exc)
            except Exception as exc:
                self._logger.exception("[REQUEST_EVENT_STREAM] iteration crashed")
                # W2-F4 (S1 follow-up): same framing concern — if any
                # MSG_STREAM_CHUNK has gone out, MSG_ERROR is illegal
                # without an intervening MSG_END_STREAM. Close the
                # writer instead of corrupting the pooled connection.
                if chunks_sent[0]:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                else:
                    await self._send_error_pickled(
                        writer, RequestException(str(exc))
                    )

        except Exception as exc:
            # V1 follow-up: this outer except is reachable only on
            # pre-stream crashes (the inner `_iterate_and_send` try at
            # ~line 2855 wraps the entire chunk-sending loop and now
            # gates its three except clauses on ``chunks_sent[0]``). If
            # any future edit moves chunk-sending code OUTSIDE the inner
            # try, MIRROR the ``if chunks_sent[0]: close-only else: send_error``
            # framing-invariant guard here — sending MSG_ERROR mid-stream
            # corrupts the pooled connection for the next caller.
            self._logger.exception("[REQUEST_EVENT_STREAM] handler crashed")
            try:
                await self._send_error_pickled(
                    writer, NetworkRequestException(str(exc))
                )
            except Exception:
                pass
        except BaseException:
            # R2-AA-3: catch CancelledError / GeneratorExit / etc. so a
            # cancel mid-stream does not leave the client connection
            # dangling without a termination frame. Mirror the framing
            # invariant from the inner except clauses: once any
            # MSG_STREAM_CHUNK has gone out, MSG_ERROR is illegal without
            # an intervening MSG_END_STREAM — close the writer instead.
            # Pre-stream cancel (no chunks) is also handled by close to
            # ensure the peer reader sees connection teardown rather than
            # an indefinite read hang.
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            raise

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

            # C-106: pin-vs-wire identity check. conn_context["peer_hostname"]
            # is set unconditionally by _handle_client from the mTLS-pinned
            # cert (line ~1455). A wire-claimed author_host that does NOT
            # match the pinned identity is a spoof attempt — reject hard.
            # The setdefault preserves the pinned value for downstream
            # call sites that read peer_hostname from conn_context.
            pinned = conn_context.get("peer_hostname")
            if pinned and pinned != author_host:
                self._logger.warning(
                    "[SUB_ADVERTISE] anti-spoof: pinned peer %r vs wire-"
                    "claimed author_host %r — drop",
                    pinned, author_host,
                )
                return
            conn_context.setdefault("peer_hostname", author_host)

            # C-110: detect peer restart via session_id mismatch.
            # Older peers omit this field — treat as unchanged (skip
            # the detection). When present, a different session_id
            # than we have on record means the peer restarted; clear
            # our outbound snapshot-sent gate so the next reciprocal
            # exchange re-sends our snapshot to the restarted peer.
            inbound_session = payload_dict.get("our_session_id")
            if isinstance(inbound_session, str) and inbound_session:
                await self._detect_peer_restart(author_host, inbound_session)

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
                # R2-CC-8: previously sent MSG_ERROR back on the inbound
                # one-shot server-side writer. That frame sits in the
                # kernel buffer and corrupts framing for the next reader
                # on the same socket. Log only — sender's ack_timeout
                # fall-back path still ends the wait (just slower).
                self._logger.warning(
                    "[SUB_ADVERTISE] rejecting oversized advert from %s: "
                    "%d entries exceeds cap %d",
                    author_host, len(subs_payload), MAX_ADVERT_SUBS_PER_PEER,
                )
                return

            # Atomic purge + reinsert (per locked #5: empty list → {}).
            # Session 4: accumulate processed_uuids for the ack frame.
            # C-040: snapshot prior state before the clear so a mid-iter
            # AdvertSub-construction raise can roll back the table to a
            # consistent state instead of leaving it half-empty. Build
            # the new entries in a staging dict first, then commit
            # atomically — that way an exception inside the for-loop
            # leaves the live tables untouched.
            processed_uuids: List[str] = []
            async with self._adverts_struct_lock:
                staged_inbound: Dict[str, AdvertSub] = {}
                staged_global: Dict[Tuple[str, str], AdvertSub] = {}
                try:
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
                        staged_inbound[sub.sub_uuid] = sub
                        staged_global[(author_host, sub.sub_uuid)] = sub
                        processed_uuids.append(sub.sub_uuid)
                except Exception:
                    # Stage failed mid-iter; do NOT commit. Live tables
                    # remain whatever they were before. Re-raise so the
                    # outer except in this handler logs + notifies peer.
                    self._logger.exception(
                        "[SUB_ADVERTISE] stage build failed mid-iter for %r; "
                        "live state untouched",
                        author_host,
                    )
                    raise
                # Commit: now that staging completed without raising, swap
                # the staged dicts into the live tables atomically (under
                # the same struct_lock).
                self._inbound_adverts[author_host] = staged_inbound
                self._inbound_global_order = {
                    k: v
                    for k, v in self._inbound_global_order.items()
                    if k[0] != author_host
                }
                self._inbound_global_order.update(staged_global)

            self._logger.debug(
                "[SUB_ADVERTISE] recorded %d subs from %s",
                len(subs_payload), author_host,
            )

            # Session 4 (v0.27.0): schedule ack BEFORE the reciprocal
            # exchange await so the ack-task is registered in the loop
            # immediately on lock release. peer_ip was captured above
            # via _safe_peer_ip(writer) for anti-spoof; reuse for the
            # helper's fallback. No-op when nothing was ingested.
            if processed_uuids:
                self.plexus._spawn_fire_and_forget(
                    self._send_advert_ack_to(
                        author_host, peer_ip, processed_uuids,
                    ),
                    name=f"advert_ack<-{author_host}",
                )

            # Reciprocal: if we haven't yet advertised to this peer,
            # send our snapshot back (locked #7).
            await self._maybe_reciprocal_exchange(author_host, writer)

        except Exception as exc:
            # R2-CC-8: previously sent MSG_ERROR back on the inbound
            # one-shot server-side writer (C-039). That frame would
            # linger in the connection's kernel buffer and corrupt
            # framing for the next reader on the same socket. Log only;
            # sender recovers via ack_timeout. The exception is already
            # captured by self._logger.exception below.
            self._logger.exception(
                "[SUB_ADVERTISE] handler crashed: %s", exc
            )

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

            # C-106: pin-vs-wire identity check. See _handle_sub_advertise
            # for the rationale — same anti-spoof guard applies here.
            pinned = conn_context.get("peer_hostname")
            if pinned and pinned != author_host:
                self._logger.warning(
                    "[SUB_DELTA] anti-spoof: pinned peer %r vs wire-"
                    "claimed author_host %r — drop",
                    pinned, author_host,
                )
                return
            conn_context.setdefault("peer_hostname", author_host)

            # C-110: peer-restart detection. See _handle_sub_advertise.
            inbound_session = payload_dict.get("our_session_id")
            if isinstance(inbound_session, str) and inbound_session:
                await self._detect_peer_restart(author_host, inbound_session)

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
            # Session 4: only kind="add" success paths produce an ack
            # (sender's _outbound_adverts only tracks sent_at on add
            # operations; remove paths delete the tracking entry, so an
            # ack would have nothing to update).
            processed_uuid: Optional[str] = None
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
                    processed_uuid = sub.sub_uuid
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
                        # C-041: log WARNING so peer-state divergence (sender
                        # thinks the sub exists, we never registered it) is
                        # visible. The skip stays by-design idempotent, but
                        # operators now see "remove of uuid we don't know"
                        # in logs instead of silent.
                        self._logger.warning(
                            "[SUB_DELTA] remove of unknown sub_uuid=%r from "
                            "peer %r — idempotent skip (per_peer_known=%s)",
                            sub_uuid, author_host, per_peer is not None,
                        )
                    else:
                        per_peer.pop(sub_uuid, None)
                        self._inbound_global_order.pop((author_host, sub_uuid), None)

            # Session 4 (v0.27.0): schedule ack BEFORE the reciprocal
            # exchange await. Only "add" success generates an ack;
            # "remove" paths intentionally leave processed_uuid=None.
            if processed_uuid is not None:
                self.plexus._spawn_fire_and_forget(
                    self._send_advert_ack_to(
                        author_host, peer_ip, [processed_uuid],
                    ),
                    name=f"advert_ack_delta<-{author_host}",
                )

            await self._maybe_reciprocal_exchange(author_host, writer)

        except Exception as exc:
            # R2-CC-8: previously sent MSG_ERROR back on the inbound
            # one-shot server-side writer (C-039). That frame would
            # corrupt framing for the next reader on the same socket.
            # Log only — sender recovers via ack_timeout.
            self._logger.exception(
                "[SUB_DELTA] handler crashed: %s", exc
            )

    # ── Session 4 (v0.27.0) — sub-advert ack protocol ─────────────

    async def _send_advert_ack_to(
        self,
        peer_hostname: str,
        fallback_peer_ip: Optional[str],
        processed_uuids: List[str],
    ) -> None:
        """Send MSG_SUB_ADVERTISE_ACK to peer_hostname via our outbound
        connection. Best-effort: silent skip on send failure or unknown
        peer. Called as a fire-and-forget task from _handle_sub_advertise
        / _handle_sub_delta after lock release.

        The ack acknowledges that we successfully INGESTED an inbound
        snapshot or delta. The sender's heartbeat loop tracks per-sub
        ``sent_at`` against ``2 * heartbeat_interval`` and resends on
        timeout, then marks ``state="ack_timeout"`` if no ack arrived
        before the second tick. The sender's ``retry_count`` is bumped
        once per resend cycle (see ``_resend_and_bump_retry``); a
        successful ack received here on the sender side transitions
        the corresponding ``_outbound_adverts[peer][sub_uuid]`` entry
        to ``state="acked"`` and clears its retry counter. So a "silent
        skip" here is the failure mode the sender's heartbeat scan
        actively recovers from — this isn't a legacy fire-and-forget,
        it's the receiver half of the Session-4 (v0.27.0) ack protocol.

        IP resolution order (NAT-correct):
          1. peers_by_endpoint by hostname (mTLS config, authoritative)
          2. self.nodes by hostname (discovered fallback)
          3. fallback_peer_ip from inbound writer (last resort)
        """
        if not peer_hostname or not processed_uuids:
            return
        # is_ready guard: detached create_task may fire between
        # _handle_sub_advertise's spawn and stop() flipping is_ready.
        # Without this guard, the helper would call _get_connection on
        # a half-torn-down pool. Mirrors _resend_and_bump_retry.
        if not getattr(self, "is_ready", False):
            return
        peer_ip: Optional[str] = None
        for (cfg_ip, _cfg_port), peer_cfg in self.peers_by_endpoint.items():
            if peer_cfg.hostname == peer_hostname:
                peer_ip = cfg_ip
                break
        if peer_ip is None:
            node = next(
                (n for n in list(self.nodes) if n.hostname == peer_hostname),
                None,
            )
            if node is not None and node.IP:
                peer_ip = node.IP
        if peer_ip is None:
            peer_ip = fallback_peer_ip
        if peer_ip is None:
            self._logger.debug(
                "[ADVERT_ACK] no IP for peer_hostname=%r — skip",
                peer_hostname,
            )
            return

        reader = None
        writer = None
        send_ok = False
        try:
            reader, writer = await self._get_connection(peer_ip)
            await self._send_message(
                writer,
                MSG_SUB_ADVERTISE_ACK,
                {
                    "author_host": self.plexus.hostname,
                    "processed_uuids": list(processed_uuids),
                },
            )
            send_ok = True
        except Exception:
            self._logger.debug(
                "[ADVERT_ACK] send to %r (%s) failed",
                peer_hostname, peer_ip, exc_info=True,
            )
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
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

    async def _handle_sub_advertise_ack(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        data: dict,
        conn_context: Dict[str, Any],
    ) -> None:
        """MSG_SUB_ADVERTISE_ACK: peer confirms ingestion of our advert.
        Updates _outbound_adverts[peer][sub_uuid].acked_at / state. No
        reply. Ack-vs-disconnect: if peer disconnected post-ack-send,
        _drop_peer_advert_state has cleared _outbound_adverts[peer] —
        defensive None-skip; no raise.
        """
        try:
            payload_dict = data if isinstance(data, dict) else {}
            author_host = payload_dict.get("author_host")
            if not isinstance(author_host, str) or not author_host:
                self._logger.debug(
                    "[SUB_ADVERTISE_ACK] invalid author_host=%r", author_host
                )
                return
            # Anti-spoof: ack author_host MUST match pin-checked
            # peer_hostname (set unconditionally at line ~1350 BEFORE
            # dispatch loop). Reject on missing OR mismatch.
            peer_hostname = conn_context.get("peer_hostname")
            if not peer_hostname or peer_hostname != author_host:
                self._logger.warning(
                    "[SUB_ADVERTISE_ACK] anti-spoof: pinned peer %r vs "
                    "author_host %r — drop",
                    peer_hostname, author_host,
                )
                return
            processed_uuids = payload_dict.get("processed_uuids", []) or []
            if not isinstance(processed_uuids, list):
                # C-042 PARTIAL: log the rejection so a peer sending a
                # malformed payload is visible in logs rather than
                # silently dropped. WARNING level — this is a protocol
                # violation that breaks ack tracking for the affected
                # subs (those entries stay "pending" until ack_timeout).
                self._logger.warning(
                    "[SUB_ADVERTISE_ACK] non-list processed_uuids=%r from "
                    "peer %r - drop",
                    type(processed_uuids).__name__, author_host,
                )
                return

            # C-012: monotonic timebase for acked_at — local-only field,
            # paired with sent_at writes that also use monotonic. NTP
            # wall-clock steps must not perturb the resend/timeout math.
            ts = time.monotonic()
            async with self._adverts_struct_lock:
                peer_table = self._outbound_adverts.get(author_host)
                if peer_table is None:
                    self._logger.debug(
                        "[SUB_ADVERTISE_ACK] peer %r dropped pre-ack — skip",
                        author_host,
                    )
                    return
                for sub_uuid in processed_uuids:
                    if not isinstance(sub_uuid, str):
                        continue
                    sub = peer_table.get(sub_uuid)
                    if sub is None:
                        continue  # Sub removed between send and ack
                    sub.acked_at = ts
                    sub.state = "acked"
        except Exception:
            self._logger.exception("[SUB_ADVERTISE_ACK] handler crashed")

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
        in Plexus acquire topic_registry._lock first then reach
        _advert_locks[peer] via send_sub_delta_remote. To avoid a cycle,
        snapshot the local subs list BEFORE acquiring _advert_locks[peer].

        is_ready guard mirrors broadcast_local_sub_added/removed and
        _check_advert_ack_timeouts/_resend_and_bump_retry: callers may
        invoke this between Plexus.start_framework's network bring-up and
        stop()'s shutdown sequence; without the guard a post-stop call
        would touch torn-down structures.
        """
        if not getattr(self, "is_ready", False):
            return
        try:
            subs = await self.plexus.topic_registry.list_local_subs()
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
                # Session 4 (v0.27.0): stamp sent_at at build time. If
                # _send_message fails below, the existing rollback at
                # line ~3088 (_outbound_adverts.pop) wipes the entries —
                # no orphan sent_at remains. The narrow race where send
                # itself takes >2*heartbeat_interval is theoretical
                # (sends complete in milliseconds; heartbeat is 10s
                # default) and is documented as accepted in the plan.
                # C-012: monotonic timebase, paired with the read in
                # _check_advert_ack_timeouts.
                ts = time.monotonic()
                projected = {
                    s.sub_uuid: AdvertSub(
                        sub_uuid=s.sub_uuid,
                        topic_pattern=s.topic_pattern,
                        hosts=s.hosts,
                        blocked_hosts=s.blocked_hosts,
                        authors=s.authors,
                        blocked_authors=s.blocked_authors,
                        sent_at=ts,
                        state="pending",
                    )
                    for s in filtered
                }
                self._outbound_adverts[peer_hostname] = projected

            wire_payload = {
                "author_host": self.plexus.hostname,
                # C-110: stamp our session_id so the receiver can
                # detect a same-hostname restart and re-arm its
                # snapshot-sent gate.
                "our_session_id": self.session_id,
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
        # R2-CC-5: is_ready guard. Mirrors _send_advert_ack_to /
        # _resend_and_bump_retry / broadcast_local_sub_*. A detached
        # create_task spawning this method can fire between stop()
        # flipping is_ready and the connection pool's full teardown;
        # without this guard, _get_connection would be invoked on a
        # half-destroyed NetworkManager.
        if not getattr(self, "is_ready", False):
            return
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
                    existing = outbound_for_peer.get(sub.sub_uuid)
                    # C-107: ack_timeout is a recoverable state, not a
                    # terminal one. The previous code early-returned on
                    # any existing entry regardless of state, which
                    # meant a set_enabled toggle (unsubscribe followed
                    # by resubscribe, or any operator action that
                    # triggers re-add for an ack_timeout sub) silently
                    # no-op'd: the entry stayed in ack_timeout, the
                    # _check_advert_ack_timeouts heartbeat scan only
                    # touches state=='pending' so the sub never recovered.
                    # Treat the add of an ack_timeout entry as a re-arm:
                    # rewrite to a fresh pending entry (new sent_at,
                    # cleared retry_count) and fall through to the
                    # wire-send branch as if it were a new add.
                    if existing is not None and existing.state != "ack_timeout":
                        return  # already advertised
                    outbound_for_peer = self._outbound_adverts.setdefault(
                        peer_hostname, {}
                    )
                    # Session 4 (v0.27.0): stamp sent_at on the new
                    # entry. Send-failure rollback at line ~3180
                    # (outbound_now.pop) wipes the entry on failure.
                    # C-012: monotonic timebase, paired with the read
                    # in _check_advert_ack_timeouts.
                    outbound_for_peer[sub.sub_uuid] = AdvertSub(
                        sub_uuid=sub.sub_uuid,
                        topic_pattern=sub.topic_pattern,
                        hosts=sub.hosts,
                        blocked_hosts=sub.blocked_hosts,
                        authors=sub.authors,
                        blocked_authors=sub.blocked_authors,
                        sent_at=time.monotonic(),
                        state="pending",
                    )
                else:
                    if sub.sub_uuid not in outbound_for_peer:
                        return  # never advertised
                    # C-011: capture the prior AdvertSub so a send-failure
                    # rollback can restore it. Without this, the
                    # _outbound_adverts entry is gone before the wire send
                    # and a failed remove leaves the peer holding the sub
                    # in _inbound_adverts indefinitely (no self-heal). The
                    # captured value is consumed in the except-branch below.
                    prior_advert = self._outbound_adverts[peer_hostname][
                        sub.sub_uuid
                    ]
                    del self._outbound_adverts[peer_hostname][sub.sub_uuid]

            if kind == "add":
                wire_subs = [self._serialize_local_sub_for_peer(sub)]
            else:
                wire_subs = [{"sub_uuid": sub.sub_uuid}]

            wire_payload = {
                "author_host": self.plexus.hostname,
                # C-110: stamp our session_id.
                "our_session_id": self.session_id,
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
                        else:
                            # C-011: restore the prior AdvertSub captured
                            # before the delete above. Without this, the
                            # peer still has the sub in its _inbound_adverts
                            # (we never told it about the remove) while we
                            # think it does not — silent divergence until
                            # the next full snapshot / reconnect. The
                            # subsequent ack-timeout scan (with retry_count
                            # logic from C-009) drives a snapshot retry that
                            # converges the state.
                            outbound_now[sub.sub_uuid] = prior_advert
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

    # ── Session 4 (v0.27.0) — sub-advert ack timeout + retry ──────

    async def _spawn_periodic_resync(self) -> None:
        """C-109: kick off a full-snapshot resend to every connected
        peer. Snapshots the peer list under struct_lock, then spawns
        per-peer detached tasks that call ``advertise_subs_remote``
        (the snapshot-replace path). Each peer's resend rebuilds our
        outbound table for that peer AND triggers a full snapshot on
        the wire — the receiver's _handle_sub_advertise does a
        wholesale replace of its _inbound_adverts[peer], which
        scrubs ghost entries that survived race-induced state drift.

        Spawned as detached because advertise_subs_remote can take
        seconds per peer (pool health check + send + ack roundtrip)
        and we don't want the heartbeat tick to wait on them.

        Skip filter:
          * Dead peers (``is_alive() == False``) — no point sending to
            a peer we've already marked dead via heartbeat strikes;
            ``_drop_peer_advert_state`` already cleared its outbound
            state and a resync would just re-create entries that the
            next failed heartbeat will drop again.
          * Peers with a pending resend task in ``_resend_tasks`` —
            the ack-timeout scan already initiated a per-peer rebuild
            for them this tick; a second wholesale replace would race
            on ``_outbound_adverts[peer]`` and overwrite the
            ack-timeout retry_count bookkeeping with a fresh
            ``retry_count=0``, defeating the cap-at-1-retry throttle
            on a persistently unresponsive peer. The ack-timeout path
            is the right recovery mechanism in that window.
        """
        if not getattr(self, "is_ready", False):
            return
        # Snapshot dead-skip candidates first so the live-iteration
        # below doesn't await per-node.
        candidate_nodes: List[Tuple[str, str]] = []
        async with self._adverts_struct_lock:
            for node in self.nodes:
                if not node.hostname or not node.IP:
                    continue
                if node.hostname == self.plexus.hostname:
                    continue
                candidate_nodes.append((node.IP, node.hostname))
            resend_in_flight = {
                h for h, t in getattr(self, "_resend_tasks", {}).items()
                if t is not None and not t.done()
            }
        # is_alive() can await (e.g. resolves last-seen ts vs liveness
        # timeout). Run outside the lock to keep struct_lock hold time
        # bounded. Single-pass — minor TOCTOU is acceptable (the
        # spawned task itself re-checks is_ready and the per-peer
        # _advert_locks serialise).
        peers_to_resync: List[Tuple[str, str]] = []
        for peer_ip, peer_hostname in candidate_nodes:
            if peer_hostname in resend_in_flight:
                continue
            node = next(
                (n for n in self.nodes if n.hostname == peer_hostname),
                None,
            )
            if node is None:
                continue
            try:
                if not (
                    node.enabled
                    and await node.is_alive(timeout=self.liveness_timeout)
                ):
                    continue
            except Exception:
                continue
            peers_to_resync.append((peer_ip, peer_hostname))
        for peer_ip, peer_hostname in peers_to_resync:
            self.plexus._spawn_fire_and_forget(
                self.advertise_subs_remote(peer_ip, peer_hostname),
                name=f"periodic_resync<-{peer_hostname}",
            )

    async def _check_advert_ack_timeouts(self) -> None:
        """Scan _outbound_adverts for entries past ack timeout. Coalesce
        per-peer: one full-snapshot resend per peer per tick. retry_count
        capped at 1 (one re-send attempt); second timeout marks state =
        'ack_timeout' and stops retrying.

        Called once per heartbeat-loop iteration AFTER the node-iteration
        block. Per-peer resends are spawned as detached tasks via
        _resend_and_bump_retry so the heartbeat tick stays on schedule
        (advertise_subs_remote can take seconds per peer in the pool-
        health-check + retry path).

        Race-safety: stale_uuids captured at scan time. After the resend
        rebuilds _outbound_adverts[peer], _resend_and_bump_retry bumps
        retry_count=1 ONLY for stale_uuids that survived the rebuild;
        new entries added by concurrent send_sub_delta_remote are NOT
        bumped — they get a clean retry chance.
        """
        if not getattr(self, "is_ready", False):
            return
        threshold = 2 * self.heartbeat_interval
        # C-012: monotonic timebase, paired with sent_at writes in
        # advertise_subs_remote / send_sub_delta_remote.
        now = time.monotonic()
        stale_by_peer: Dict[str, Set[str]] = {}
        peers_to_timeout: Dict[str, List[str]] = {}

        async with self._adverts_struct_lock:
            for peer_hostname, sub_table in self._outbound_adverts.items():
                for sub_uuid, sub in sub_table.items():
                    if sub.state != "pending":
                        continue
                    if sub.sent_at is None:
                        continue
                    if now - sub.sent_at <= threshold:
                        continue
                    if sub.retry_count < 1:
                        stale_by_peer.setdefault(
                            peer_hostname, set()
                        ).add(sub_uuid)
                    else:
                        peers_to_timeout.setdefault(
                            peer_hostname, []
                        ).append(sub_uuid)

            # Apply timeouts in-place under the lock. Skip peers that
            # are also being resent — the resend rebuilds the projected
            # dict (retry_count=0 again), so the timeout-eligible
            # entries get a fresh lease via the rebuild rather than a
            # terminal ack_timeout state from this tick.
            for peer_hostname, uuids in peers_to_timeout.items():
                if peer_hostname in stale_by_peer:
                    continue
                sub_table = self._outbound_adverts.get(peer_hostname, {})
                for sub_uuid in uuids:
                    sub = sub_table.get(sub_uuid)
                    if sub is not None and sub.state == "pending":
                        sub.state = "ack_timeout"

        # Spawn per-peer resend tasks. Heartbeat tick proceeds without
        # waiting (advertise_subs_remote can take seconds per peer).
        # C-115: register the task in self._resend_tasks keyed by peer
        # hostname so _drop_peer_advert_state can cancel any pending
        # resend for that peer. Without this, a resend task can finish
        # advertising to a peer that was just revoked. Done-callback
        # evicts so the dict stays bounded. _resend_tasks is initialised
        # in __init__; the lazy-init below is purely defensive for test
        # scaffolds that bypass __init__ via object.__new__() — falling
        # back to early-return would silently drop resends after
        # peers_to_timeout already mutated state above.
        resend_tasks = getattr(self, "_resend_tasks", None)
        if resend_tasks is None:
            resend_tasks = {}
            self._resend_tasks = resend_tasks
        for peer_hostname, stale_uuids in stale_by_peer.items():
            task = self.plexus._spawn_fire_and_forget(
                self._resend_and_bump_retry(peer_hostname, stale_uuids),
                name=f"resend_retry<-{peer_hostname}",
            )
            if task is not None:
                resend_tasks[peer_hostname] = task
                task.add_done_callback(
                    lambda _t, h=peer_hostname, rt=resend_tasks: rt.pop(h, None)
                    if rt.get(h) is _t
                    else None
                )

    async def _resend_and_bump_retry(
        self,
        peer_hostname: str,
        stale_uuids: Set[str],
    ) -> None:
        """Per-peer resend + post-resend retry_count bump. Spawned as a
        detached task from _check_advert_ack_timeouts. Resolves peer IP
        internally via the same NAT-correct chain as _send_advert_ack_to
        (peers_by_endpoint → self.nodes).

        is_ready guard at task entry: detached tasks are not tracked in
        any cancellation registry, so they could fire after stop()
        flips is_ready=False. Without the guard, post-stop tasks would
        call advertise_subs_remote on a half-torn-down manager.

        Bump rule: only stale_uuids that survived the rebuild get
        retry_count=1. New entries added by concurrent subscribes
        between the scan and the resend are NOT bumped (they get a
        clean retry chance).
        """
        if not getattr(self, "is_ready", False):
            return
        try:
            peer_ip: Optional[str] = None
            for (cfg_ip, _cfg_port), peer_cfg in self.peers_by_endpoint.items():
                if peer_cfg.hostname == peer_hostname:
                    peer_ip = cfg_ip
                    break
            if peer_ip is None:
                node = next(
                    (n for n in list(self.nodes) if n.hostname == peer_hostname),
                    None,
                )
                if node is not None and node.IP:
                    peer_ip = node.IP
            if peer_ip is None:
                self._logger.debug(
                    "[ADVERT_ACK_TIMEOUT] no IP for peer_hostname=%r — skip",
                    peer_hostname,
                )
                return
            try:
                await self.advertise_subs_remote(peer_ip, peer_hostname)
            except Exception:
                self._logger.debug(
                    "[ADVERT_ACK_TIMEOUT] resend to %r failed",
                    peer_hostname, exc_info=True,
                )
                return
        finally:
            # C-009: always bump retry_count on stale_uuids — even when
            # the IP could not be resolved or the resend raised. Without
            # this, _check_advert_ack_timeouts re-schedules the same
            # stale entries every heartbeat tick forever (retry_count
            # stays at 0, never reaches the >=1 ack_timeout-promotion
            # threshold). One bump per attempt is sufficient: the next
            # tick that observes the still-pending entry will hit the
            # retry_count>=1 branch and transition to ack_timeout.
            async with self._adverts_struct_lock:
                sub_table = self._outbound_adverts.get(peer_hostname)
                if sub_table is None:
                    return
                for sub_uuid in stale_uuids:
                    sub = sub_table.get(sub_uuid)
                    if sub is not None and sub.state == "pending":
                        sub.retry_count = 1

    # ── Sub-broadcast helpers (called from Plexus subscribe/unsubscribe) ──

    async def broadcast_local_sub_added(self, sub) -> None:
        """Filter peers + send add-delta. No-op when not ready.

        C-117: per-peer send failures now also emit
        ``_core/sub/peer_unreachable`` so the owning plugin (or a
        monitor) can react without polling. The DEBUG log stays for
        ops visibility; the topic carries structured payload.
        """
        if not getattr(self, "is_ready", False):
            return
        for node in list(self.nodes):
            if node.hostname is None:
                continue
            if node.hostname == self.plexus.hostname:
                continue
            try:
                if not (
                    node.enabled
                    and await node.is_alive(timeout=self.liveness_timeout)
                ):
                    continue
            except Exception:
                continue
            if not self._should_advertise_sub_to_peer(sub, node.hostname):
                continue
            try:
                await self.send_sub_delta_remote(
                    node.IP, node.hostname, "add", sub
                )
            except Exception as exc:
                self._logger.debug(
                    "broadcast_local_sub_added: send to %s failed",
                    node.hostname, exc_info=True,
                )
                # C-117: emit observable signal.
                try:
                    self.plexus._internal_emit(
                        "_core/sub/peer_unreachable",
                        peer_hostname=node.hostname,
                        sub_uuid=sub.sub_uuid,
                        kind="add",
                        error=f"{type(exc).__name__}: {exc}",
                        ts=time.time(),
                    )
                except Exception:
                    pass

    async def broadcast_local_sub_removed(self, sub) -> None:
        """Filter peers + send remove-delta. Only sends to peers we have
        actually advertised this sub to (outbound table is authority).

        C-117: see :meth:`broadcast_local_sub_added` for the
        peer_unreachable emit.
        """
        if not getattr(self, "is_ready", False):
            return
        for node in list(self.nodes):
            if node.hostname is None:
                continue
            if node.hostname == self.plexus.hostname:
                continue
            try:
                if not (
                    node.enabled
                    and await node.is_alive(timeout=self.liveness_timeout)
                ):
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
            except Exception as exc:
                self._logger.debug(
                    "broadcast_local_sub_removed: send to %s failed",
                    node.hostname, exc_info=True,
                )
                # C-117: emit observable signal (remove variant).
                try:
                    self.plexus._internal_emit(
                        "_core/sub/peer_unreachable",
                        peer_hostname=node.hostname,
                        sub_uuid=sub.sub_uuid,
                        kind="remove",
                        error=f"{type(exc).__name__}: {exc}",
                        ts=time.time(),
                    )
                except Exception:
                    pass

    # ── Initial-exchange + disconnect cleanup helpers ──

    async def _spawn_initial_exchange(self, node, *, force: bool = False) -> None:
        """Schedule (or skip) initial advert exchange to a Node. Idempotent
        via in-flight task table + ``_snapshot_sent`` guard inside the
        inner task body.

        C-116: lexicographic-hostname tiebreak (when ``force=False``).
        Only the lower-hostname peer of a (self, peer) pair initiates
        the proactive snapshot send (e.g. mutual discovery cascade);
        the higher-hostname peer waits for the inbound snapshot to
        fire its ``_maybe_reciprocal_exchange`` path. The
        reciprocal-exchange caller passes ``force=True`` because at
        that point the OTHER side has already sent and we're sending
        back — the tiebreak no longer applies.

        Deterministic — exactly one of (self_host > peer_host) /
        (self_host < peer_host) / (self_host == peer_host) holds;
        equality is self-exchange which we already skip below.
        """
        host = getattr(node, "hostname", None)
        if not host:
            return
        if not force and self.plexus.hostname > host:
            self._logger.debug(
                "[INIT_EXCH] lexicographic tiebreak: self=%r > peer=%r — "
                "deferring initiate; will reciprocate when peer's "
                "snapshot arrives",
                self.plexus.hostname, host,
            )
            return
        async with self._adverts_struct_lock:
            prev = self._initial_exchange_tasks.get(host)
            if prev is not None and not prev.done():
                return
            # R4-YY-5: route through _spawn_fire_and_forget so the
            # spawned coroutine runs under the depth-isolating wrapper
            # (matches the discipline used elsewhere; forward-proofs
            # against future event-emitting work added inside
            # _initial_advert_exchange). _spawn_fire_and_forget returns
            # None when no event loop is running (shutdown race) —
            # treat as "nothing to track".
            inner = self.plexus._spawn_fire_and_forget(
                self._initial_advert_exchange(node),
                name=f"init_advert_exch->{host}",
            )
            if inner is None:
                return
            self._initial_exchange_tasks[host] = inner

        def _deregister(_t, h=host):
            async def _drop():
                async with self._adverts_struct_lock:
                    cur = self._initial_exchange_tasks.get(h)
                    if cur is _t:
                        self._initial_exchange_tasks.pop(h, None)
            self.plexus._spawn_fire_and_forget(
                _drop(), name=f"init_exch_dereg<-{h}"
            )
            # Consume task's exception so Python doesn't log
            # "Task exception was never retrieved" at GC time.
            # _initial_advert_exchange re-raises after logging.
            try:
                if not _t.cancelled():
                    exc = _t.exception()
                    if exc is not None:
                        self._logger.debug(
                            "initial advert exchange to %s raised: %r", h, exc
                        )
            except Exception:
                pass

        inner.add_done_callback(_deregister)

    async def _perform_initial_exchange(self, peer_ip: str, host: str) -> None:
        """Check-then-set _snapshot_sent under struct_lock, then send
        the snapshot to peer_ip. Bail if another trigger already
        claimed the slot. Discard the flag and re-raise on any
        failure so the next reciprocal trigger can retry.

        C-063 + C-129: catches BaseException (covers CancelledError,
        which is a BaseException, not Exception). Previously the
        IP-only variant only caught Exception, letting a cancellation
        between ``_snapshot_sent.add(host)`` and the successful end
        of ``advertise_subs_remote`` leave ``host`` stuck in
        ``_snapshot_sent`` forever — every future reciprocal-exchange
        trigger for that peer would short-circuit at the gate, and
        the peer would never see our subs again until process restart.
        Unified to one helper so the two spawn sites
        (``_spawn_initial_exchange`` for Node-backed peers,
        ``_spawn_initial_exchange_for_ip`` for client-only peers
        without a Node entry yet) share identical exception handling.
        """
        async with self._adverts_struct_lock:
            if host in self._snapshot_sent:
                return
            # C-110: claim the slot with the peer's session_id if we
            # already know it (subsequent inbound payloads with the
            # same session_id won't trigger _detect_peer_restart's
            # mismatch path). If we don't know it yet (first contact),
            # use None as a "session unknown" sentinel — the first
            # inbound payload's session_id is what eventually fills
            # this slot via _detect_peer_restart's None-sentinel
            # branch (which adopts the inbound session_id without
            # popping the gate). Without the sentinel branch, seeding
            # with "" would mismatch any real session_id and trigger
            # a spurious second snapshot send on first contact.
            self._snapshot_sent[host] = self._peer_session_ids.get(host)
        try:
            await self.advertise_subs_remote(peer_ip, host)
        except BaseException:
            async with self._adverts_struct_lock:
                self._snapshot_sent.pop(host, None)
            self._logger.warning(
                "initial advert exchange to %s failed", host
            )
            raise

    async def _initial_advert_exchange(self, node) -> None:
        """Authoritative check-then-set wrapper for Node-backed peers.
        Delegates to ``_perform_initial_exchange`` which handles the
        check-then-set / advertise / discard sequence. See that
        helper's docstring for exception-handling rationale (C-063).
        """
        host = getattr(node, "hostname", None)
        if not host:
            return
        await self._perform_initial_exchange(node.IP, host)

    async def _spawn_initial_exchange_for_ip(
        self, peer_ip: str, host: str, *, force: bool = False
    ) -> None:
        """Variant for client-only peers without a Node entry yet.

        C-116: same lexicographic tiebreak as _spawn_initial_exchange.
        Lower-hostname initiates; higher-hostname waits to reciprocate.
        Reciprocal-exchange callers pass ``force=True`` to bypass the
        tiebreak (peer has already sent; we're sending back).
        """
        if not host:
            return
        if not force and self.plexus.hostname > host:
            self._logger.debug(
                "[INIT_EXCH_IP] lexicographic tiebreak: self=%r > peer=%r"
                " — deferring initiate",
                self.plexus.hostname, host,
            )
            return
        async with self._adverts_struct_lock:
            prev = self._initial_exchange_tasks.get(host)
            if prev is not None and not prev.done():
                return

            # R4-YY-5: route through _spawn_fire_and_forget so the
            # spawned coroutine runs under the depth-isolating wrapper
            # (matches sibling _spawn_initial_exchange; forward-proofs
            # against future event-emitting work added inside
            # _perform_initial_exchange). Returns None on no-loop —
            # treat as "nothing to track".
            t = self.plexus._spawn_fire_and_forget(
                self._perform_initial_exchange(peer_ip, host),
                name=f"init_advert_exch_ip->{host}",
            )
            if t is None:
                return
            self._initial_exchange_tasks[host] = t

        def _dereg(_t, h=host):
            async def _drop():
                async with self._adverts_struct_lock:
                    cur = self._initial_exchange_tasks.get(h)
                    if cur is _t:
                        self._initial_exchange_tasks.pop(h, None)
            self.plexus._spawn_fire_and_forget(
                _drop(), name=f"init_exch_ip_dereg<-{h}"
            )
            # Consume task's exception so Python doesn't log
            # "Task exception was never retrieved" at GC time.
            # _exchange re-raises after the snapshot-discard.
            try:
                if not _t.cancelled():
                    exc = _t.exception()
                    if exc is not None:
                        self._logger.debug(
                            "initial advert exchange (ip) to %s raised: %r",
                            h, exc,
                        )
            except Exception:
                pass

        t.add_done_callback(_dereg)

    async def _drop_peer_advert_state(self, peer_hostname: str) -> None:
        """Clear all advert state for a peer + cancel in-flight tasks
        targeting it (locked #4 + #17)."""
        if not peer_hostname:
            return
        # R2-EE-4: also drain + drop the peer's connection_pools entry.
        # Without this, dead/flapping peers leave their asyncio.Queue
        # lingering in connection_pools (only revoke_peer pops it),
        # growing the dict unboundedly. Resolve the (ip, port) via
        # peers_by_endpoint reverse-lookup, then snapshot+close each
        # pooled writer and pop the key.
        pool_keys = [
            (cfg_ip, cfg_port)
            for (cfg_ip, cfg_port), peer_cfg in self.peers_by_endpoint.items()
            if getattr(peer_cfg, "hostname", None) == peer_hostname
        ]
        for pool_key in pool_keys:
            pool = self.connection_pools.pop(pool_key, None)
            if pool is None:
                continue
            while True:
                try:
                    _r, w = pool.get_nowait()
                except asyncio.QueueEmpty:
                    break
                except Exception as e:
                    self._logger.warning(
                        "_drop_peer_advert_state pool drain unexpected error: %s", e
                    )
                    break
                try:
                    w.close()
                    await w.wait_closed()
                except Exception as e:
                    self._logger.debug(
                        "_drop_peer_advert_state pool close error for %s: %s — continuing drain",
                        pool_key, e,
                    )
        # R4-VV-3: also evict the matching peers_by_endpoint entries.
        # Without this, a concurrent _return_connection sees
        # peers_by_endpoint still holds the key (bypassing the early-close
        # guard at "if key not in self.peers_by_endpoint"), then finds
        # connection_pools key absent and recreates the queue — pooling
        # the stale writer and resurrecting the dropped peer's slot.
        for pool_key in pool_keys:
            self.peers_by_endpoint.pop(pool_key, None)
        async with self._adverts_struct_lock:
            self._inbound_adverts.pop(peer_hostname, None)
            self._inbound_global_order = {
                k: v
                for k, v in self._inbound_global_order.items()
                if k[0] != peer_hostname
            }
            self._outbound_adverts.pop(peer_hostname, None)
            # C-110: dict.pop replaces set.discard.
            self._snapshot_sent.pop(peer_hostname, None)
            # Also forget the peer's announced session_id so the next
            # contact starts fresh and we don't compare-against-stale.
            self._peer_session_ids.pop(peer_hostname, None)
            # B-071: reset per-peer wire counters on heartbeat-declared
            # disconnect (current-session only per O7). Late frames after
            # this pop are silently skipped by _count_sent/_count_recv
            # (None-skip in helper), so we don't recreate stale state.
            self.peer_stats.pop(peer_hostname, None)
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

        # C-115: cancel any in-flight resend task for this peer so a
        # stale advertise_subs_remote call doesn't fire after we just
        # cleared the peer's advert state. The done-callback at the
        # spawn site evicts the entry; cancel-then-pop here is safe.
        resend_tasks = getattr(self, "_resend_tasks", None)
        if resend_tasks is not None:
            resend_task = resend_tasks.pop(peer_hostname, None)
            if resend_task is not None and not resend_task.done():
                try:
                    resend_task.cancel()
                    try:
                        await asyncio.wait_for(resend_task, timeout=1.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                        pass
                except Exception:
                    pass

        # C-007 fix: do NOT pop the per-peer Lock entry. The previous
        # behaviour (pop here outside struct_lock + setdefault outside
        # struct_lock at the two call sites in advertise_subs_remote and
        # send_sub_delta_remote) created a window where two concurrent
        # callers for the same peer could each get a DIFFERENT Lock
        # object — no mutual exclusion. Leaving the entry in
        # ``_advert_locks`` makes the lock per-peer-singleton for the
        # process lifetime; memory growth is bounded by peer count
        # (typically tens to low-hundreds, not enough to matter). A
        # re-added peer after revoke reuses the existing lock, which is
        # the correct invariant. Lock cleanup on full shutdown is via
        # GC when NetworkManager is freed.

    async def _mark_node_dead(self, node) -> None:
        """Centralised node-dead helper. Idempotent."""
        if not node.enabled:
            return
        node.enabled = False
        host = getattr(node, "hostname", None)
        if host:
            await self._drop_peer_advert_state(host)
            # C-044: clear strike counter so a future re-enable starts
            # with a fresh budget.
            self._heartbeat_misses.pop(host, None)
            # C-092: fail any Plexus-level Request objects whose target
            # is this peer (or whose remote-stamp matches the peer's
            # node) so callers see a fast NetworkRequestException
            # instead of waiting for the underlying TCP socket timeout
            # (which can take minutes). _inflight_publishes was
            # already cancelled inside _drop_peer_advert_state; this
            # closes the higher-level requests too.
            try:
                requests = getattr(self.plexus, "requests", None)
                if requests:
                    for req in list(requests.values()):
                        # Filter to requests targeting this peer. The
                        # author_host field is the canonical "where
                        # was this request routed" stamp for remote
                        # dispatch — set when _process_request sees
                        # a RemotePlugin target.
                        if (
                            getattr(req, "_is_remote", False)
                            and getattr(req, "target_host", None) == host
                            and not req._future.done()
                        ):
                            try:
                                req._future.set_exception(
                                    NetworkRequestException(
                                        f"peer {host!r} marked dead; "
                                        f"in-flight request {req.id} "
                                        f"fast-failed (C-092)"
                                    )
                                )
                            except Exception:
                                pass
            except Exception:
                self._logger.debug(
                    "_mark_node_dead: in-flight request fast-fail failed",
                    exc_info=True,
                )

    async def _record_heartbeat_miss(self, node) -> bool:
        """C-044 helper: increment per-host miss counter; return True
        iff the count has reached ``self.heartbeat_strikes`` (meaning
        the caller should now mark the node dead).

        Falls back to dead-on-first-miss semantics (legacy behaviour)
        if ``heartbeat_strikes`` is set to 1 or less.
        """
        host = getattr(node, "hostname", None) or ""
        # Without a hostname we have nowhere to track misses; fail
        # closed (mark dead immediately) — same as pre-C-044 behaviour.
        if not host:
            return True
        strikes = max(1, int(getattr(self, "heartbeat_strikes", 3)))
        count = self._heartbeat_misses.get(host, 0) + 1
        if count >= strikes:
            # Reached threshold — clear and signal dead.
            self._heartbeat_misses.pop(host, None)
            return True
        self._heartbeat_misses[host] = count
        self._logger.debug(
            "[HEARTBEAT] miss %d/%d for peer=%r — tolerating",
            count, strikes, host,
        )
        return False

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
        from .notifier import TopicRegistry as _TR  # local import: cycle

        out: Dict[str, List[AdvertSub]] = {}
        async with self._adverts_struct_lock:
            per_peer_snap = {
                host: list(adverts.values())
                for host, adverts in self._inbound_adverts.items()
            }

        for node in list(self.nodes):
            if node.hostname == self.plexus.hostname:
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
                if not (
                    node.enabled
                    and await node.is_alive(timeout=self.liveness_timeout)
                ):
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
                if not self.plexus._sub_accepts_remote_publisher(
                    advert, self.plexus.hostname, author
                ):
                    continue
                if not self.plexus._sub_accepts_author(advert, author):
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
                    # have an explicit port; otherwise dedupe by exact
                    # tuple. C-006: build a NEW tuple then rebind so
                    # concurrent readers iterate either the old or the
                    # new snapshot, never a mid-update mix.
                    new_node_ips: Tuple[Tuple[str, Optional[int]], ...]
                    new_node_ips = self.node_ips
                    if resolved_port is not None:
                        new_node_ips = tuple(
                            e
                            for e in new_node_ips
                            if not (e[0] == client_ip and e[1] is None)
                        )
                        # Also patch any existing Node for this IP that was
                        # created before we knew its listener port. Without
                        # this, _resolve_port walks self.nodes first and
                        # finds the stale port=None Node — masking the
                        # corrected node_ips entry forever. Node objects
                        # themselves are mutable per-instance (the tuple
                        # holding them is what's immutable); patching
                        # n.port in place is safe across the snapshot
                        # boundary because every snapshot references the
                        # same Node instances.
                        for n in self.nodes:
                            if n.IP == client_ip and n.port is None:
                                n.port = resolved_port
                                if n.hostname is None and hostname:
                                    n.hostname = hostname
                    if client_entry not in new_node_ips:
                        new_node_ips = new_node_ips + (client_entry,)
                    self.node_ips = new_node_ips

            response = {
                "hostname": self.plexus.hostname,
                "auto_discoverable": self.auto_discoverable,
                "nodes": [
                    await node._to_tuple()
                    for node in self.nodes
                    if node.auto_discoverable
                    and not node.hostname == hostname
                    and discover_nodes_info
                    and node.enabled
                    and await node.is_alive(timeout=self.liveness_timeout)
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

        R3-PP-2: snapshot peers_by_endpoint BEFORE the resolve+lookup pair so
        a concurrent revoke_peer.pop() between step 1 (_resolve_port) and
        step 2 (peers_by_endpoint.get) cannot turn a valid peer into a
        spurious ConnectionError("not in peers config"). The lookup below
        consults the frozen snapshot instead of the live dict.
        """
        peers_snapshot = dict(self.peers_by_endpoint)
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

        # R3-PP-2: consult the snapshot taken before _resolve_port so a
        # mid-flight revoke cannot produce a misleading "not in peers config"
        # error for a peer that was valid at call time.
        peer_cfg = peers_snapshot.get((IP, port))
        if peer_cfg is None or peer_cfg.fingerprint != peer_fp:
            writer.close()
            try: await writer.wait_closed()
            except Exception: pass
            raise ConnectionError(
                f"Server fingerprint {peer_fp} for {IP}:{port} not in peers config"
            )

        # B-071: stamp peer hostname for wire-counter accounting in
        # _send_message / _receive_message / _count_sent / _count_recv.
        # Pool reuse keeps the attribute alive (pool keyed by (IP, port)
        # → same peer). Pre-create entry so hot-path helpers can use
        # dict.get without dict-literal allocation per call.
        writer._aio_peer_hostname = peer_cfg.hostname
        reader._aio_peer_hostname = peer_cfg.hostname
        self.peer_stats.setdefault(peer_cfg.hostname, {
            "bytes_sent": 0, "bytes_recv": 0,
            "msgs_sent": 0, "msgs_recv": 0,
        })

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

        C-045: transactional revoke. After the config-side pop, drops
        the per-peer advert state (which itself cancels in-flight
        publishes via _drop_peer_advert_state). This prevents stale
        ``_inbound_adverts`` / ``_outbound_adverts`` entries from
        outliving the peer's credentials and dispatching events to a
        revoked target. Best-effort: cleanup errors are logged but do
        not interrupt the pool drain that follows.
        """
        closed = 0
        spec = next((p for p in self.peers if p.fingerprint == fingerprint), None)
        if spec is None:
            return 0
        self.peers_by_endpoint.pop((spec.ip, spec.port), None)
        self.peers_by_fingerprint.pop(spec.fingerprint, None)
        self.peers = [p for p in self.peers if p.fingerprint != fingerprint]
        # C-045: drop advert state for the revoked peer. This also
        # cancels any pending _inflight_publishes for the hostname
        # (per _drop_peer_advert_state lines 3863-3884). Without this,
        # events keep dispatching to a peer whose credentials we just
        # revoked.
        peer_hostname = getattr(spec, "hostname", None)
        if peer_hostname:
            try:
                await self._drop_peer_advert_state(peer_hostname)
            except Exception:
                self._logger.warning(
                    "revoke_peer: _drop_peer_advert_state for %r failed",
                    peer_hostname, exc_info=True,
                )
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
        # R3-PP-1: rebuild self.ssl_context so subsequent _create_connection
        # outbound paths (and any future server context rebuild) no longer
        # trust the revoked peer's CA cert. NOTE: asyncio.start_server captured
        # the SSL context at construction time, so the *running* accept loop
        # continues to use the original context until the next NetworkManager
        # rebuild (e.g. config hot-reload). This rebuild closes the outbound
        # trust-store hole and ensures any future SSL-context refresh consumers
        # see the post-revoke state. Documented hot-swap limitation for inbound.
        #
        # R3-PP-1 review follow-up:
        #   - Last-peer case: when ``self.peers`` is empty after this revoke,
        #     ``_create_pinned_ssl_context`` would hard-raise (no trusted CAs),
        #     so the rebuild is skipped. The stale ``self.ssl_context`` is
        #     unavoidable here — the empty-peers warning above already
        #     documents that further outgoing connections will fail until a
        #     new peer is added; operator must restart or hot-reload.
        #   - Concurrent revoke serialisation: the unsynchronised mutations
        #     above (peers_by_endpoint/peers_by_fingerprint/self.peers) plus
        #     this rebuild are not guarded by a lock. revoke_peer is
        #     documented as a single-caller administrative path (called from
        #     hot-reload / runtime cert revocation). If two concurrent
        #     revoke_peer calls ever land, the last writer wins on
        #     self.ssl_context but both peer-set mutations are interleaved
        #     correctly by the GIL — net behaviour is "both peers revoked"
        #     with one rebuild reflecting the final state.
        if self.peers:
            try:
                self.ssl_context = self._create_server_ssl_context()
            except Exception:
                self._logger.warning(
                    "revoke_peer: ssl_context rebuild failed; outbound trust store stale",
                    exc_info=True,
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
                        # C-043: track checked-out writer for stop()
                        # drain coverage. getattr defensive against
                        # test scaffolds that bypass __init__.
                        co = getattr(self, "_checked_out_writers", None)
                        if co is not None:
                            co.add(writer)
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
        try:
            reader, writer = await self._create_connection(IP)
        except Exception:
            # R2-CC-6: distinguish revoke-induced failures from transient
            # network errors. If the peer was revoked between pool.get()
            # and _create_connection, peers_by_endpoint no longer carries
            # the (IP, port) entry — surface that explicitly so operators
            # don't chase a network outage that's really a revocation race.
            if key not in self.peers_by_endpoint:
                self._logger.debug(
                    "[CONNECTION] _create_connection to %s failed: peer "
                    "no longer in peers_by_endpoint — revoke-induced "
                    "failure (peer was revoked between pool.get() and "
                    "_create_connection).",
                    IP,
                )
            raise
        # C-043: track checked-out writer for stop() drain coverage.
        co = getattr(self, "_checked_out_writers", None)
        if co is not None:
            co.add(writer)
        return reader, writer

    async def _return_connection(
        self, IP: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Return a connection to the pool (keyed by (IP, port))."""
        key = self._pool_key(IP)

        # R2-CC-1: revoke-race guard. If the peer was revoked between
        # the caller's pool.get() and this return (revoke_peer pops the
        # pool key + peers_by_endpoint entry under the same tick), the
        # writer in our hand is a now-untrusted connection. Re-creating
        # the pool entry and pooling the writer would resurrect the
        # revoked peer's slot for the next caller. Close instead.
        if key not in self.peers_by_endpoint:
            # C-043: also discard from checked-out tracking before close.
            co = getattr(self, "_checked_out_writers", None)
            if co is not None:
                co.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            return

        if key not in self.connection_pools:
            self.connection_pools[key] = asyncio.Queue(maxsize=self.pool_size)

        # C-043: writer is being returned to pool (or closed); remove
        # from checked-out tracking so stop() doesn't double-close it.
        # Defensive getattr — some tests scaffold NM via object.__new__
        # which bypasses __init__.
        co = getattr(self, "_checked_out_writers", None)
        if co is not None:
            co.discard(writer)

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
            # R4-VV-5: wait_closed() can raise (Windows ConnectionResetError
            # when the remote already closed; OSError on some Linux kernels).
            # This is the overflow disposal path — transport errors here
            # must not propagate to the caller.
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

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
        # R2-LL-2: ``timeout`` is the scalar duration (seconds) the
        # requester is willing to wait. The legacy ``(duration,
        # sender_created_at)`` tuple shape is still accepted for back-compat
        # with older peers but the second element is ignored by the
        # receiver — the remote deadline is always anchored to the
        # receiver's own monotonic clock.
        timeout=None,
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
                "author_host": author_host or self.plexus.hostname,
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

                # R2-CC-4: sender/receiver parity — both reject at exactly MAX.
                if msg_length >= MAX_MESSAGE_SIZE:
                    raise NetworkRequestException(
                        f"Message length {msg_length} exceeds maximum {MAX_MESSAGE_SIZE}"
                    )

                msg_type_byte = await reader.readexactly(1)
                msg_type = msg_type_byte[0]

                payload_length = msg_length - 1
                if payload_length > 0:
                    payload = await reader.readexactly(payload_length)

                    # B-071: per-peer recv counter (with-payload frame).
                    self._count_recv(reader, 4 + 1 + payload_length)

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
                        # K-6 (B-066): wrap unknown class as NetworkRequestException.
                        try:
                            error_data = safe_loads(payload)
                        except pickle.UnpicklingError as _e:
                            raise NetworkRequestException(f"Remote node {IP} sent unknown exception class: {_e}. Make custom exceptions inherit from plexus.serialization.SerializableException.") from _e
                        if isinstance(error_data, BaseException):
                            # W1-A4: preserve pickled exception identity.
                            self._logger.warning("[REMOTE] EXECUTE error from %s: %s: %s", IP, type(error_data).__name__, error_data)
                            raise error_data
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
                    # B-071: per-peer recv counter (no-payload END_STREAM).
                    self._count_recv(reader, 4 + 1)
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

        except RequestException:
            # W1-A4 / L1 (cycle review): typed RequestException (incl. peer
            # plugin's SerializableException subclasses) propagates unwrapped
            # so the caller's ``except RequestException`` clause sees the
            # original type. Mirror request_event_remote (line 3531).
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                connection_returned = True
            raise
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
            raise NetworkRequestException(f"Remote execution failed: {e}") from e
        except BaseException:
            # R2-AA-2: catch CancelledError (and any other BaseException
            # like KeyboardInterrupt / SystemExit). On cancel mid-read the
            # plain ``except Exception`` above is bypassed and the finally
            # would otherwise pool a connection whose reader still holds
            # un-consumed protocol bytes — the next caller gets framing
            # corruption. Close the writer here and mark the connection
            # already-handled so the finally does NOT attempt to pool it.
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
                connection_returned = True
            raise
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
        # R2-LL-2: scalar duration (seconds). Tuple shape accepted for
        # back-compat — see ``execute_remote`` for the rationale.
        timeout=None,
        author_host: str = None,
        request_id: str = None,
    ):
        """Execute a streaming plugin method on a remote node."""
        reader = None
        writer = None
        connection_returned = False
        # R2-AA-10: gate pool-return on a clean MSG_END_STREAM completion.
        # The consumer of this async generator may break out of its
        # async-for loop, which closes the gen and raises GeneratorExit
        # (a BaseException, NOT caught by `except Exception` below). The
        # finally would otherwise pool a connection whose reader still
        # holds un-consumed protocol bytes — corrupting the pool. Only
        # the explicit MSG_END_STREAM break paths set this True.
        clean_exit = False

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
                "author_host": author_host or self.plexus.hostname,
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

                # R2-CC-4: sender/receiver parity — both reject at exactly MAX.
                if msg_length >= MAX_MESSAGE_SIZE:
                    raise NetworkRequestException(
                        f"Message length {msg_length} exceeds maximum {MAX_MESSAGE_SIZE}"
                    )

                msg_type_byte = await reader.readexactly(1)
                msg_type = msg_type_byte[0]

                payload_length = msg_length - 1
                if payload_length > 0:
                    payload = await reader.readexactly(payload_length)

                    # B-071: per-peer recv counter (with-payload frame).
                    self._count_recv(reader, 4 + 1 + payload_length)

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
                                # R3-SS-3 fix: an error sentinel pushed as the
                                # final stream item and terminated with
                                # MSG_END_STREAM must raise — not be yielded as
                                # data. Mirrors the MSG_STREAM_ITEM_END check
                                # below so both flush sites agree.
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
                                yield item
                            except NetworkRequestException:
                                # Let the sentinel-raise above propagate.
                                raise
                            except Exception as e:
                                self._logger.exception(
                                    f"Failed to unpickle final stream item from {IP}"
                                )
                        # R2-AA-10: clean termination — pool may be reused.
                        clean_exit = True
                        break
                    elif msg_type == MSG_ERROR:
                        # K-6 (B-066): wrap unknown class as NetworkRequestException.
                        try:
                            error_data = safe_loads(payload)
                        except pickle.UnpicklingError as _e:
                            raise NetworkRequestException(f"Remote node {IP} sent unknown exception class: {_e}. Make custom exceptions inherit from plexus.serialization.SerializableException.") from _e
                        if isinstance(error_data, BaseException):
                            # W1-A4 / W5-R4: preserve pickled exception identity.
                            self._logger.warning("[REMOTE_STREAM] Node %s returned ERROR: %s: %s", IP, type(error_data).__name__, error_data)
                            raise error_data
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
                    # B-071: per-peer recv counter (no-payload ITEM_END —
                    # the common item-boundary marker; fires every yield).
                    self._count_recv(reader, 4 + 1)
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
                    # B-071: per-peer recv counter (no-payload END_STREAM).
                    self._count_recv(reader, 4 + 1)
                    # End of stream with no payload
                    if current_item_chunks:
                        full_pickled = b"".join(current_item_chunks)
                        try:
                            item = safe_loads(full_pickled)
                            # R3-SS-3 fix: an error sentinel flushed via the
                            # no-payload MSG_END_STREAM path must raise rather
                            # than be yielded as data. Mirrors the
                            # MSG_STREAM_ITEM_END sentinel-check pattern above.
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
                            yield item
                        except NetworkRequestException:
                            # Let the sentinel-raise above propagate.
                            raise
                        except Exception as e:
                            self._logger.exception(
                                f"Failed to unpickle final stream item from {IP}"
                            )
                    # R2-AA-10: clean termination — pool may be reused.
                    clean_exit = True
                    break
                else:
                    raise NetworkRequestException(
                        f"Unexpected message type: {msg_type}"
                    )

        except NetworkRequestException:
            # F5 fix: NetworkRequestException raised from the decoder's
            # sentinel-detection path means the remote handler reported
            # a real error. Re-raise so _process_request_stream's outer
            # except (core.py) handles it (sets request error,
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
        except RequestException:
            # W1-A4 / W5-R4 / L1 (cycle review): typed RequestException
            # (incl. peer plugin's SerializableException subclasses)
            # propagates unwrapped. Existing `except NetworkRequestException`
            # branch above only handled the narrow subclass; this wider
            # clause covers all RequestException subtypes the peer pickled.
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
            # R2-AA-10: pool ONLY on clean MSG_END_STREAM termination.
            # GeneratorExit (consumer break-out) bypasses every except
            # clause above and arrives here with clean_exit=False — the
            # reader may still hold un-consumed protocol bytes that would
            # corrupt the next caller. Close the writer in that case.
            # Success-path (clean_exit=True): pool as before. Exception
            # paths already closed and set connection_returned=True.
            if reader and writer and not connection_returned:
                if clean_exit:
                    try:
                        self._logger.debug(
                            f"[REMOTE_STREAM] Returning connection for {IP}"
                        )
                        await self._return_connection(IP, reader, writer)
                        connection_returned = True
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
                    connection_returned = True

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
    #    ) as client:  # verify='./cert.pem'
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
    #                "author_host": self.plexus.hostname,
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
    #                    "author_host": self.plexus.hostname,
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
        # Merge and deduplicate endpoints — additional may contain raw
        # strings ("IP" / "IP:PORT") or dicts; normalise all to
        # (ip, port) tuples. C-006: build a new tuple locally then
        # rebind once so readers never see a partial mutation.
        if additional_IP_list:
            merged = list(self.node_ips)
            for entry in additional_IP_list:
                merged.append(self._parse_endpoint(entry))
            self.node_ips = tuple(dict.fromkeys(merged))
        elif not isinstance(self.node_ips, tuple):
            # Defensive: a prior code path mutated to list. Rebind.
            self.node_ips = tuple(dict.fromkeys(self.node_ips))

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
                await node.update(response, self.plexus.hostname)
                # PR3 Stage C: peer-connect lifecycle hook (Site A —
                # locked #7). Symmetric initial-exchange — fire-and-
                # forget; idempotent via `_snapshot_sent`.
                if (
                    getattr(self.plexus, "networking_enabled", False)
                    and node.hostname
                    and node.hostname != self.plexus.hostname
                    and node.hostname not in self._snapshot_sent
                ):
                    self.plexus._spawn_fire_and_forget(
                        self._spawn_initial_exchange(node),
                        name=f"discov_cascade<-{node.hostname}",
                    )

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

                if sub_hostname == self.plexus.hostname:
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
                    # Update Node.port if the cascade just told us a port
                    # we didn't have. Fix node_ips dedupe at the same time
                    # so (IP, None) and (IP, port) don't both linger.
                    # C-006: build new tuple locally then rebind once.
                    if existing.port is None and sub_port is not None:
                        existing.port = sub_port
                        new_node_ips = tuple(
                            e
                            for e in self.node_ips
                            if not (e[0] == sub_ip and e[1] is None)
                        )
                        new_entry = (sub_ip, sub_port)
                        if new_entry not in new_node_ips:
                            new_node_ips = new_node_ips + (new_entry,)
                        self.node_ips = new_node_ips
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
            # C-006: tuple rebind, not append.
            self.node_ips = self.node_ips + (entry,)

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
            new_node = Node(
                IP=IP,
                hostname=hostname,
                enabled=True,
                auto_discoverable=False,
                port=port,
            )
            # C-006: tuple rebind, not append.
            self.nodes = self.nodes + (new_node,)

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
                "hostname": self.plexus.hostname,
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
            # C-006: tuple rebind, not list.remove(). Identity compare
            # via `is` so two Nodes with equal __eq__ but distinct
            # identity don't both get filtered out.
            self.nodes = tuple(n for n in self.nodes if n is not node)
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
    async def node_exists(self, IP: str):
        # C-176: future enhancement — accept a hostname kwarg and search
        # by (IP or hostname). Tracked outside source comments.
        for node in self.nodes:
            if node.IP == IP:
                return True

        return False

    @async_log_errors
    async def _get_node(
        self, IP: str, hostname: Union[str, None] = None, autogenerate: bool = False
    ) -> Optional[Node]:
        # C-176: future enhancement — when hostname is supplied and
        # unique, search by hostname only. Tracked outside source comments.

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
            matching the format used by Plexus.find_endpoints_by_tag,
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
