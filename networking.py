from logging import Logger
import ssl
import socket
import ipaddress
import asyncio
import pickle
import struct
import os
from typing import List, Union, Optional, Tuple
from decorators import async_log_errors, async_handle_errors, async_gen_handle_errors
from exceptions import NetworkRequestException, NodeException
from networking_classes import Node
from networking_classes import RemotePlugin


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

MSG_NOTIFY = 7  # Fire-and-forget topic notification
MSG_TOPIC_REQUEST = 8  # Request-by-topic (one-to-one with response)
MSG_TOPIC_REQUEST_STREAM = 9  # Streaming request-by-topic

MSG_STREAM_ITEM_END = 14  # Marks end of one item in a streaming response

MSG_AUTH = 20  # Authentication message (shared secret)

CHUNK_SIZE = 64 * 1024  # 64KB chunks for streaming
MAX_MESSAGE_SIZE = 100 * 1024 * 1024  # 100MB max message size

# Sentinel returned by request_topic_remote when server sends no result data.
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
    ):
        self.plugin_core = plugin_core
        self._logger = logger

        # Internal node_ips is normalized to list[tuple[str, Optional[int]]].
        # Each entry is (ip, port_or_None); port=None means "use the cluster
        # default port self.port". External callers can pass either the legacy
        # form (list of "IP" or "IP:PORT" strings, mixed) or the new form
        # (list of {"ip": ..., "port": ...} dicts) — see _parse_endpoint.
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

        # Security configuration
        self.secret = secret or os.getenv("NETWORKING_SECRET", "")
        if isinstance(self.secret, str):
            self.secret = self.secret.encode()
        if not self.secret:
            logger.warning(
                "[SECURITY] No shared secret configured! Set 'secret' in networking "
                "config or NETWORKING_SECRET env var. Networking will refuse to start."
            )
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

        Walks self.nodes for the first matching IP and returns its `port`
        (falling back to self.port if the Node was created without one).
        Falls back to self.port if no Node matches yet — the discovery flow
        creates Nodes lazily, so the first connection-attempt to an IP we
        haven't yet promoted to Node uses the cluster default port.
        """
        for node in self.nodes:
            if node.IP == IP:
                return node.port if node.port is not None else self.port
        # No matching node yet — also check raw node_ips entries
        for ip, port in self.node_ips:
            if ip == IP and port is not None:
                return port
        return self.port

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
                MSG_NOTIFY: "NOTIFY",
                MSG_TOPIC_REQUEST: "TOPIC_REQUEST",
                MSG_TOPIC_REQUEST_STREAM: "TOPIC_REQUEST_STREAM",
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
                data = pickle.loads(payload)
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
        """Starts socket server without blocking the main loop."""
        if not self.secret:
            raise RuntimeError(
                "Networking cannot start without a shared secret. "
                "Set 'secret' in networking config or NETWORKING_SECRET env var."
            )
        self._logger.info(
            f"[SERVER] Starting server: port={self.port}, discover_nodes={self.discover_nodes}, "
            f"direct_discoverable={self.direct_discoverable}, auto_discoverable={self.auto_discoverable}, "
            f"heartbeat_interval={self.heartbeat_interval}, lookup_interval={self.lookup_interval}, "
            f"liveness_timeout={self.liveness_timeout}"
        )
        # Create SSL context
        self.ssl_context = self._create_ssl_context()

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
                                node.enabled = False
                        except Exception:
                            # Mark node disabled on heartbeat failure
                            try:
                                node.enabled = False
                            except Exception:
                                pass
                except Exception:
                    self._logger.debug("Heartbeat iteration failed")
                await asyncio.sleep(self.heartbeat_interval)

        self._logger.debug("[SERVER] Starting heartbeat loop task")
        self.heartbeat_task = asyncio.create_task(heartbeat_loop())

    async def stop(self):
        """Stop the socket server and close all connections."""
        self._logger.info("[SERVER] Stopping server and background tasks")
        # Cancel background tasks first
        for task in [self.heartbeat_task, self.discovery_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

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
        """Main server-side connection handler with authentication."""
        client_addr = writer.get_extra_info("peername")
        self._logger.debug(f"New client connection from {client_addr}")

        try:
            # Authenticate client
            try:
                msg_type, data = await asyncio.wait_for(
                    self._receive_message(reader), timeout=5.0
                )
            except (asyncio.TimeoutError, ConnectionError, ConnectionResetError):
                self._logger.debug(
                    f"Auth phase failed/timeout from {client_addr}; closing connection"
                )
                return
            if msg_type != MSG_AUTH:
                await self._send_error(
                    writer, "Authentication required as first message"
                )
                return

            if data != self.secret:
                await self._send_error(writer, "Authentication failed")
                self._logger.warning(f"Authentication failed for {client_addr}")
                return

            # Authentication successful — send confirmation so client knows
            await self._send_message(writer, MSG_RESULT, {"status": "authenticated"})

            # Process requests
            while True:
                try:
                    msg_type, data = await self._receive_message(reader)
                except (ConnectionError, ConnectionResetError):
                    self._logger.debug(
                        f"Connection lost while handling client {client_addr}"
                    )
                    break

                if msg_type == MSG_EXECUTE:
                    await self._handle_execute(reader, writer, data)
                elif msg_type == MSG_EXECUTE_STREAM:
                    await self._handle_execute_stream(reader, writer, data)
                elif msg_type == MSG_HAS_ENDPOINT:
                    self._logger.debug(
                        f"[ENDPOINT] Routing HAS_ENDPOINT message from {client_addr} to handler"
                    )
                    await self._handle_has_endpoint(reader, writer, data)
                elif msg_type == MSG_PING:
                    await self._handle_ping(reader, writer, data)
                elif msg_type == MSG_INFO:
                    await self._handle_info(reader, writer, data)
                elif msg_type == MSG_FIND_TAGGED_ENDPOINTS:
                    await self._handle_find_tagged_endpoints(reader, writer, data)
                elif msg_type == MSG_NOTIFY:
                    await self._handle_notify(reader, writer, data)
                elif msg_type == MSG_TOPIC_REQUEST:
                    await self._handle_topic_request(reader, writer, data)
                elif msg_type == MSG_TOPIC_REQUEST_STREAM:
                    await self._handle_topic_request_stream(reader, writer, data)
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

    async def _handle_execute(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, data: dict
    ):
        """Handle EXECUTE message."""
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
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, data: dict
    ):
        """Handle EXECUTE_STREAM message."""
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
                    break

            await self._send_end_stream(writer)
            self._logger.info(f"[EXECUTE_STREAM] Completed: items_sent={sent_items}")

        except Exception as e:
            self._logger.exception("Exception while streaming")
            try:
                err_obj = ("__STREAM_EXCEPTION__", str(e))
                await self._send_stream_chunk(writer, err_obj)
                await self._send_end_stream(writer)
            except Exception:
                pass

    async def _handle_has_endpoint(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, data: dict
    ):
        """Handle HAS_ENDPOINT message (checks plugin existence AND endpoint in one call)."""
        client_addr = writer.get_extra_info("peername")
        try:
            access_name = data.get("access_name")
            plugin_uuid = data.get("plugin_uuid")
            requester_id = data.get("requester_id")
            target_plugin = data.get("target_plugin")

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
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, data: dict
    ):
        """Handle FIND_TAGGED_ENDPOINTS message -- returns all local endpoints matching a tag."""
        client_addr = writer.get_extra_info("peername")
        try:
            tag = data.get("tag")
            self._logger.info(
                f"[TAG_SEARCH] Received FIND_TAGGED_ENDPOINTS from {client_addr}: tag='{tag}'"
            )

            endpoints = []
            for plugin in self.plugin_core.plugins.values():
                if plugin.enabled:
                    for endpoint in plugin.endpoints:
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

    # ── Notifier handlers (server-side, called when a remote node sends us a topic message) ──

    async def _handle_notify(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, data: dict
    ):
        """Handle MSG_NOTIFY: fan out to local subscribers."""
        try:
            topic = data.get("topic")
            args = data.get("args")
            author = data.get("author", "remote")
            author_id = data.get("author_id", "remote")

            self._logger.info(f"[NOTIFY] Remote notify for topic '{topic}'")

            count = await self.plugin_core.notify(
                topic,
                args,
                hosts="local",
                author=author,
                author_id=author_id,
            )
            await self._send_message(writer, MSG_RESULT, {"count": count})
        except Exception as e:
            self._logger.exception(f"[NOTIFY] Exception: {e}")
            await self._send_error(writer, str(e))

    async def _handle_topic_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, data: dict
    ):
        """Handle MSG_TOPIC_REQUEST: find local handler and return result."""
        try:
            topic = data.get("topic")
            args = data.get("args")
            author = data.get("author", "remote")
            author_id = data.get("author_id", "remote")
            timeout = data.get("timeout")

            self._logger.info(f"[TOPIC_REQUEST] Remote request for topic '{topic}'")

            result = await self.plugin_core.request_topic(
                topic,
                args,
                hosts="local",
                author=author,
                author_id=author_id,
                timeout=timeout,
            )

            # Send result using same chunked protocol as _handle_execute
            payload = pickle.dumps(result)
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
                chunk_length = len(payload) + 1
                header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                writer.write(header + payload)
                await writer.drain()

            await self._send_end_stream(writer)
        except Exception as e:
            self._logger.exception(f"[TOPIC_REQUEST] Exception: {e}")
            await self._send_error(writer, str(e))

    async def _handle_topic_request_stream(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, data: dict
    ):
        """Handle MSG_TOPIC_REQUEST_STREAM: find local handler and stream results."""
        try:
            topic = data.get("topic")
            args = data.get("args")
            author = data.get("author", "remote")
            author_id = data.get("author_id", "remote")
            timeout = data.get("timeout")

            self._logger.info(
                f"[TOPIC_STREAM] Remote stream request for topic '{topic}'"
            )

            async for chunk in self.plugin_core.request_topic_stream(
                topic,
                args,
                hosts="local",
                author=author,
                author_id=author_id,
                timeout=timeout,
            ):
                payload = pickle.dumps(chunk)
                chunk_length = len(payload) + 1
                header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                writer.write(header + payload)
                await writer.drain()

            await self._send_end_stream(writer)
        except Exception as e:
            self._logger.exception(f"[TOPIC_STREAM] Exception: {e}")
            await self._send_error(writer, str(e))

    async def _handle_ping(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, data: dict
    ):
        """Handle PING message."""
        try:
            await self._send_message(writer, MSG_RESULT, {"status": "ok"})
        except Exception as e:
            self._logger.exception("Exception in _handle_ping")
            await self._send_error(writer, str(e))

    async def _handle_info(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, data: dict
    ):
        """Handle INFO message."""
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
                if isinstance(client_listener_port, int) and client_listener_port > 0:
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
        """Create a new TLS connection to a node.

        Resolves the per-node port via _resolve_port(IP). Connections to two
        peers on the same IP with different ports are tracked separately.
        """
        port = self._resolve_port(IP)
        self._logger.debug(f"[CONNECTION] Creating new TLS connection to {IP}:{port}")
        # Create SSL context for client
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False  # Allow self-signed certs
        ssl_context.verify_mode = (
            ssl.CERT_NONE
        )  # For testing - production should verify

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

        # Authenticate with shared secret
        self._logger.debug(f"[CONNECTION] Authenticating with {IP}")
        await self._send_message(writer, MSG_AUTH, self.secret)
        self._logger.debug(f"[CONNECTION] Authentication message sent to {IP}")

        # Wait for auth confirmation
        try:
            msg_type, data = await asyncio.wait_for(
                self._receive_message(reader), timeout=5.0
            )
            if msg_type == MSG_ERROR:
                raise ConnectionError(f"Authentication failed: {data}")
            if msg_type != MSG_RESULT:
                raise ConnectionError(f"Unexpected auth response type: {msg_type}")
            self._logger.debug(f"[CONNECTION] Authenticated with {IP}")
        except asyncio.TimeoutError:
            writer.close()
            await writer.wait_closed()
            raise ConnectionError(f"Authentication timeout with {IP}")

        return reader, writer

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
                        # Error message is pickled, so unpickle it
                        error_data = pickle.loads(payload)
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
                result = pickle.loads(full_pickled)
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
            raise NetworkRequestException(f"Remote execution failed: {e}")
        finally:
            # Return connection to pool (or close if error)
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

    @async_gen_handle_errors(None)
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
                        # Item boundary — reconstruct and yield accumulated chunks
                        if current_item_chunks:
                            full_pickled = b"".join(current_item_chunks)
                            try:
                                item = pickle.loads(full_pickled)
                                # Check if it's an error marker
                                if isinstance(item, tuple) and len(item) == 2:
                                    if item[0] == "__STREAM_ERROR__":
                                        self._logger.exception(
                                            f"Stream error from {IP}: {item[1]}"
                                        )
                                        yield (
                                            "__REMOTE_STREAM_DECODE_ERROR__",
                                            item[1],
                                        )
                                        break
                                    elif item[0] == "__STREAM_EXCEPTION__":
                                        self._logger.exception(
                                            f"Stream exception from {IP}: {item[1]}"
                                        )
                                        yield ("__REMOTE_STREAM_ERROR__", item[1])
                                        break
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
                                item = pickle.loads(full_pickled)
                                items_yielded += 1
                                yield item
                            except Exception as e:
                                self._logger.exception(
                                    f"Failed to unpickle final stream item from {IP}"
                                )
                        break
                    elif msg_type == MSG_ERROR:
                        error_data = pickle.loads(payload)
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
                    # Item boundary with no payload — same handling
                    if current_item_chunks:
                        full_pickled = b"".join(current_item_chunks)
                        try:
                            item = pickle.loads(full_pickled)
                            items_yielded += 1
                            yield item
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
                            item = pickle.loads(full_pickled)
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

        except Exception as e:
            self._logger.exception(f"Error in execute_remote_stream to {IP}")
            yield ("__REMOTE_STREAM_ERROR__", str(e))
        finally:
            # Return connection to pool (or close if error)
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

    # ── Notifier client methods (called by PluginCore to reach remote nodes) ──

    async def notify_remote(
        self,
        IP: str,
        topic: str,
        args=None,
        author: str = "remote",
        author_id: str = "remote",
    ) -> int:
        """Send a fire-and-forget notification to a remote node."""
        reader = None
        writer = None
        connection_returned = False
        try:
            reader, writer = await self._get_connection(IP)
            request_data = {
                "topic": topic,
                "args": args,
                "author": author,
                "author_id": author_id,
            }
            await self._send_message(writer, MSG_NOTIFY, request_data)

            msg_type, data = await self._receive_message(reader)
            if msg_type == MSG_ERROR:
                self._logger.warning(
                    f"[NOTIFY_REMOTE] Node {IP} returned error: {data}"
                )
                return 0

            await self._return_connection(IP, reader, writer)
            connection_returned = True
            return data.get("count", 0) if isinstance(data, dict) else 0
        except Exception as e:
            self._logger.exception(f"[NOTIFY_REMOTE] Error notifying {IP}: {e}")
            return 0
        finally:
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    async def request_topic_remote(
        self,
        IP: str,
        topic: str,
        args=None,
        author: str = "remote",
        author_id: str = "remote",
        timeout=None,
    ):
        """Request-by-topic on a remote node, returning the result."""
        reader = None
        writer = None
        connection_returned = False
        try:
            reader, writer = await self._get_connection(IP)
            request_data = {
                "topic": topic,
                "args": args,
                "author": author,
                "author_id": author_id,
                "timeout": timeout,
            }
            await self._send_message(writer, MSG_TOPIC_REQUEST, request_data)

            # Receive chunked response (same protocol as execute_remote)
            result_chunks_bytes = []
            while True:
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
                        result_chunks_bytes.append(payload)
                    elif msg_type == MSG_END_STREAM:
                        break
                    elif msg_type == MSG_ERROR:
                        error_data = pickle.loads(payload)
                        raise NetworkRequestException(str(error_data))
                    else:
                        raise NetworkRequestException(
                            f"Unexpected msg type: {msg_type}"
                        )
                elif msg_type == MSG_END_STREAM:
                    break

            await self._return_connection(IP, reader, writer)
            connection_returned = True

            if result_chunks_bytes:
                full_pickled = b"".join(result_chunks_bytes)
                return pickle.loads(full_pickled)
            return REMOTE_NO_RESULT
        except Exception as e:
            self._logger.exception(f"[TOPIC_REQUEST_REMOTE] Error from {IP}: {e}")
            raise
        finally:
            if reader and writer and not connection_returned:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    async def request_topic_stream_remote(
        self,
        IP: str,
        topic: str,
        args=None,
        author: str = "remote",
        author_id: str = "remote",
        timeout=None,
    ):
        """Request-by-topic streaming on a remote node, yielding results."""
        reader = None
        writer = None
        connection_returned = False
        try:
            reader, writer = await self._get_connection(IP)
            request_data = {
                "topic": topic,
                "args": args,
                "author": author,
                "author_id": author_id,
                "timeout": timeout,
            }
            await self._send_message(writer, MSG_TOPIC_REQUEST_STREAM, request_data)

            while True:
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
                        yield pickle.loads(payload)
                    elif msg_type == MSG_END_STREAM:
                        break
                    elif msg_type == MSG_ERROR:
                        error_data = pickle.loads(payload)
                        raise NetworkRequestException(str(error_data))
                    else:
                        raise NetworkRequestException(
                            f"Unexpected msg type: {msg_type}"
                        )
                elif msg_type == MSG_END_STREAM:
                    break

            await self._return_connection(IP, reader, writer)
            connection_returned = True
        except Exception as e:
            self._logger.exception(f"[TOPIC_STREAM_REMOTE] Error from {IP}: {e}")
            raise
        finally:
            if reader and writer and not connection_returned:
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
                    node.enabled = False
                raise NetworkRequestException("Couldnt reach host")

            # Check if it's a MockResponse (418 error)
            if hasattr(response, "status_code") and response.status_code == 418:
                node = await self._get_node(IP)
                if node:
                    node.enabled = False
                raise NodeException("Host is not discoverable")

            # Response is now a dict, not an httpx.Response
            if not isinstance(response, dict):
                node = await self._get_node(IP)
                if node:
                    node.enabled = False
                raise NetworkRequestException(f"Invalid response format from {IP}")

            # Update node info and mark enabled
            node = await self._get_node(IP)
            if node:
                node.enabled = True
                await node.update(response, self.plugin_core.hostname)

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
                return None

        except Exception as e:
            self._logger.debug(f"[GET_INFO] Failed to reach {IP}: {e}")
            return None
        finally:
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
        (await self._get_node(IP)).enabled = False

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
                return True
            return False

        except Exception as e:
            self._logger.debug(
                f"Pinging Node with IP {node.IP} was not successful: {e}"
            )
            return False
        finally:
            if reader and writer and not connection_returned:
                try:
                    self._logger.debug(
                        f"[HEARTBEAT] Returning connection for {node.IP}"
                    )
                    await self._return_connection(node.IP, reader, writer)
                    connection_returned = True
                except Exception:
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
                return None

        except Exception as e:
            self._logger.exception(f"[ENDPOINT] Error checking endpoint on {IP}: {e}")
            return None
        finally:
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

            return None

        except Exception as e:
            self._logger.exception(
                f"[TAG_SEARCH] Error querying node {IP} for tag '{tag}': {e}"
            )
            return None
        finally:
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
