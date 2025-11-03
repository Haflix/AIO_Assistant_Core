from logging import Logger
import ssl
import socket
import asyncio
import pickle
import struct
import os
from typing import List, Union, Optional, Tuple
from decorators import async_log_errors, async_handle_errors
from exceptions import NetworkRequestException, NodeException
from networking_classes import Node
from networking_classes import RemotePlugin


# Message type constants
MSG_EXECUTE = 1
MSG_EXECUTE_STREAM = 2
MSG_HAS_ENDPOINT = 3
MSG_PING = 4
MSG_INFO = 5

MSG_RESULT = 10
MSG_STREAM_CHUNK = 11
MSG_ERROR = 12
MSG_END_STREAM = 13

MSG_AUTH = 20  # Authentication message (shared secret)

CHUNK_SIZE = 64 * 1024  # 64KB chunks for streaming
MAX_MESSAGE_SIZE = 100 * 1024 * 1024  # 100MB max message size


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

        self.node_ips = node_ips

        self.discover_nodes = discover_nodes
        self.direct_discoverable = direct_discoverable
        self.auto_discoverable = auto_discoverable

        self.port = port
        self.nodes: list[Node] = []

        # Security configuration
        self.secret = secret or os.getenv("NETWORKING_SECRET", "").encode()
        if isinstance(self.secret, str):
            self.secret = self.secret.encode()
        self.cert_file = cert_file
        self.key_file = key_file
        self.pool_size = pool_size

        # Connection pools: dict[IP -> asyncio.Queue[Tuple[reader, writer]]]
        self.connection_pools: dict[str, asyncio.Queue] = {}

        # Server state
        self.server = None
        self.server_task = None

        # SSL context (will be initialized in start)
        self.ssl_context = None

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

            writer.write(header + payload)
            await writer.drain()
        except Exception as e:
            self._logger.exception(f"Error sending message type {msg_type}")
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

            return msg_type, data
        except asyncio.IncompleteReadError as e:
            self._logger.debug(f"Incomplete read: {e}")
            raise ConnectionError("Connection closed unexpectedly")
        except Exception as e:
            self._logger.exception("Error receiving message")
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
                                x509.IPAddress(socket.inet_aton("127.0.0.1")),
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

    async def stop(self):
        """Stop the socket server and close all connections."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()

        if self.server_task:
            self.server_task.cancel()
            try:
                await self.server_task
            except asyncio.CancelledError:
                pass

        # Close all pooled connections
        for ip, pool in self.connection_pools.items():
            while not pool.empty():
                try:
                    reader, writer = await asyncio.wait_for(pool.get(), timeout=0.1)
                    writer.close()
                    await writer.wait_closed()
                except (asyncio.TimeoutError, Exception):
                    break

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
            msg_type, data = await self._receive_message(reader)
            if msg_type != MSG_AUTH:
                await self._send_error(
                    writer, "Authentication required as first message"
                )
                return

            if data != self.secret:
                await self._send_error(writer, "Authentication failed")
                self._logger.warning(f"Authentication failed for {client_addr}")
                return

            # Authentication successful, process requests
            while True:
                msg_type, data = await self._receive_message(reader)

                if msg_type == MSG_EXECUTE:
                    await self._handle_execute(reader, writer, data)
                elif msg_type == MSG_EXECUTE_STREAM:
                    await self._handle_execute_stream(reader, writer, data)
                elif msg_type == MSG_HAS_ENDPOINT:
                    await self._handle_has_endpoint(reader, writer, data)
                elif msg_type == MSG_PING:
                    await self._handle_ping(reader, writer, data)
                elif msg_type == MSG_INFO:
                    await self._handle_info(reader, writer, data)
                else:
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

            # Execute plugin method
            if isinstance(args, (list, tuple)):
                result = await self.plugin_core.execute(
                    plugin,
                    method,
                    *args,
                    plugin_uuid=plugin_uuid,
                    host="local",
                    timeout=timeout,
                    author=author,
                    author_id=author_id,
                    author_host=author_host,
                    request_id=request_id,
                )
            else:
                result = await self.plugin_core.execute(
                    plugin,
                    method,
                    args,
                    plugin_uuid=plugin_uuid,
                    host="local",
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
                offset = 0
                while offset < len(payload):
                    chunk_data = payload[offset : offset + CHUNK_SIZE]
                    chunk_length = len(chunk_data) + 1
                    header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                    writer.write(header + chunk_data)
                    await writer.drain()
                    offset += CHUNK_SIZE
            else:
                # Small result, send as single chunk
                chunk_length = len(payload) + 1
                header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                writer.write(header + payload)
                await writer.drain()

            await self._send_end_stream(writer)

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

            # Execute streaming plugin method
            if isinstance(args, (list, tuple)):
                agen = self.plugin_core.execute_stream(
                    plugin=plugin,
                    method=method,
                    *args,
                    plugin_uuid=plugin_uuid,
                    host="local",
                    author=author,
                    author_id=author_id,
                    timeout=timeout,
                    author_host=author_host,
                    request_id=request_id,
                )
            else:
                agen = self.plugin_core.execute_stream(
                    plugin=plugin,
                    method=method,
                    args=args,
                    plugin_uuid=plugin_uuid,
                    host="local",
                    author=author,
                    author_id=author_id,
                    timeout=timeout,
                    author_host=author_host,
                    request_id=request_id,
                )

            # Stream results - each yielded item as a chunk
            async for line in agen:
                try:
                    # Send each item - may split if very large
                    payload = pickle.dumps(line)
                    if len(payload) > CHUNK_SIZE:
                        # Split large item into chunks
                        offset = 0
                        while offset < len(payload):
                            chunk_data = payload[offset : offset + CHUNK_SIZE]
                            chunk_length = len(chunk_data) + 1
                            header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                            writer.write(header + chunk_data)
                            await writer.drain()
                            offset += CHUNK_SIZE
                    else:
                        # Small item, send as single chunk
                        chunk_length = len(payload) + 1
                        header = struct.pack(">IB", chunk_length, MSG_STREAM_CHUNK)
                        writer.write(header + payload)
                        await writer.drain()
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
        try:
            access_name = data.get("access_name")
            plugin_uuid = data.get("plugin_uuid")
            requester_id = data.get("requester_id")
            target_plugin = data.get("target_plugin")

            # Use find_endpoint which already does both checks
            plugin, endpoint, node = await self.plugin_core.find_endpoint(
                access_name=access_name,
                host="local",
                plugin_uuid=plugin_uuid,
                requester_id=requester_id,
                target_plugin=target_plugin,
            )

            response = {
                "available": plugin is not None and endpoint is not None,
                "hostname": self.plugin_core.hostname,
            }

            if plugin:
                response["plugin_info"] = {
                    "name": plugin.plugin_name,
                    "version": getattr(plugin, "version", "unknown"),
                    "uuid": plugin.plugin_uuid,
                    "description": getattr(plugin, "description", "Remote plugin"),
                }
            else:
                response["plugin_info"] = None

            if endpoint:
                response["endpoint"] = endpoint
            else:
                response["endpoint"] = None

            await self._send_message(writer, MSG_RESULT, response)

        except Exception as e:
            self._logger.exception("Exception in _handle_has_endpoint")
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
                if self.discover_nodes and client_ip not in self.node_ips:
                    self.node_ips.append(client_ip)

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

            await self._send_message(writer, MSG_RESULT, response)

        except Exception as e:
            self._logger.exception("Exception in _handle_info")
            await self._send_error(writer, str(e))

    # Client-side Connection Pool Management

    async def _create_connection(
        self, IP: str
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Create a new TLS connection to a node."""
        # Create SSL context for client
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_context.check_hostname = False  # Allow self-signed certs
        ssl_context.verify_mode = (
            ssl.CERT_NONE
        )  # For testing - production should verify

        reader, writer = await asyncio.open_connection(
            IP,
            self.port,
            ssl=ssl_context,
        )

        # Authenticate with shared secret
        await self._send_message(writer, MSG_AUTH, self.secret)

        # Wait for auth confirmation (server should not send error)
        # For now, we'll just proceed - if auth fails, the next message will error

        return reader, writer

    async def _get_connection(
        self, IP: str
    ) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Get a connection from pool or create new one."""
        if IP not in self.connection_pools:
            self.connection_pools[IP] = asyncio.Queue(maxsize=self.pool_size)

        pool = self.connection_pools[IP]

        # Try to get from pool
        if not pool.empty():
            try:
                reader, writer = await asyncio.wait_for(pool.get(), timeout=0.1)
                # Health check - try a ping
                try:
                    await self._send_message(writer, MSG_PING, {})
                    msg_type, _ = await asyncio.wait_for(
                        self._receive_message(reader), timeout=2.0
                    )
                    if msg_type == MSG_RESULT:
                        return reader, writer
                    else:
                        # Connection is bad, close it
                        writer.close()
                        await writer.wait_closed()
                except Exception:
                    # Connection is bad, close it and create new
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
            except asyncio.TimeoutError:
                pass

        # Create new connection
        return await self._create_connection(IP)

    async def _return_connection(
        self, IP: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        """Return a connection to the pool."""
        if IP not in self.connection_pools:
            self.connection_pools[IP] = asyncio.Queue(maxsize=self.pool_size)

        pool = self.connection_pools[IP]

        try:
            pool.put_nowait((reader, writer))
        except asyncio.QueueFull:
            # Pool is full, close connection
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
            reader, writer = await self._get_connection(IP)

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
            await self._send_message(writer, MSG_EXECUTE, request_data)

            # Receive response (may be chunked if large)
            result_chunks_bytes = []
            while True:
                # Read raw message to get pickled bytes (don't unpickle yet for chunks)
                length_bytes = await reader.readexactly(4)
                msg_length = struct.unpack(">I", length_bytes)[0]

                msg_type_byte = await reader.readexactly(1)
                msg_type = msg_type_byte[0]

                payload_length = msg_length - 1
                if payload_length > 0:
                    payload = await reader.readexactly(payload_length)

                    if msg_type == MSG_STREAM_CHUNK:
                        # Collect raw pickled bytes
                        result_chunks_bytes.append(payload)
                    elif msg_type == MSG_END_STREAM:
                        break
                    elif msg_type == MSG_ERROR:
                        # Error message is pickled, so unpickle it
                        error_data = pickle.loads(payload)
                        if (
                            isinstance(error_data, tuple)
                            and len(error_data) == 2
                            and error_data[0] == "__STREAM_ERROR__"
                        ):
                            raise NetworkRequestException(error_data[1])
                        raise NetworkRequestException(str(error_data))
                    else:
                        raise NetworkRequestException(
                            f"Unexpected message type: {msg_type}"
                        )
                elif msg_type == MSG_END_STREAM:
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
                return result
            else:
                return None

        except Exception as e:
            self._logger.exception(f"Error in execute_remote to {IP}")
            raise NetworkRequestException(f"Remote execution failed: {e}")
        finally:
            # Return connection to pool (or close if error)
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
            reader, writer = await self._get_connection(IP)

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
            await self._send_message(writer, MSG_EXECUTE_STREAM, request_data)

            # Stream results - collect chunks until we have a complete item
            current_item_chunks = []
            while True:
                # Read raw message
                length_bytes = await reader.readexactly(4)
                msg_length = struct.unpack(">I", length_bytes)[0]

                msg_type_byte = await reader.readexactly(1)
                msg_type = msg_type_byte[0]

                payload_length = msg_length - 1
                if payload_length > 0:
                    payload = await reader.readexactly(payload_length)

                    if msg_type == MSG_STREAM_CHUNK:
                        current_item_chunks.append(payload)
                        # Check if this is the last chunk of an item (smaller than CHUNK_SIZE)
                        # Actually, we can't know from just one chunk. We need to check if next message is END_STREAM or another chunk.
                        # For now, we'll peek ahead or use a different approach.
                        # Better: send a marker between items, OR accumulate until we get END_STREAM or next item starts.
                        # Actually, for streaming generators, each complete item should be sent as one or more chunks,
                        # then END_STREAM marks the end of the stream (all items).
                        # So we need to know when an item is complete. Let's use a heuristic: if chunk is smaller than CHUNK_SIZE,
                        # it might be the last chunk of an item. But this isn't reliable.

                        # Better solution: Send each item as atomic - if large, split pickled bytes, but mark item boundaries.
                        # For now, simpler: accumulate all chunks until END_STREAM, then yield items.
                        # But that defeats streaming...

                        # Actually, the protocol should be: each generator item = one or more STREAM_CHUNK messages (if item is large),
                        # followed by a special marker or we just know by size.
                        # Simplest: assume if we receive a chunk smaller than CHUNK_SIZE and it's not the first chunk,
                        # it's the last chunk of an item. Then unpickle and yield.

                        # Check if this completes an item (heuristic: small chunk after a large one, or first small chunk)
                        if len(payload) < CHUNK_SIZE:
                            # This might be the last chunk of an item - reconstruct and yield
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
                                    yield item
                                    current_item_chunks = []
                                except Exception as e:
                                    self._logger.exception(
                                        f"Failed to unpickle stream item from {IP}"
                                    )
                                    yield ("__REMOTE_STREAM_DECODE_ERROR__", str(e))
                                    current_item_chunks = []
                    elif msg_type == MSG_END_STREAM:
                        # End of stream - yield any remaining chunks as final item
                        if current_item_chunks:
                            full_pickled = b"".join(current_item_chunks)
                            try:
                                item = pickle.loads(full_pickled)
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
                        raise NetworkRequestException(error_msg)
                    else:
                        raise NetworkRequestException(
                            f"Unexpected message type: {msg_type}"
                        )
                elif msg_type == MSG_END_STREAM:
                    # End of stream - yield any remaining chunks
                    if current_item_chunks:
                        full_pickled = b"".join(current_item_chunks)
                        try:
                            item = pickle.loads(full_pickled)
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
        additional_IP_list: list[str] = [],
        timeout: int = 5,
        ignore_enabled_status: bool = False,
    ) -> List[Node]:

        self.node_ips.extend(additional_IP_list)

        await self._create_nodes(self.node_ips)

        tasks = [
            self.update_single(IP, timeout)
            for IP in self.node_ips
            if (await self._get_node(IP)).enabled or ignore_enabled_status
        ]
        results = await asyncio.gather(*tasks)

        return self.nodes

    @async_log_errors
    async def update_single(self, IP: str, timeout: int = 5):

        await self._create_new_node(IP)

        try:
            response = await self._get_ip_info(IP, timeout=timeout)

            if not response:
                raise NetworkRequestException("Couldnt reach host")

            # Check if it's a MockResponse (418 error)
            if hasattr(response, "status_code") and response.status_code == 418:
                (await self._get_node(IP)).enabled = False
                raise NodeException("Host is not discoverable")

            # Response is now a dict, not an httpx.Response
            if not isinstance(response, dict):
                raise NetworkRequestException(f"Invalid response format from {IP}")

            await (await self._get_node(IP)).update(response, self.plugin_core.hostname)

            for sub_node in response.get("nodes", []):
                if (
                    sub_node[0] not in self.node_ips
                    and sub_node[1] != self.plugin_core.hostname
                ):
                    await self._add_ip(sub_node[0])

                    await self._create_new_node(sub_node[0], sub_node[1])

                    await self.update_single(sub_node[0])

                if sub_node[1] != self.plugin_core.hostname:
                    self._logger.info(f"[DISCOVERY] Node found at {IP}")

                else:
                    self._logger.info(f"[DISCOVERY] Found own node at {IP}")

        except Exception as e:
            self._logger.debug(f"[DISCOVERY] Failed to reach {IP}: {e}")

    @async_log_errors
    async def _add_ip(self, IP):
        await self.node_ips.append(IP)

    @async_log_errors
    async def _create_nodes(self, IP_list: list):
        for IP in IP_list:
            await self._create_new_node(IP)

    @async_log_errors
    async def _create_new_node(self, IP: str, hostname: Union[str, None] = None):
        if not await self.node_exists(IP):

            self.nodes.append(
                Node(IP=IP, hostname=hostname, enabled=True, auto_discoverable=False)
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
            reader, writer = await self._get_connection(IP)

            request_data = {
                "hostname": self.plugin_core.hostname,
                "discover_nodes_info": self.discover_nodes,
            }

            await self._send_message(writer, MSG_INFO, request_data)

            msg_type, data = await self._receive_message(reader)

            if msg_type == MSG_RESULT:
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
        self.nodes.remove(await self._get_node(IP))

    @async_log_errors
    async def _enable_node(self, IP: str):
        (await self._get_node(IP)).enabled = True

    @async_log_errors
    async def _disable_node(self, IP: str):
        (await self._get_node(IP)).enabled = False

    @async_log_errors
    async def node_exists(self, IP: str):  # FIXME: Add search for hostname
        for node in self.nodes:
            if node.IP == IP:
                return True

        return False

    @async_log_errors
    async def _get_node(
        self, IP: str, hostame: Union[str, None] = None, autogenerate: bool = False
    ) -> Node:  # FIXME: Get Node only by hostname if theres no duplicate?

        if autogenerate:
            await self._create_new_node(IP=IP, hostname=hostame)

        for node in self.nodes:
            if node.IP == IP:
                if node.hostname == hostame or hostame == None:
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
        )

    async def heartbeat_node(self, node: Node, timeout=5):
        """Ping a node to check if it's alive."""
        reader = None
        writer = None
        connection_returned = False

        try:
            reader, writer = await self._get_connection(node.IP)

            await self._send_message(writer, MSG_PING, {})

            msg_type, data = await asyncio.wait_for(
                self._receive_message(reader), timeout=timeout
            )

            if msg_type == MSG_RESULT and data.get("status") == "ok":
                await node.heartbeat()
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

        try:
            reader, writer = await self._get_connection(IP)

            request_data = {
                "access_name": access_name,
                "plugin_uuid": plugin_uuid,
                "requester_id": requester_id,
                "target_plugin": target_plugin,
            }

            await self._send_message(writer, MSG_HAS_ENDPOINT, request_data)

            msg_type, data = await self._receive_message(reader)

            if msg_type == MSG_RESULT:
                return data
            elif msg_type == MSG_ERROR:
                self._logger.debug(f"Node {IP} returned error for has_endpoint: {data}")
                return None
            else:
                self._logger.warning(f"Unexpected message type {msg_type} from {IP}")
                return None

        except Exception as e:
            self._logger.debug(f"Error checking endpoint on {IP}: {e}")
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
        DEPRECATED: Use node_has_endpoint instead. This method uses node_has_endpoint with access_name=None.
        """
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


#    async def discover_nodes(self, cidr_range=None, timeout=10):
#        if cidr_range is None:
#            #hostname = socket.gethostname()
#            #local_ip = socket.gethostbyname(hostname)
#            networks = [ipaddress.ip_network(f"{self.network_ip}/24", strict=False)]
#        else:
#            networks = [ipaddress.ip_network(cidr_range, strict=False)]
#
#        ips_to_scan = [str(ip) for network in networks for ip in network.hosts()]
#        sem = asyncio.Semaphore(20)
#        active_nodes = []
#
#        @async_handle_errors(None)
#        async def _probe_node(ip, client, sem):
#            async with sem:
#                try:
#                    self._logger.debug(f"Trying to find node on http://{ip}:{self.port}/plugins")
#                    response = await client.get(
#                        f"http://{ip}:{self.port}/plugins"
#                    )
#                    if response.status_code == 200:
#                        return ip
#                except (httpx.ConnectError, httpx.TimeoutException):
#                    pass
#                except Exception:
#                    self._logger.warning(f"Exception while trying to find node on http://{ip}:{self.port}/plugins: ")
#                    pass
#            return None
#
#        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout=timeout)) as client:
#        #async with httpx.AsyncClient(timeout=httpx.Timeout(connect=0.1, read=0.2, write=0.2, pool=0.5)) as client:
#            tasks = [asyncio.create_task(_probe_node(ip, client, sem)) for ip in ips_to_scan]
#
#            for task in asyncio.as_completed(tasks):
#                result = await task
#                if result:
#                    active_nodes.append(result)
#                    self._logger.info(f"[DISCOVERY] Found active node: {result}")
#
#        self.nodes = active_nodes
#        return active_nodes


#    async def find_plugin_on_nodes(self, plugin_name: str):
#        """Check all known nodes for the requested plugin."""
#        found_hosts = []
#        for node in self.nodes:
#            try:
#                async with httpx.AsyncClient(timeout=2.0) as client:
#                    response = await client.get(f"http://{node.ip}:{self.port}/plugins")
#                    if response.status_code == 200:
#                        if plugin_name in response.json():
#                            found_hosts.append(node)
#            except:
#                continue
#        return found_hosts
