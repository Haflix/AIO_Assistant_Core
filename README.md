# AIO Assistant Core Documentation

## Table of Contents

1. [Overview](#overview)
2. [Getting Started](#getting-started)
3. [Architecture](#architecture)
4. [Core Components](#core-components)
5. [Plugin System](#plugin-system)
6. [Networking](#networking)
7. [Configuration](#configuration)
8. [API Reference](#api-reference)
9. [Error Handling](#error-handling)
10. [CLI Plugin](#cli-plugin)
11. [Examples](#examples)

---

## Overview

**AIO Assistant Core** is a powerful plugin loader and management system designed to make connecting scripts (both synchronous and asynchronous) easier across multiple devices. It provides a unified framework for plugin communication, whether locally or over a network.

### Key Features

- **Hot-Pluggable Plugins**: Plugins can be loaded, enabled, disabled, reloaded, and removed dynamically at runtime
- **Synchronous & Asynchronous Support**: Seamless integration between sync and async code, including sync/async generators
- **Network Communication**: TLS-encrypted TCP socket networking with shared-secret authentication for distributed plugin execution across devices
- **Connection Pooling**: Efficient reuse of network connections with configurable pool sizes
- **Endpoint-Based Routing**: Fine-grained access control per endpoint with `accessible_by_other_plugins` and `remote` flags
- **Simple API**: One-liner syntax for executing plugin methods
- **Topic-Based Notifier**: Pub/sub and request-by-topic system for decoupled plugin communication with wildcard support
- **Streaming Support**: Full support for both sync and async generator-based data streams
- **Built-in CLI**: Interactive command-line interface for managing plugins at runtime
- **Error Handling**: Comprehensive decorator-based error handling for sync functions, async functions, sync generators, and async generators
- **Non-Blocking Logging**: Thread-safe queue-based logging with colored console output and file logging

---

## Getting Started

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd AIO_Assistant_Core

# Install core dependencies
pip install pyyaml colorama
# For networking with auto-generated TLS certificates:
pip install cryptography
# For the CLI plugin:
pip install prompt_toolkit

# Plugin-specific dependencies are listed in requirements.txt
# (only install what you need for the plugins you plan to use)
```

### Basic Usage

```python
import asyncio
from PluginCore import PluginCore

async def main():
    plugin_core = PluginCore("config.yml")

    # Wait for plugins to be loaded and networking to start
    await plugin_core.wait_until_ready()

    # Execute a plugin method
    result = await plugin_core.execute("PluginB", "calculate_square", 6, host="local")
    print(f"Result: {result}")

    # Graceful shutdown
    await plugin_core.close()

if __name__ == "__main__":
    asyncio.run(main())
```

### Production Pattern: Signal Handling and Shutdown Event

The included `main_application.py` demonstrates a production-ready pattern with signal handlers and a shutdown event to keep the application alive until explicitly stopped:

```python
import asyncio
import signal
from PluginCore import PluginCore

_plugin_core = None

async def main():
    global _plugin_core
    try:
        _plugin_core = PluginCore("config.yml")

        # Register signal handlers for graceful shutdown
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown(s)))
            except NotImplementedError:
                pass  # Windows — handled by KeyboardInterrupt

        await _plugin_core.wait_until_ready()

        # Keep alive until shutdown is triggered
        _shutdown_event = asyncio.Event()
        _plugin_core._shutdown_event = _shutdown_event
        await _shutdown_event.wait()
    finally:
        if _plugin_core is not None:
            await _plugin_core.graceful_shutdown()
            _plugin_core = None

async def shutdown(sig=None):
    global _plugin_core
    if _plugin_core is not None:
        if hasattr(_plugin_core, "_shutdown_event"):
            _plugin_core._shutdown_event.set()
        await _plugin_core.graceful_shutdown()
        _plugin_core = None

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user")
```

### Alternative: Using `start()` / `close()`

```python
async def main():
    plugin_core = PluginCore("config.yml")

    # start() initializes background tasks, loads plugins, and starts networking
    await plugin_core.start()

    result = await plugin_core.execute("PluginB", "calculate_square", 6, host="local")
    print(f"Result: {result}")

    await plugin_core.close()
```

---

## Architecture

### High-Level Flow

```
1. main_application.py loads PluginCore with config.yml
2. PluginCore validates config and applies settings (hostname, networking, etc.)
3. PluginCore loads plugins based on configuration
4. PluginCore initializes NetworkManager (if enabled) with TLS and shared-secret auth
5. Application can execute plugin methods via PluginCore.execute()
6. PluginCore uses find_endpoint() to locate plugins locally or on remote nodes
7. Requests are created and processed, results returned to the caller
8. Background maintenance loop cleans up completed tasks and collected requests
```

### Component Interaction

```
┌──────────────────┐
│  main_application│
└────────┬─────────┘
         │
         ▼
┌─────────────────┐
│   PluginCore    │◄───────┐
│                 │        │
│  ┌───────────┐  │   ┌────┴──────┐
│  │  Request  │  │   │  Network  │
│  │  Manager  │  │   │  Manager  │
│  └───────────┘  │   │  (TLS/TCP)│
│                 │   └───────────┘
│  ┌───────────┐  │
│  │  Plugin   │  │
│  │  Registry │  │
│  └───────────┘  │
└─────────────────┘
         │
    ┌────┴────┐
    │ Plugins │
    └─────────┘
```

---

## Core Components

### PluginCore

The main class that manages all plugins and facilitates communication between them.

**Location**: `PluginCore.py`

**Key Responsibilities**:

- Loading and managing plugins from configuration
- Endpoint-based routing with access control
- Handling plugin requests (sync, async, and generator/streaming)
- Managing plugin lifecycle (load, enable, disable, reload, remove)
- Coordinating with NetworkManager for remote execution
- Request processing, result management, and cleanup

**Key Methods**:

- `start()` - Initialize background tasks, load plugins, and start networking
- `close()` - Gracefully shutdown background tasks and networking
- `wait_until_ready()` - Wait for plugins and network to finish loading
- `execute()` - Execute a plugin method asynchronously
- `execute_sync()` - Execute a plugin method synchronously
- `execute_stream()` - Execute a generator/streaming plugin method (async)
- `execute_stream_sync()` - Execute a generator/streaming plugin method (sync)
- `find_endpoint()` - Locate a plugin endpoint locally or remotely with access control
- `get_plugin_info()` - Get structured information about a loaded plugin
- `get_plugin_endpoints()` - Get all endpoints for a plugin
- `get_plugins()` - Load plugins from configuration
- `start_plugins()` - Enable all loaded plugins
- `purge_plugins()` - Unload all plugins
- `purge_plugins_except()` - Unload all plugins except specified ones
- `pop_plugin()` - Remove a specific plugin
- `graceful_shutdown()` - Gracefully shutdown the entire system

### NetworkManager

Manages network communication between multiple nodes for distributed plugin execution using TLS-encrypted TCP sockets with a binary protocol.

**Location**: `networking.py`

**Key Features**:

- TLS-encrypted TCP socket communication
- Shared-secret authentication on every connection
- Connection pooling with configurable pool sizes
- Binary message protocol using pickle serialization
- Node discovery (auto-discovery and manual node IPs)
- Endpoint availability checking across nodes via `node_has_endpoint()`
- Remote plugin execution and streaming
- Heartbeat-based liveness monitoring
- Background loops for discovery and heartbeat

**Message Protocol**:

Each message uses a binary format: `[4-byte length][1-byte message_type][pickle payload]`

| Message Type | Constant | Description |
|---|---|---|
| Execute | `MSG_EXECUTE` (1) | Execute a plugin method remotely |
| Execute Stream | `MSG_EXECUTE_STREAM` (2) | Execute a streaming plugin method remotely |
| Has Endpoint | `MSG_HAS_ENDPOINT` (3) | Check if a node has a specific endpoint |
| Ping | `MSG_PING` (4) | Health check / heartbeat |
| Info | `MSG_INFO` (5) | Exchange node information and discovery data |
| Result | `MSG_RESULT` (10) | Response with result data |
| Stream Chunk | `MSG_STREAM_CHUNK` (11) | A chunk of streaming data |
| Error | `MSG_ERROR` (12) | Error response |
| End Stream | `MSG_END_STREAM` (13) | Marks end of a stream |
| Auth | `MSG_AUTH` (20) | Authentication message (shared secret) |

### Plugin Base Class

All plugins must inherit from the `Plugin` base class.

**Location**: `utils.py`

**Required Methods**:

- `on_load(*args, **kwargs)` - Called when plugin is loaded (sync). Receives arguments from `plugin_config.yml`.
- `on_enable()` - Called when plugin is enabled (can be sync or async)
- `on_disable()` - Called when plugin is disabled (can be sync or async)

**Built-in Execution Methods** (available on every plugin instance):

- `execute()` - Async one-liner to call another plugin's method
- `execute_sync()` - Sync one-liner to call another plugin's method
- `execute_stream()` - Async generator to stream from another plugin's method
- `execute_stream_sync()` - Sync generator to stream from another plugin's method

**Properties**:

- `plugin_name` - Name of the plugin
- `version` - Plugin version
- `plugin_uuid` - Unique identifier for the plugin instance (auto-generated)
- `enabled` - Whether the plugin is currently enabled
- `remote` - Whether the plugin supports remote execution
- `description` - Plugin description
- `arguments` - Arguments passed during loading
- `endpoints` - List of endpoint configuration dicts
- `_logger` - Logger instance for the plugin
- `_plugin_core` - Reference to the PluginCore instance
- `event_loop` - Reference to the main event loop

### Request & GeneratorRequest

**Location**: `utils.py`

Request objects manage the lifecycle of plugin calls:

- `Request` - For standard (non-streaming) calls. Uses an `asyncio.Future` for result delivery.
- `GeneratorRequest` - For streaming/generator calls. Uses an `asyncio.Queue` for streaming results, with `EndOfQueue` sentinel to signal completion.

Both support timeouts, error tracking, and collection status for cleanup.

### Sync vs Async Plugins: Thread Pool Deadlock Risk

PluginCore supports both sync and async plugin methods seamlessly. However, there is a
critical architectural detail that plugin developers **must** understand:

**How sync methods are executed:**

When PluginCore calls a **sync** plugin method, it runs it in a thread pool via
`run_in_executor()`. This thread pool has a **fixed size** (Python default:
`min(32, cpu_count + 4)`, typically ~20 threads). While the method runs, it occupies one
thread.

**How `execute_sync()` works from inside a sync method:**

When a sync method calls `execute_sync()` to reach another plugin, it:
1. Submits a coroutine to the event loop via `run_coroutine_threadsafe()`
2. Calls `future.result()` which **blocks the current thread** until the result arrives
3. The event loop processes the request — if the target is sync, it needs **another thread**
   from the same pool

**The deadlock scenario:**

```
Thread pool (capacity: 2 for illustration)

Thread 1: ai_chat() → calls execute_sync("PostgreSQL", "pg_execute")
           → submits coroutine to event loop
           → blocks on future.result() ← WAITING FOR THREAD

Thread 2: another_method() → calls execute_sync("PostgreSQL", "pg_fetch")
           → submits coroutine to event loop
           → blocks on future.result() ← WAITING FOR THREAD

Event loop: receives both coroutines
           → calls run_in_executor(None, pg_execute)  ← NEEDS A FREE THREAD
           → calls run_in_executor(None, pg_fetch)    ← NEEDS A FREE THREAD
           → no threads available → DEADLOCK
```

Both threads are waiting for results that require threads to produce. The pool is both
the producer and the consumer. Once full, **nothing can finish because everything is
waiting for everything else**. This is not a slowdown — it is a permanent, silent deadlock.

**How to prevent it:**

1. **Prefer async methods.** Async plugin endpoints run directly in the event loop — no
   thread pool involvement, no deadlock risk. This is the recommended approach for any
   plugin that primarily does I/O (database, network, API calls).

2. **Use timeouts.** Always pass `timeout=` to `execute()` and `execute_sync()` calls.
   Without a timeout, a deadlocked request waits forever. With a timeout, it fails with a
   `RequestException` after the specified seconds, freeing the caller to recover.

3. **Avoid deep sync-to-sync call chains.** Each hop in a sync → `execute_sync` → sync
   chain consumes one thread. A chain of 3 sync plugins needs 3 threads simultaneously.
   Under concurrent load, this exhausts the pool quickly.

**Rule of thumb:** If your plugin does I/O (database queries, HTTP requests, API calls),
make its methods `async def`. Reserve sync methods for pure CPU-bound work that genuinely
needs a thread (e.g., audio processing, ML inference).

---

## Plugin System

### Creating a Plugin

1. **Create a plugin directory** (e.g., `plugins_test/MyPlugin/`)
2. **Create `plugin.py`**:

```python
from utils import Plugin
from decorators import async_log_errors, log_errors

class MyPlugin(Plugin):
    """Example plugin."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self.plugin_name = "MyPlugin"
        self.version = "1.0.0"
        self.description = "My custom plugin"

    @async_log_errors
    async def on_enable(self):
        self._logger.info("Plugin enabled!")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("Plugin disabled!")

    async def my_method(self, arg1, arg2):
        """Example method that can be called from other plugins."""
        return arg1 + arg2
```

3. **Create `plugin_config.yml`**:

```yaml
description: My custom plugin
version: 1.0.0
remote: True
arguments: []
endpoints:
  - internal_name: my_method
    access_name: my_method
    tags: []
    remote: True
    accessible_by_other_plugins: True
    description: Adds two numbers
    arguments:
      - name: arg1
        type: int
        description: First number
      - name: arg2
        type: int
        description: Second number
```

4. **Add to `config.yml`**:

```yaml
plugins:
  - name: MyPlugin
    enabled: true
    path: ./plugins_test/MyPlugin  # Optional: auto-resolves if omitted
```

### Plugin Lifecycle

1. **Loading**: Plugin class is dynamically imported, instantiated, and `on_load()` is called with arguments from `plugin_config.yml`
2. **Enabling**: `on_enable()` method is called (async or sync)
3. **Active**: Plugin is ready to receive requests via its registered endpoints
4. **Disabling**: `on_disable()` method is called (async or sync)
5. **Unloading**: Plugin instance is removed from the registry and UUID index

### Calling Other Plugins

From within a plugin:

```python
# Async execution
result = await self.execute("PluginName", "method_access_name", args, host="local")

# Sync execution (call from sync context, must not be called from async)
result = self.execute_sync("PluginName", "method_access_name", args, host="local")

# With keyword arguments
result = await self.execute("PluginName", "method_name", {"key": "value"}, host="any")

# With positional arguments as tuple
result = await self.execute("PluginName", "method_name", (arg1, arg2), host="any")

# Host options: "local", "remote", "any", or specific hostname
```

### Streaming/Generator Support

Plugins can expose both sync and async generators. Callers can consume them from either context:

```python
# Async context -> async generator endpoint
async for item in self.execute_stream("PluginA", "streaming_method", args, host="any"):
    print(item)

# Sync context -> any generator endpoint
for item in self.execute_stream_sync("PluginA", "streaming_method", args, host="any"):
    print(item)
```

### Topic-Based Communication (Notifier System)

The notifier system provides topic-based pub/sub and request-by-topic routing, decoupling plugins from having to know each other's names.

**Two communication patterns:**

| Pattern | Method | Description |
|---|---|---|
| Fire-and-forget | `notify()` / `notify_sync()` | One-to-many. All subscribers called, errors logged, no return value. |
| Request-by-topic | `request_topic()` / `request_topic_sync()` | One-to-one. First matching handler called, result returned. |
| Streaming request | `request_topic_stream()` / `request_topic_stream_sync()` | One-to-one streaming. |

**Topics** use `/` as separator. Single-level wildcard `*` matches exactly one segment:

```
"ai/chat"                   — exact topic
"sensor/*/temperature"      — matches "sensor/bathroom/temperature"
"sensor/*"                  — does NOT match "sensor/bathroom/temperature"
```

**Subscribe via config** (in `plugin_config.yml`):

```yaml
endpoints:
  - internal_name: _handle_chat
    access_name: handle_chat
    topic: "ai/chat"              # auto-subscribed on plugin load
    remote: True
    accessible_by_other_plugins: True
```

**Subscribe via code** (runtime, in `on_enable`):

```python
async def on_enable(self):
    self._sub_id = await self.subscribe("events/*", self._on_event)

async def on_disable(self):
    await self.unsubscribe(self._sub_id)
```

**Usage from a plugin:**

```python
# Fire-and-forget — all subscribers receive it
count = await self.notify("sensor/temperature", {"value": 22.5})

# Request with response — first matching handler
result = await self.request_topic("ai/chat", {"message": "hello"})

# Streaming
async for chunk in self.request_topic_stream("ai/stream", args):
    process(chunk)
```

**Cross-node:** Topic operations support the same `host` parameter as `execute()` (`"any"`, `"local"`, `"remote"`, or a specific hostname). Remote nodes are queried when no local handler is found.

### Tags and AI Modes

Endpoint `tags` serve two purposes: general categorization and AI tool discovery. The AI system (`AI_Interaction` plugin) uses `find_endpoints_by_tag()` to discover endpoints at runtime via a four-mode system:

| Mode | Model | Tags Loaded | Use Case |
|------|-------|-------------|----------|
| **Minimum** | Haiku | `AI-minimum` | Quick voice tasks (device control, weather). Short TTS-optimized responses. Separate non-Genesis identity. |
| **Conversation** | Haiku | `AI-minimum` + `AI-conversation` | Default mode. Normal chatting and personal assistant tasks (memory, tasks, appointments, sessions). |
| **Working** | Sonnet | + `AI-working` | High-accuracy workflows, document processing, web search. Activated via `/mode working`. |
| **Debug** | Sonnet/Opus | + `AI-debug` | Full system access including all raw CRUD endpoints. For debugging and administration. |

Each endpoint is explicitly tagged with every mode it belongs to (no inheritance). Modes are configured per-session and reset on inactivity timeout. Users switch modes via the `/mode` slash command in Discord or Telegram.

Key technical details:
- `tool_choice.disable_parallel_tool_use: true` for Haiku modes (prevents tool accuracy issues)
- Per-mode configuration: model, max_tokens, temperature, system_prompt_file, enable_reasoning, prompt_caching, inject flags (person context, memories, session info), summarization
- Conversation history caching for Working/Debug modes
- Daily cost tracking with configurable EUR threshold warning

- **Empty `[]`**: The endpoint is invisible to the AI and only callable by other plugins directly.

This replaces the old three-tier system (AI-1/AI-2/AI-3) with explicit per-endpoint mode tags.

### Endpoint Access Control

Endpoints have two access flags in `plugin_config.yml`:

- `accessible_by_other_plugins`: Controls whether other local plugins can call this endpoint. If `False`, only the plugin itself can invoke it. Must be `True` for the AI to call the endpoint.
- `remote`: Controls whether this endpoint can be called from remote nodes. The plugin-level `remote` flag must also be `True`.

---

## Networking

### Overview

The networking layer uses TLS-encrypted TCP sockets with a binary protocol (pickle-serialized payloads). Every connection starts with a shared-secret authentication handshake. Connections are pooled and reused for efficiency.

### Configuration

Enable networking in `config.yml`:

```yaml
networking:
    enabled: True
    node_ips:
      - "192.168.1.100"
      - "192.168.1.101"
    port: 2510
    discover_nodes: True
    direct_discoverable: True
    auto_discoverable: True
    secret: "my-shared-secret"       # Optional: shared secret for authentication
    cert_file: "./certs/server.pem"  # Optional: TLS certificate file
    key_file: "./certs/server.key"   # Optional: TLS private key file
    pool_size: 5                     # Optional: connection pool size per node
```

### Security

- **TLS Encryption**: All connections use TLS. If `cert_file` and `key_file` are not provided, a self-signed certificate is generated automatically (requires the `cryptography` package). For production, provide proper certificates.
- **Shared-Secret Authentication**: Every connection must authenticate with a shared secret as its first message. The secret can be set via `networking.secret` in config or the `NETWORKING_SECRET` environment variable (recommended for production).

### Node Discovery

- **Manual Nodes**: Specify IP addresses in `node_ips`
- **Auto Discovery**: Set `discover_nodes: True` for automatic network discovery. The system will query known nodes for additional node information.
- **Direct Discoverable**: Allows other nodes to discover this node directly via the INFO protocol
- **Auto Discoverable**: Enables this node's IP to be shared with other nodes during discovery. Note: setting `auto_discoverable: True` forces `direct_discoverable: True`.

### Background Loops

When networking is enabled, two background tasks run:

- **Heartbeat Loop** (default: every 10 seconds): Pings all enabled nodes. Disables nodes that fail to respond within the liveness timeout (default: 30 seconds).
- **Discovery Loop** (default: every 60 seconds): Queries known nodes for new node information and cascades discovery.

### Remote Execution

When networking is enabled, you can execute plugins on remote nodes:

```python
# Execute on any available node (local first, then remote)
result = await plugin_core.execute("RemotePlugin", "method", args, host="any")

# Execute only on remote nodes
result = await plugin_core.execute("RemotePlugin", "method", args, host="remote")

# Execute on specific node by hostname
result = await plugin_core.execute("RemotePlugin", "method", args, host="my-hostname")

# Stream from a remote node
async for item in plugin_core.execute_stream("RemotePlugin", "stream_method", args, host="remote"):
    print(item)
```

---

## Configuration

### config.yml Structure

```yaml
# Plugin Configuration
plugins:
  - name: PluginA
    enabled: true
    path: ./plugins_test/pluginA_v1  # Optional: explicit path
  - name: PluginB
    enabled: true  # Path auto-resolves to plugin_package/PluginB

# General Settings
general:
    hostname: ""              # Unique identifier for this node (empty = system hostname)
    plugin_package: plugins_test  # Base directory for plugins
    console_log_level: "INFO" # DEBUG, INFO, WARNING, ERROR, CRITICAL

# Networking Configuration
networking:
    enabled: False
    node_ips: []              # List of known node IP addresses
    port: 2510
    discover_nodes: True
    direct_discoverable: True
    auto_discoverable: True
    secret: ""                # Shared secret for authentication (or use NETWORKING_SECRET env var)
    cert_file: ""             # Path to TLS certificate file
    key_file: ""              # Path to TLS private key file
    pool_size: 5              # Connection pool size per remote node
```

### Configuration Options

**General**:

- `hostname`: Unique identifier for this node (used in network communication). If empty, defaults to the system hostname.
- `plugin_package`: Default directory where plugins are located if `path` is not specified per plugin
- `console_log_level`: Logging level for console output (DEBUG, INFO, WARNING, ERROR, CRITICAL)

**Plugins**:

- `name`: Plugin name (must match class name)
- `enabled`: Whether to load and enable this plugin on startup
- `path`: Optional explicit path to plugin directory. If omitted, resolves to `{plugin_package}/{name}`
- `arguments`: Optional data passed to the plugin's `on_load()` method

**Networking**:

- `enabled`: Enable/disable networking
- `node_ips`: List of known node IP addresses for manual connection
- `port`: Port number for network communication (default: 2510)
- `discover_nodes`: Enable automatic node discovery
- `direct_discoverable`: Allow direct connection from other nodes
- `auto_discoverable`: Broadcast availability for auto-discovery (forces `direct_discoverable` to `True`)
- `secret`: Shared secret for connection authentication. Use the `NETWORKING_SECRET` environment variable for better security.
- `cert_file`: Path to TLS certificate file (PEM format)
- `key_file`: Path to TLS private key file (PEM format)
- `pool_size`: Maximum number of pooled connections per remote node (default: 5)

### plugin_config.yml Structure

Each plugin directory contains a `plugin_config.yml`:

```yaml
description: str                 # What your plugin does
version: str                     # Semantic version (e.g., "1.0.0")
remote: boolean                  # Allow remote access to this plugin
arguments:                       # Optional: Load-time arguments passed to on_load()
endpoints:
  - internal_name: method_name   # Actual method name in the plugin class
    access_name: method_name     # Name used when calling from other plugins
    tags: []                     # Optional categorization tags
    remote: boolean              # Allow this endpoint to be called remotely
    accessible_by_other_plugins: boolean  # Allow other local plugins to call this
    description: str             # What the endpoint does
    arguments:
      - name: param_name
        type: str                # int, str, dict, list, any, etc.
        description: str         # Include "Optional" or "Omit" to auto-exclude from required
        required: boolean        # Optional: explicit override (true/false) for auto-detection
```

---

## API Reference

### PluginCore Methods

#### `PluginCore(config_path: str)`

Constructor. Loads and validates the configuration file, sets up logging, and initializes internal state. Does **not** load plugins or start networking -- call `start()` or `wait_until_ready()` for that.

#### `start()`

Initialize background tasks, load plugins, and start networking. Equivalent to calling `wait_until_ready()` but also sets up the running loop.

**Returns**: None (coroutine)

#### `close()`

Gracefully shutdown background tasks, wait for in-flight tasks, and stop networking.

**Returns**: None (coroutine)

#### `graceful_shutdown()`

Gracefully shutdown the system by calling `close()`. The main application is responsible for stopping the event loop.

**Returns**: None (coroutine)

#### `execute(plugin, method, args=None, plugin_uuid="", host="any", author="system", author_id="system", timeout=None, author_host=None, request_id=None)`

Execute a plugin method asynchronously.

**Parameters**:

- `plugin` (str): Name of the target plugin
- `method` (str): Access name of the endpoint to execute
- `args` (tuple/dict/None): Arguments to pass to the method. Tuples are unpacked as positional args, dicts as keyword args, single values passed directly.
- `plugin_uuid` (str): Optional UUID to target a specific plugin instance
- `host` (str): `"local"`, `"remote"`, `"any"`, or a specific hostname
- `author` (str): Name of the caller (defaults to hostname)
- `author_id` (str): UUID of the caller (defaults to hostname)
- `timeout` (float/tuple): Optional timeout in seconds
- `author_host` (str): Hostname of the author (defaults to this node's hostname)
- `request_id` (str): Optional request ID (auto-generated if not provided)

**Returns**: Result from the plugin method, or `None` if error handling is active

**Raises**: `RequestException` if execution fails

#### `execute_sync(plugin, method, args=None, ...)`

Synchronous wrapper for `execute()`. Uses `asyncio.run_coroutine_threadsafe()`. Must not be called from within an async context.

**Parameters**: Same as `execute()`

**Returns**: Result from the plugin method

#### `execute_stream(plugin, method, args=None, ...)`

Execute a generator/streaming plugin method asynchronously. Returns an async generator.

**Parameters**: Same as `execute()`

**Yields**: Results from the plugin's generator method

**Raises**: `RequestException` if execution fails

#### `execute_stream_sync(plugin, method, args=None, ...)`

Synchronous version of `execute_stream()`. Returns a sync generator. Must not be called from within an async context.

**Parameters**: Same as `execute()`

**Yields**: Results from the plugin's generator method

#### `notify(topic, args=None, host="any", author="system", author_id="system")`

Fire-and-forget publish to a topic. All matching subscribers are called concurrently; errors are logged but do not propagate.

**Parameters**:

- `topic` (str): Topic string using `/` separator (e.g. `"sensor/bathroom/temperature"`)
- `args` (tuple/dict/None): Arguments forwarded to every subscriber
- `host` (str): `"local"`, `"remote"`, `"any"`, or a specific hostname

**Returns**: Number of subscribers that were called (int)

#### `notify_sync(topic, args=None, host="any", ...)`

Synchronous variant of `notify()`.

#### `request_topic(topic, args=None, host="any", author="system", author_id="system", timeout=None)`

Request-by-topic: find the first matching handler and return its result. Same discovery logic as `execute()` with `host="any"` (local first, then remote).

**Parameters**:

- `topic` (str): Topic string to request
- `args` (tuple/dict/None): Arguments forwarded to the handler
- `host` (str): `"local"`, `"remote"`, `"any"`, or a specific hostname
- `timeout` (float): Optional timeout in seconds

**Returns**: Result from the handler

**Raises**: `RequestException` if no handler found

#### `request_topic_sync(topic, args=None, ...)`

Synchronous variant of `request_topic()`.

#### `request_topic_stream(topic, args=None, host="any", ...)`

Request-by-topic with streaming. Finds the first matching handler and yields its results.

**Yields**: Results from the handler's generator method

**Raises**: `RequestException` if no handler found

#### `request_topic_stream_sync(topic, args=None, ...)`

Synchronous streaming variant of `request_topic()`.

#### `subscribe(topic, plugin_name, plugin_uuid, endpoint_access_name=None, handler=None, config_driven=False)`

Register a topic subscription. Used internally by `Plugin.subscribe()`.

**Returns**: Subscription ID (str)

#### `unsubscribe(subscription_id)`

Remove a topic subscription by ID.

**Returns**: `True` if found and removed

#### `wait_until_ready()`

Ensure initialization tasks are started and await their completion. Safe to call multiple times.

**Returns**: None (coroutine)

#### `find_endpoint(access_name, host="any", plugin_uuid=None, requester_id=None, target_plugin=None)`

Find a plugin endpoint locally or on remote nodes with access control.

**Parameters**:

- `access_name` (str): The access name of the endpoint to find
- `host` (str): Target host (`"local"`, `"remote"`, `"any"`, or specific hostname)
- `plugin_uuid` (str): Optional specific plugin UUID
- `requester_id` (str): UUID of the requesting plugin (for access control)
- `target_plugin` (str): Optional target plugin name filter

**Returns**: Tuple of `(plugin, endpoint_dict, node)` or `(None, None, None)`

#### `get_plugin_info(plugin_name)`

Get structured information about a loaded plugin.

**Returns**: Dict with keys `name`, `version`, `uuid`, `enabled`, `remote`, `description`, `arguments` -- or `None` if not found.

#### `get_plugin_endpoints(plugin_name)`

Get all endpoints for a plugin.

**Returns**: List of endpoint dicts with keys `access_name`, `internal_name`, `remote`, `accessible_by_other_plugins`, `description`, `tags` -- or `None` if not found.

#### `find_endpoints_by_tag(tag)`

Find all endpoints (local and remote) that have a specific tag. Used by the AI system to discover tools at runtime.

**Parameters**:

- `tag` (str): The tag to search for (e.g., `"AI-minimum"`, `"AI-conversation"`, `"AI-working"`, `"AI-debug"`)

**Returns**: List of tuples `(plugin, endpoint_dict, description, arguments)` where `plugin` is either a `Plugin` (local) or `RemotePlugin` (remote) instance. Returns an empty list if no endpoints match.

#### `get_plugins()`

Load plugins from configuration.

**Returns**: None (coroutine)

#### `start_plugins()`

Enable all loaded plugins.

**Returns**: None (coroutine)

#### `purge_plugins()`

Disable and unload all plugins.

**Returns**: None (coroutine)

#### `purge_plugins_except(excluded_names)`

Disable and unload all plugins except those in the provided list.

**Parameters**:

- `excluded_names` (List[str]): Plugin names to keep

**Returns**: None (coroutine)

#### `pop_plugin(plugin_name)`

Remove a specific plugin by name. Disables it first if currently enabled.

**Parameters**:

- `plugin_name` (str): Name of plugin to remove

**Returns**: None (coroutine)

### Plugin Base Class Methods

These methods are available on every plugin instance for calling other plugins:

#### `execute(plugin, method, args=None, plugin_uuid="", host="any", ...)`

Async one-liner to call another plugin's method. Automatically sets `author` and `author_id` to this plugin's name and UUID.

#### `execute_sync(plugin, method, args=None, plugin_uuid="", host="any", ...)`

Sync one-liner to call another plugin's method. Must not be called from async context.

#### `execute_stream(plugin, method, args=None, plugin_uuid="", host="any", ...)`

Async generator one-liner to stream from another plugin's generator method.

#### `execute_stream_sync(plugin, method, args=None, plugin_uuid="", host="any", ...)`

Sync generator one-liner to stream from another plugin's generator method. Must not be called from async context.

---

## Error Handling

### Exception Classes

**Location**: `exceptions.py`

- `ConfigException`: Configuration-related errors (missing sections, invalid values)
- `RequestException`: Plugin request execution errors
- `NetworkRequestException`: Network communication errors
- `NodeException`: Node-related errors (e.g., node not discoverable)
- `PluginTypeMissmatchError`: Raised when a decorator is applied to the wrong function type (e.g., `@async_log_errors` on a sync function)

### Error Decorators

**Location**: `decorators.py`

All decorators include automatic type checking -- using the wrong decorator (e.g., `@async_log_errors` on a sync function) raises `PluginTypeMissmatchError` with a message indicating the correct decorator. The `async_handle_errors` decorator also lets `RequestException` propagate (re-raised instead of caught), so callers can handle plugin/request errors upstream.

**For sync functions:**

- `@log_errors` / `@log_errors()`: Log exceptions without stopping execution, then re-raise
- `@handle_errors(default_return=...)`: Catch exceptions, log them, and return a default value

**For async functions:**

- `@async_log_errors`: Log exceptions without stopping execution, then re-raise
- `@async_handle_errors` / `@async_handle_errors(default_return=...)`: Catch exceptions, log them, and return a default value (can be used with or without parentheses; defaults to `None`)

**For sync generators:**

- `@gen_log_errors` / `@gen_log_errors()`: Log exceptions in sync generators, then re-raise
- `@gen_handle_errors(default_return=...)`: Catch exceptions in sync generators and stop the generator

**For async generators:**

- `@async_gen_log_errors` / `@async_gen_log_errors()`: Log exceptions in async generators, then re-raise
- `@async_gen_handle_errors` / `@async_gen_handle_errors(default_return=...)`: Catch exceptions in async generators and stop the generator (can be used with or without parentheses; defaults to `None`)

### Usage

```python
from decorators import async_handle_errors, async_gen_log_errors

@async_handle_errors(default_return=None)
async def my_method(self):
    # If an exception occurs, returns None instead of raising
    return risky_operation()

@async_gen_log_errors
async def my_stream(self, count):
    for i in range(count):
        yield i
```

---

## CLI Plugin

The CLI plugin provides an interactive command-line interface for managing plugins at runtime using `prompt_toolkit`.

**Location**: `plugins_test/CLI/`

### Available Commands

**Plugin Management:**

| Command | Description |
|---|---|
| `load <name>` | Load a plugin from config |
| `enable <name>` | Enable a loaded plugin |
| `disable <name>` | Disable a plugin |
| `reload <name>` | Reload a plugin (disable, remove, re-load, re-enable) |
| `pop <name>` | Remove a plugin from memory |
| `purge` | Remove all plugins (including CLI) |
| `purge_safe` | Remove all plugins except CLI |

**Inspection:**

| Command | Description |
|---|---|
| `list` | List all loaded plugins with status |
| `info <name>` | Show detailed plugin information |
| `endpoints <name>` | Show all endpoints for a plugin |
| `status` | Show system status (hostname, plugin count, networking) |

**Execution:**

| Command | Description |
|---|---|
| `call <plugin> <method> [json_args]` | Call a plugin method (runs in background) |
| `stream <plugin> <method> [json_args]` | Stream from a plugin method (runs in background) |

**System:**

| Command | Description |
|---|---|
| `shutdown` | Gracefully shutdown the system |
| `clear` | Clear the screen |
| `config show` | Show current config file contents |
| `config edit` | Show instructions for editing config |
| `help` | Show help message |

### CLI Examples

```
AIO> list
AIO> info PluginA
AIO> endpoints PluginB
AIO> call PluginB calculate_square 6
AIO> call PluginA perform_operation {"argument": 5}
AIO> stream PluginA perform_operation_stream {"argument": 5}
AIO> reload PluginB
AIO> shutdown
```

---

## Examples

### Example 1: Basic Plugin Execution

```python
import asyncio
from PluginCore import PluginCore

async def main():
    plugin_core = PluginCore("config.yml")
    await plugin_core.wait_until_ready()

    result = await plugin_core.execute(
        "PluginB",
        "calculate_square",
        6,
        host="local"
    )
    print(f"Square of 6: {result}")

    await plugin_core.close()

asyncio.run(main())
```

### Example 2: Streaming Results

```python
async def main():
    plugin_core = PluginCore("config.yml")
    await plugin_core.wait_until_ready()

    async for item in plugin_core.execute_stream(
        "PluginA",
        "perform_operation_stream",
        9,
        host="any"
    ):
        print(f"Received: {item}")

    await plugin_core.close()
```

### Example 3: Plugin-to-Plugin Communication

```python
from utils import Plugin
from decorators import async_log_errors, log_errors

class PluginA(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        self.plugin_name = "PluginA"

    @async_log_errors
    async def on_enable(self):
        result = await self.execute(
            "PluginB",
            "calculate_square",
            5,
            host="local"
        )
        self._logger.info(f"Got result: {result}")

    @async_log_errors
    async def on_disable(self):
        pass

    async def process_data(self, data):
        return data * 2
```

### Example 4: Sync Plugin Calling Async Plugin

```python
from utils import Plugin
from decorators import log_errors

class SyncPlugin(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @log_errors
    def on_enable(self):
        pass

    @log_errors
    def on_disable(self):
        pass

    @log_errors
    def perform_operation(self, argument):
        # Call an async plugin from sync context
        result = self.execute_sync("PluginB", "calculate_square", argument)
        return result
```

### Example 5: Error Handling with Decorators

```python
from decorators import async_handle_errors, async_gen_log_errors

class SafePlugin(Plugin):
    @async_handle_errors(default_return=0)
    async def safe_calculation(self, x):
        return x / 0  # Returns 0 instead of raising

    @async_gen_log_errors
    async def safe_stream(self, count):
        for i in range(count):
            yield i  # Exceptions are logged and re-raised
```

### Example 6: Remote Execution

```python
async def main():
    plugin_core = PluginCore("config.yml")
    await plugin_core.wait_until_ready()

    # Execute on any available node (local first, then remote)
    result = await plugin_core.execute(
        "RemotePlugin",
        "remote_method",
        args,
        host="any"
    )

    # Stream from a remote node
    async for item in plugin_core.execute_stream(
        "RemotePlugin",
        "stream_method",
        args,
        host="remote"
    ):
        print(item)

    await plugin_core.close()
```

---

## Interop Test Plugins

Two plugins validate sync/async calls and streaming (generators) across all calling combinations:

- `InteropTarget`: Exposes test endpoints
  - `it_sync_add(a, b=1)` - Sync add
  - `it_async_add(a, b=1)` - Async add
  - `it_sync_gen(n=3, prefix="g", delay_ms=10)` - Sync generator
  - `it_async_gen(n=3, prefix="ag", delay_ms=10)` - Async generator
- `InteropCaller`: Runs the test matrix via `run_suite(host)` (async) and `run_suite_sync(host)` (sync), logging PASS/FAIL for each case.

Usage:

```python
# Async context
result = await self.execute("InteropCaller", "interop_run", {"host": "any"})

# Sync context
result = self.execute_sync("InteropCaller", "interop_run_sync", {"host": "any"})

# Cross-device
result = await self.execute("InteropCaller", "interop_run", {"host": "remote"})
```

Host options: `"any"`, `"local"`, `"remote"`, or a specific hostname.

---

## File Structure

```
AIO_Assistant_Core/
├── PluginCore.py              # Main plugin management class
├── networking.py              # Network communication manager (TLS/TCP sockets)
├── networking_classes.py      # Node and RemotePlugin classes
├── utils.py                   # Plugin base class, Request, GeneratorRequest,
│                              #   ConfigUtil, LogUtil, EndOfQueue
├── decorators.py              # Error handling decorators (sync, async, generators)
├── exceptions.py              # Custom exception classes
├── main_application.py        # Example application entry point
├── config.yml                 # Main configuration file
├── requirements.txt           # Python package dependencies
├── notes.txt                  # Development notes and TODOs
├── README.md                  # This documentation file
├── copypasta/                 # Plugin templates and examples
│   ├── README.md              # Template usage guide
│   ├── NEW_PUGIN_INFO.md      # Guide for AI-integrated endpoint setup
│   ├── AveragePlugin/         # Example plugin template
│   │   ├── plugin.py
│   │   └── plugin_config.yml
│   └── config_structures.txt  # Config file structure reference
├── plugins_test/              # Test and example plugins
│   ├── pluginA_v1/            # Async plugin calling another plugin
│   ├── PluginB/               # Async calculation plugin
│   ├── PluginC/               # Sync plugin calling an async plugin
│   ├── CLI/                   # Interactive command-line interface plugin
│   ├── InteropTarget/         # Interop test target (sync/async/generators)
│   ├── InteropCaller/         # Interop test runner
│   └── NetTest/               # Network testing plugin (echo, big objects, streaming)
├── _private/                  # Production plugins (not part of open-source core)
│   ├── AI_Plugin/             # AI assistant (Claude API via Anthropic SDK, 4-mode system)
│   ├── DataCollection/        # Centralized life-data storage (PostgreSQL + MinIO)
│   ├── VoicePipeline/         # Voice-to-text with speaker diarization and TTS
│   ├── DiscordBot/            # Discord bot interface with approval system and AI modes
│   ├── TelegramBot/           # Telegram bot for document scanning and AI chat
│   ├── DATABASE/              # PostgreSQL and MinIO database plugins
│   └── TTS/                   # Piper text-to-speech plugin
```

---

## Logging

The system uses a custom logging utility (`LogUtil` in `utils.py`) that provides:

- **Non-blocking I/O**: Uses `QueueHandler` and `QueueListener` for thread-safe, non-blocking log output
- **Colored console output**: Uses `colorama` for color-coded log levels and components
- **File logging**: Timestamped log files in the `logs/` directory with plain-text formatting
- **Hierarchical loggers**: Each plugin and component gets its own child logger (e.g., `root.PluginA`, `root.networking`)
- **Dynamic log level**: `LogUtil.change_level()` adjusts console output level at runtime without affecting file logging
- **Automatic cleanup**: The `QueueListener` is stopped via `atexit` hook

Log files are automatically created in the `logs/` directory with format: `AIO_AI_YYYY-MM-DD_HH-MM-SS.log`

---

## Private Plugins

The `_private/` directory contains production plugins that build on top of the core framework. These are not part of the open-source core but demonstrate the framework's capabilities. Each plugin has its own documentation in its directory.

### AI_Interaction (`_private/AI_Plugin/`)

An AI assistant powered by Claude (Haiku/Sonnet/Opus) via the Anthropic SDK. Key features:

- **Anthropic API backend**: Uses `anthropic.Anthropic` client with Claude's native structured tool calling (`tool_use`/`tool_result` content blocks). Requires `ANTHROPIC_API_KEY` environment variable or `api_key` in plugin config. Message normalization (`_normalize_messages`) ensures proper user/assistant alternation required by the API.
- **Inference lock and cancellation**: An `_inference_lock` (threading.Lock) serializes all `ai_chat` / `ai_chat_stream` calls so only one inference runs at a time across all consumers (Discord, voice, API). A `_cancel_event` is checked every token and tool iteration. The `cancel_generation` endpoint allows any consumer to stop the current generation immediately.
- **Four-mode AI system**: Mode-aware routing selects model, tools, and context per task complexity. Minimum (Haiku, device control), Conversation (Haiku, personal assistant), Working (Sonnet, documents/web), Debug (Sonnet/Opus, full access). Each mode defines its own model, max_tokens, temperature, system prompt, tool tags, and context injection flags. Modes are per-session and reset on inactivity timeout. See [Tags and AI Modes](#tags-and-ai-modes) for details.
- **Prompt caching**: `_inject_context` returns a `(system_blocks, messages)` tuple. The system prompt is fully static and cacheable (including owner/family person records fetched once on load). Dynamic context (time, channel, memories, semantic results) is prepended as a `[System Context]` message in the messages array. Cache structure: system (cached) -> tools (cached) -> messages (dynamic). Conversation history caching enabled for Working/Debug modes.
- **Smart optional parameters**: Tool definitions auto-detect optional parameters from argument descriptions containing "optional" or "omit". These parameters are not marked as `required` in the JSON schema, so the LLM does not have to fill them with placeholder values.
- **Endpoint approval system**: Before executing a tool call, the AI checks the endpoint's approval policy (prompt/auto_approve/deny) from the database. Users approve via Discord buttons.
- **Tool call visibility**: Conversation history includes `[Tools used: ...]` summaries from metadata, so the AI can reference previous tool results across turns.
- **Context injection**: Dynamic context is injected per-message (configurable per mode) from: (1) general knowledge memory, (2) active short-term memories, (3) semantically relevant notes, and (4) pending/in-progress tasks semantically related to the user's message.
- **Daily cost tracking**: Per-call API cost tracking with configurable EUR threshold warning via Discord DM.
- **Error handling**: Explicit handling for API errors (authentication, rate limits, etc.) that yields/returns visible error messages instead of silent failures.

### DataCollection (`_private/DataCollection/`)

Centralized data storage for all "life data" (people, organizations, documents, appointments, tasks, notes, logs, etc.) backed by PostgreSQL and MinIO. Supports vector-based semantic search (fastembed) over notes, short-term memories, tasks, and documents. See `_private/DataCollection/DOCS.md` for full technical documentation.

### VoicePipeline (`_private/VoicePipeline/`)

Voice-to-text pipeline with speaker diarization (pyannote or speechbrain), Whisper-based STT, and Piper TTS. Uses the current `token=` parameter (not the deprecated `use_auth_token=`) for `Model.from_pretrained` calls. See `_private/VoicePipeline/SETUP_GUIDE.md`.

### DiscordBot (`_private/DiscordBot/`)

Discord bot interface that bridges users to the AI assistant with approval buttons, conversation management, proactive reminders, and AI mode control. Key features:

- **`!ais <message>` (stop+replace)**: Cancels the current AI generation and processes the new message instead.
- **`!ai` while busy**: The message is queued (hourglass reaction), then processed after the current response finishes. Maximum queue depth: 5 per channel.
- **`/stop` slash command**: Cancels the current AI generation without sending a new message.
- **`/mode <minimum|conversation|working|debug>`**: Switch AI mode per channel. Shows confirmation embed with mode name, model, and tool count.
- **`/debuginfo on|off`**: Toggle debug overlay on AI responses (shows mode, model, token usage).
- **Per-channel state tracking**: Each channel tracks busy status, a cancel event, a message queue, and active AI mode independently.
- **Auto-registration of DM channels**: When a user DMs the bot, the DM channel is automatically registered in the DataCollection database with `is_private: True` and `belongs_to` set to the user's person ID.

---

## Future Plans

The following features are planned but not yet implemented:

- **Hot-swapping plugins**: Dynamically reload plugins without restarting (partial: `_reload_plugin()` exists but is not fully polished)
- **Pipeline/Datastream**: Continuous data processing pipelines usable for both streaming and batch processing
- **Plugin caching**: Cache plugin locations on remote devices for faster lookup

---

## Contributing

When creating plugins or contributing to the core:

1. Follow the plugin structure outlined in the [Plugin System](#plugin-system) section
2. Use appropriate error handling decorators matching your function type (sync, async, sync generator, async generator)
3. Document your plugin methods clearly in `plugin_config.yml`
4. Test both local and remote execution (if applicable)
5. Use the InteropCaller/InteropTarget pattern to validate cross-context calls
6. Update this documentation if adding new features

---

## License

See `LICENSE` file for details.

---

## Support

For issues, questions, or contributions, please refer to the project repository.
