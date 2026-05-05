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
10. [CLI Dashboard Plugin](#cli-dashboard-plugin)
11. [Examples](#examples)
12. [Interop Test Plugins](#interop-test-plugins)
13. [File Structure](#file-structure)
14. [Logging](#logging)
15. [Future Plans](#future-plans)
16. [Contributing](#contributing)

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
- **Topic-Based Event System**: Pub/sub and request-by-event routing for decoupled plugin communication with wildcard support
- **Streaming Support**: Full support for both sync and async generator-based data streams
- **Built-in CLI Dashboard**: Textual-based TUI with system stats, plugin management, config editing, and live logs
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
# For the CLI dashboard plugin:
pip install textual
# Optional (enables CPU/memory sparkline graphs):
pip install psutil

# Plugin-specific dependencies (only install what you need for the plugins you plan to use)
# are documented in each plugin's own README or comments inside its plugin.py
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
    result = await plugin_core.execute("PluginB", "calculate_square", 6, hosts="local")
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

    result = await plugin_core.execute("PluginB", "calculate_square", 6, hosts="local")
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
- `list_config_files()` - Return `{label: absolute_path}` dict for main config and all plugin configs
- `read_config_file()` - Read content of a known config file (path-allowlisted)
- `save_config_file()` - Validate YAML, back up, and write a config file (thread-safe)
- `is_main_config()` - Check whether a path points to the main config.yml

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
- Tag-based endpoint discovery on remote nodes via `node_get_tagged_endpoints()`
- Remote plugin execution and streaming
- Cross-node topic publish/request via `publish_event_remote()`, `request_event_remote()`, and `request_event_stream_remote()`
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
| Find Tagged Endpoints | `MSG_FIND_TAGGED_ENDPOINTS` (6) | Query endpoints by tag on a remote node |
| Result | `MSG_RESULT` (10) | Response with result data |
| Stream Chunk | `MSG_STREAM_CHUNK` (11) | A chunk of streaming data |
| Error | `MSG_ERROR` (12) | Error response |
| End Stream | `MSG_END_STREAM` (13) | Marks end of a stream |
| Stream Item End | `MSG_STREAM_ITEM_END` (14) | Marks end of an individual streamed item |
| Publish Event | `MSG_PUBLISH_EVENT` (15) | Fire-and-forget event publish to remote nodes |
| Request Event | `MSG_REQUEST_EVENT` (16) | Request-by-event call to a remote node |
| Request Event Stream | `MSG_REQUEST_EVENT_STREAM` (17) | Streaming request-by-event to a remote node |
| Sub Advertise | `MSG_SUB_ADVERTISE` (18) | Initial subscription-snapshot exchange between peers |
| Sub Delta | `MSG_SUB_DELTA` (19) | Incremental subscribe/unsubscribe delta to peers |
| Auth | `MSG_AUTH` (20) | Authentication message (shared secret) |

(MSG types 7-9 are reserved — they previously held the legacy notify /
request_topic / request_topic_stream wire IDs which were retired in PR3
Stage D.)

A `REMOTE_NO_RESULT` sentinel distinguishes "handler returned `None`" from "no remote handler responded" for `request_event` calls across nodes.

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
- `endpoints` - Dict keyed by access_name; each value is the endpoint configuration dict
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
arguments:                          # optional load-time arguments dict; omit or null for none
endpoints:
  # Dict keyed by access_name. internal_name defaults to the key when omitted.
  my_method:
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
result = await self.execute("PluginName", "method_access_name", args, hosts="local")

# Sync execution (call from sync context, must not be called from async)
result = self.execute_sync("PluginName", "method_access_name", args, hosts="local")

# With keyword arguments
result = await self.execute("PluginName", "method_name", {"key": "value"}, hosts="any")

# With positional arguments as tuple
result = await self.execute("PluginName", "method_name", (arg1, arg2), hosts="any")

# Host options: "local", "remote", "any", or specific hostname
```

### Streaming/Generator Support

Plugins can expose both sync and async generators. Callers can consume them from either context:

```python
# Async context -> async generator endpoint
async for item in self.execute_stream("PluginA", "streaming_method", args, hosts="any"):
    print(item)

# Sync context -> any generator endpoint
for item in self.execute_stream_sync("PluginA", "streaming_method", args, hosts="any"):
    print(item)
```

### Topic-Based Communication (Event System)

The event system provides topic-based pub/sub and request-by-topic routing, decoupling plugins from having to know each other's names. Publishers declare named events in `plugin_config.yml` and call them by `event_id`; subscribers declare topic patterns plus a target endpoint that receives an `Event` object.

**Two communication patterns:**

| Pattern | Method | Description |
|---|---|---|
| Fire-and-forget | `publish_event()` / `publish_event_sync()` | One-to-many. All matching subscribers called concurrently, errors logged. Returns the number of subscribers that received the event (int). |
| Request-by-event | `request_event()` / `request_event_sync()` | One-to-one. First matching handler called, result returned. |
| Streaming request | `request_event_stream()` / `request_event_stream_sync()` | One-to-one streaming. |

**Topics** use `/` as separator. Single-level wildcard `*` matches exactly one segment:

```
"ai/chat"                   — exact topic
"sensor/*/temperature"      — matches "sensor/bathroom/temperature"
"sensor/*"                  — does NOT match "sensor/bathroom/temperature"
```

**Declare events you publish** (publisher side, in `plugin_config.yml`):

```yaml
events:
  ai_chat:
    topic: "ai/chat"
    hosts: "any"
```

**Declare subscriptions** (subscriber side, in `plugin_config.yml`):

```yaml
subscriptions:
  handle_chat_sub:
    topic: "ai/chat"
    target_access_name: handle_chat       # endpoint that receives the Event
    hosts: "any"
endpoints:
  handle_chat:
    internal_name: handle_chat
    remote: True
    accessible_by_other_plugins: True
    arguments:
      - name: event
```

**Subscribe via code** (runtime, in `on_enable`):

```python
async def on_enable(self):
    self._sub_id = await self.subscribe(
        "events/*", target_access_name="my_event_handler"
    )

async def on_disable(self):
    await self.unsubscribe(self._sub_id)
```

The handler endpoint receives an `Event` object with `event.topic`, `event.payload`, `event.author`, and `event.author_host`.

**Usage from a plugin:**

```python
# Fire-and-forget — all subscribers receive it
count = await self.publish_event("ai_chat", payload={"message": "hello"})

# Request with response — first matching handler
result = await self.request_event("ai_chat", payload={"message": "hello"})

# Streaming
async for chunk in self.request_event_stream("ai_stream", payload=args):
    process(chunk)
```

**Cross-node:** Event operations support the same `hosts` parameter as `execute()` (`"any"`, `"local"`, `"remote"`, or a specific hostname). Remote nodes are reached via the advertised-subscription protocol; remote subscribers receive the same `Event` object.

### Tags and Endpoint Discovery

Endpoint `tags` serve two purposes:

1. **General categorization** — group endpoints by topic, capability, or trust level so callers can enumerate them.
2. **Runtime discovery** — `PluginCore.find_endpoints_by_tag(tag)` returns every local *and* remote endpoint that carries a given tag, as a list of `(plugin, endpoint_dict, description, arguments)` tuples.

Tags are set per-endpoint in `plugin_config.yml`:

```yaml
endpoints:
  my_method:
    tags: ["sensors", "weather"]    # arbitrary strings — define your own taxonomy
    ...
```

A common pattern is to use tags as a capability gate for AI / orchestrator plugins: the orchestrator queries `find_endpoints_by_tag("AI-conversation")` (or any tag of its choosing) to discover which tools it is allowed to expose to the model in a given mode. Endpoints with empty `tags: []` are invisible to tag-based lookups and only callable by plugins that already know the access name.

Tags are matched as exact strings — there is no hierarchy or wildcard. An endpoint that should appear under multiple tags simply lists all of them.

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
result = await plugin_core.execute("RemotePlugin", "method", args, hosts="any")

# Execute only on remote nodes
result = await plugin_core.execute("RemotePlugin", "method", args, hosts="remote")

# Execute on specific node by hostname
result = await plugin_core.execute("RemotePlugin", "method", args, hosts="my-hostname")

# Stream from a remote node
async for item in plugin_core.execute_stream("RemotePlugin", "stream_method", args, hosts="remote"):
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
    file_log_level: "DEBUG"   # Log level for file output (independent of console)
    asyncio_debug: false      # Enable Python's asyncio debug mode (slower, more verbose)

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
- `file_log_level`: Logging level for the rotating log files in `logs/`, independent from `console_log_level`. You can run a quiet console with verbose file logs (or the reverse).
- `asyncio_debug`: Enable Python's asyncio debug mode. Useful for diagnosing slow callbacks and unawaited coroutines, but adds overhead — keep `false` in production.

**Plugins**:

- `name`: Plugin name (must match class name)
- `enabled`: Whether to load and enable this plugin on startup
- `path`: Optional explicit path to plugin directory. If omitted, resolves to `{plugin_package}/{name}`
- `overrides`: Optional dict overriding values from the plugin's own `plugin_config.yml` — covers `arguments`, `endpoints`, and plugin-level fields (`description`, `remote`, `version`). See [Argument Overrides](#argument-overrides) for the merge rules and the `__replace__` marker. (The pre-PR2 top-level `arguments:` field on plugin entries is now a legacy form: it logs a warning and is ignored — wrap it in `overrides: { arguments: ... }` instead.)

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

### Argument Overrides

A plugin's defaults live in its own `plugin_config.yml`. To change them per-deployment without editing the plugin's file, add an `overrides:` block to the plugin's entry in the main `config.yml`. The block covers three sections — `arguments`, `endpoints` — plus the plugin-level fields `description`, `remote`, and `version`:

```yaml
plugins:
  - name: AI_Interaction
    enabled: true
    path: ./_private/AI/AI_Interaction
    overrides:
      arguments:                          # deep-merges into plugin_config.yml's arguments
        modes:
          conversation:
            temperature: 0.3              # only this leaf is overridden
        daily_cost_warning_eur: 5.0
      endpoints:                          # per-key deep-merge into endpoints dict
        chat:
          remote: True                    # change just this field on the `chat` endpoint
        legacy_method:
          __replace__: true               # wholesale-replace this entry
          internal_name: legacy_method
          remote: False
          accessible_by_other_plugins: True
      description: "AI for production node"   # plugin-level field replace
      remote: True
```

**Section-aware unknown-key behavior (Q2):**

- `endpoints` is **strict**: an unknown endpoint key in the override (one that doesn't exist in `plugin_config.yml`'s endpoints dict) is an ERROR and the plugin fails to load. Catches typos in deployment configs that would otherwise silently miss the override target.
- `arguments` is **lenient**: unknown subkeys are added to the merged dict. Plugin authors are free to read or ignore them.
- Unknown TOP-LEVEL keys in the `overrides:` block (anything not in `{arguments, endpoints, events, subscriptions, description, remote, version, prefix, verbose_notifier}`) log a WARN and are ignored.

**Merge rules (apply to `arguments` and `endpoints` deep-merges):**

- **Dicts deep-merge.** Same key + both dict ⇒ recurse into both. Sibling keys in the base are preserved untouched at every depth. The example above replaces only `modes.conversation.temperature` and adds `daily_cost_warning_eur` — every other mode and every other field of `conversation` stays as declared in `plugin_config.yml`.
- **Lists fully replace.** Override list wins wholesale. For list-of-dicts cases (e.g. `notifiers:`, `servers:` in MCPClient), you must restate every element you want to keep.
- **Scalars replace.** Override value wins; the merged dict reaches the plugin via `on_load(**arguments)` and `self.arguments`.
- **Type mismatch logs a warning.** A `bool`-base overridden with a `str`, or a typed value overridden with `null`, applies anyway and logs `arg '<path>' type mismatch (X -> Y); override applied`. Base-was-`null` is not a mismatch.
- **No-op overrides are silent.** Same-type, same-value overrides do not log and do not increment counters.

**`__replace__: true` marker — wholesale subtree replacement.**

To bypass deep-merge at a specific node and replace its value entirely, include the special key `__replace__: true` inside the override dict at that level. The marker itself is stripped from the result. Works in both the `arguments` and `endpoints` sections.

```yaml
overrides:
  arguments:
    modes:
      __replace__: true              # discard every existing mode
      conversation: {model: x}       # this is the new modes dict
```

Two common uses:
- **Clear a subtree:** `key: {__replace__: true}` ⇒ `key: {}`.
- **Replace without inheriting siblings:** `key: {__replace__: true, a: 1}` ⇒ `key: {a: 1}` (any other keys the base had under `key` are gone).

For endpoints, `__replace__: true` discards the base entry entirely; the override must then satisfy the required fields itself (`remote`, `accessible_by_other_plugins`).

**Plugin-level field overrides:**

`description`, `remote`, and `version` at the top of `overrides:` replace the corresponding values from `plugin_config.yml` outright (no merge — they are scalars). Useful when running two instances of the same plugin under different names with different remote-flag policies.

**Constraints and corner cases:**

- The top-level `arguments:` in `plugin_config.yml` must be a dict, `null`, or omitted. Lists and scalars at the root are rejected (the plugin fails to load).
- The `overrides:` block in `config.yml` must be a dict (or omitted). A wrong type logs a warning and the override is ignored — other plugins keep loading.
- A legacy top-level `arguments:` field on the plugin entry in `config.yml` (the pre-PR2 form) logs a warning and is ignored. Wrap it inside `overrides: { arguments: ... }` to make it take effect.
- `arguments: null` (or missing) + no override ⇒ `self.arguments is None` (unchanged from before this feature).
- `arguments: {}` (explicit empty dict) + no override ⇒ `self.arguments == {}` (unchanged).
- `arguments: null` + a real override dict ⇒ `self.arguments == <override>`.
- Plugins must not mutate `self.arguments` in place — nested dicts may share references with the original parsed config until reload.

**Logging:**

- Each per-key change is logged at DEBUG level: `arg added '<path>'`, `arg replaced '<path>'`, or `arg subtree replaced '<path>'`. Only key paths are logged — never values, since arguments may contain secrets.
- One INFO summary per plugin: `applied N override(s) (X added, Y replaced, Z type-mismatched)`. Plugins with no override changes emit no summary line.

**Multi-instance plugins:**

Each entry in the `plugins:` list is independent. Two `DiscordBot` entries with different `name` values can carry different override dicts — handy when running two bot instances on the same node with different configurations.

**Networking:**

Overrides apply locally on the node where they are configured. There is no cross-node merging — each node's `config.yml` defines its own overrides for its own plugin loadout.

**Hot-reload:**

Overrides are baked in at plugin load time. To pick up edits to `config.yml` without restarting, use the CLI Dashboard in two steps:

1. Click **Reload Main Config** (in the Config tab) — re-reads `config.yml` into `self.yaml_config`.
2. Click the per-plugin **Reload** button — re-instantiates the plugin with the new merged arguments.

Skipping step 1 leaves the system using whatever override was active at startup; the per-plugin reload alone will not pick up edits to `config.yml`.

### plugin_config.yml Structure

Each plugin directory contains a `plugin_config.yml`:

```yaml
description: str                 # What your plugin does
version: str                     # Semantic version (e.g., "1.0.0")
remote: boolean                  # Allow remote access to this plugin
arguments:                       # Optional: Load-time arguments passed to on_load()
endpoints:                       # Dict keyed by access_name (the name other plugins call)
  method_access_name:            # access_name = key; must be a valid Python identifier
    internal_name: method_name   # Optional: actual method name on the class. Defaults to key.
    tags: []                     # Optional categorization tags
    remote: boolean              # Allow this endpoint to be called remotely
    accessible_by_other_plugins: boolean  # Allow other local plugins to call this
    topic: "some/topic"          # Optional: auto-subscribe this endpoint to a topic on plugin load
    description: str             # What the endpoint does
    arguments:
      - name: param_name
        type: str                # int, str, dict, list, any, etc.
        description: str         # Free-form description of the argument
        required: boolean        # Optional: marks the argument as required (read by the CLI dashboard
                                 # and available to any orchestrator that builds AI tool schemas
                                 # from the endpoint metadata)
```

Per-plugin overrides go in `config.yml` under an `overrides:` block on the plugin entry:

```yaml
plugins:
  - name: MyPlugin
    enabled: true
    overrides:
      arguments:                 # Deep-merges into plugin_config.yml's `arguments`
        api_key: "..."
      endpoints:                 # Per-key deep-merge into the endpoints dict
        my_method:
          remote: True           # Override just this field
        other_method:
          __replace__: true      # Wholesale-replace the entry (rest must satisfy required fields)
          internal_name: other_method
          remote: True
          accessible_by_other_plugins: True
      description: "Override per-instance description"  # Plugin-level field replace
      remote: True
```

Additional fields (e.g. a `default:` value, or other schema hints) are ignored by core. They are passed through verbatim in `endpoint["arguments"]`, so an orchestrator plugin is free to define and consume its own extra keys.

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

#### `execute(plugin, method, args=None, plugin_uuid="", hosts="any", author="system", author_id="system", timeout=None, author_hosts=None, request_id=None)`

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

#### `publish_event(event_id, payload=None, hosts=None, ...)`

Fire-and-forget publish for a declared event. All matching subscribers are dispatched concurrently; errors are logged but do not propagate.

**Parameters**:

- `event_id` (str): Event id declared in `events:` of `plugin_config.yml`
- `payload` (any): Payload forwarded to every subscriber as `event.payload`
- `hosts` (str/list): `"local"`, `"remote"`, `"any"`, or a specific hostname / list of hostnames (defaults to the event's declared `hosts`)

**Returns**: Number of subscribers that received the event (int — local + remote)

#### `publish_event_sync(event_id, payload=None, hosts=None, ...)`

Synchronous variant of `publish_event()`.

#### `request_event(event_id, payload=None, hosts=None, timeout=None, ...)`

Request-by-event: find the first matching subscription and return the target endpoint's result. Same discovery logic as `execute()` with `hosts="any"` (local first, then remote).

**Parameters**:

- `event_id` (str): Event id declared in `events:` of `plugin_config.yml`
- `payload` (any): Payload forwarded to the handler as `event.payload`
- `hosts` (str/list): `"local"`, `"remote"`, `"any"`, or a specific hostname
- `timeout` (float): Optional timeout in seconds

**Returns**: Result from the target endpoint

**Raises**: `RequestException` if no subscription matches the event

#### `request_event_sync(event_id, payload=None, ...)`

Synchronous variant of `request_event()`.

#### `request_event_stream(event_id, payload=None, hosts=None, ...)`

Request-by-event with streaming. Finds the first matching subscription and yields the target endpoint's results.

**Yields**: Items yielded by the handler's generator method (the first item is wrapped as an `Event`)

**Raises**: `RequestException` if no subscription matches the event

#### `request_event_stream_sync(event_id, payload=None, ...)`

Synchronous streaming variant of `request_event()`.

#### `subscribe(topic, plugin_name, plugin_uuid, target_access_name=None, ...)`

Register a topic subscription targeted at a declared endpoint. Used internally by `Plugin.subscribe()`.

**Returns**: Subscription ID (str)

#### `unsubscribe(subscription_id)`

Remove a topic subscription by ID.

**Returns**: `True` if found and removed

#### `wait_until_ready()`

Ensure initialization tasks are started and await their completion. Safe to call multiple times.

**Returns**: None (coroutine)

#### `list_config_files() -> Dict[str, str]`

Return a dict of `{label: absolute_path}` for the main `config.yml` and every plugin's `plugin_config.yml`. Labels follow the format `"config.yml (main)"` and `"PluginName/plugin_config.yml"`.

**Returns**: Dict mapping human-readable labels to absolute file paths.

#### `read_config_file(path: str) -> str`

Read and return the raw content of a known config file. The path must appear in `list_config_files()` (allowlist validation).

**Parameters**:

- `path` (str): Absolute path to the config file.

**Returns**: File content as a string.

**Raises**: `ValueError` if path is not in the allowlist; `FileNotFoundError` if the file does not exist.

#### `save_config_file(path: str, content: str, backup: bool = True) -> None`

Validate YAML syntax, optionally create a `.yml.bak` backup, and write new content to a config file. Writing is serialized with a `threading.Lock` for thread safety. The path must appear in `list_config_files()`.

**Important**: This method does **not** auto-reload the main config. If `is_main_config(path)` returns `True`, the caller must call `load_config_yaml()` explicitly for settings to take effect.

**Parameters**:

- `path` (str): Absolute path to the config file.
- `content` (str): New YAML content to write.
- `backup` (bool): Whether to create a `.yml.bak` before overwriting (default `True`).

**Raises**: `ValueError` if path is not in the allowlist or content parses to empty; `yaml.YAMLError` if content is invalid YAML.

#### `is_main_config(path: str) -> bool`

Check whether a path points to the main `config.yml`.

**Parameters**:

- `path` (str): Path to check.

**Returns**: `True` if the path resolves to the main config file.

#### `find_endpoint(access_name, hosts="any", plugin_uuid=None, requester_id=None, target_plugin=None)`

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

#### `execute(plugin, method, args=None, plugin_uuid="", hosts="any", ...)`

Async one-liner to call another plugin's method. Automatically sets `author` and `author_id` to this plugin's name and UUID.

#### `execute_sync(plugin, method, args=None, plugin_uuid="", hosts="any", ...)`

Sync one-liner to call another plugin's method. Must not be called from async context.

#### `execute_stream(plugin, method, args=None, plugin_uuid="", hosts="any", ...)`

Async generator one-liner to stream from another plugin's generator method.

#### `execute_stream_sync(plugin, method, args=None, plugin_uuid="", hosts="any", ...)`

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

All decorators include automatic type checking -- using the wrong decorator (e.g., `@async_log_errors` on a sync function) raises `PluginTypeMissmatchError` with a message indicating the correct decorator.

**For sync functions:**

- `@log_errors` / `@log_errors()`: Log exceptions without stopping execution, then re-raise
- `@handle_errors(default_return=...)`: Catch exceptions, log them, and return a default value

**For async functions:**

- `@async_log_errors`: Log exceptions without stopping execution, then re-raise. Bare-decorator form only — do not call with parentheses.
- `@async_handle_errors` / `@async_handle_errors(default_return=...)`: Catch exceptions, log them, and return a default value (can be used with or without parentheses; defaults to `None`). `RequestException` is intentionally re-raised so callers can handle plugin/request errors upstream.

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

## CLI Dashboard Plugin

The CLI plugin provides a Textual-based terminal dashboard (TUI) for managing and monitoring the PluginCore at runtime.

**Location**: `plugins_test/CLI/`
**Version**: 2.4.0
**Dependencies**: `textual` (required), `psutil` (optional — enables CPU/memory sparkline graphs)

### Dashboard Tabs

| Tab | Description |
|---|---|
| **Home** | System stats (CPU, memory, uptime) with live sparkline graphs. Active requests and network node tables with empty-state labels |
| **Plugins** | DataTable of all loaded plugins with enable/disable/reload/remove buttons |
| **Config** | Edit `config.yml` and per-plugin `plugin_config.yml` files via PluginCore's config editing API (`list_config_files`, `read_config_file`, `save_config_file`). YAML validation, backup-on-save, dirty tracking warns on unsaved changes when switching files or tabs |
| **Logs** | Live log viewer with level filtering, text search, and auto-scroll. Incremental DataTable updates (append/remove) preserve scroll position. Record count indicator shows filtered/total with "(filtered)" suffix. Per-level color styling (ERROR red, WARNING amber, INFO gray, DEBUG dim). Backed by an in-memory record store (5000 records) so filters can be applied retroactively |
| **Settings** | Adjust the dashboard's own runtime settings: stats/plugin/request poll intervals, console log level, and live PluginCore + networking info panels |
| **Per-Plugin** | Auto-generated tabs for each plugin (from endpoints or custom registration). Each tab includes a "Close Tab" button to detach the panel without affecting the underlying plugin |

### Plugin Registration API

Plugins can register custom TUI panels. The Dashboard checks these in priority order:

1. **`get_tui_module_info()` → dict** (recommended for rich UIs): Return `{"path": "...", "class_name": "..."}` pointing to a TUI package. The Dashboard imports it via importlib, registering it as a proper Python package so relative imports work.
2. **`get_tui_menu()` → dict** (no Textual dependency): Return a declarative dict describing menu sections and the Dashboard renders them automatically.
3. **Auto-generated view** from the plugin's registered endpoints (fallback).

When a plugin has a custom view, the tab shows toggle buttons to switch between "Custom View" and "Generated View".

See `plugins_test/CLI/CUSTOM_TABS.md` for full API documentation and examples.

### Key Bindings

| Key | Action |
|---|---|
| `q` | Request quit — opens a confirmation dialog before exiting |
| `ctrl+q` | Force-quit immediately (no confirmation) |
| `r` | Refresh stats, plugin table, requests, and config file list |
| `1`–`5` | Jump directly to the Home / Plugins / Config / Logs / Settings tabs |

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
        hosts="local"
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
        hosts="any"
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
            hosts="local"
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
        hosts="any"
    )

    # Stream from a remote node
    async for item in plugin_core.execute_stream(
        "RemotePlugin",
        "stream_method",
        args,
        hosts="remote"
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
├── notifier.py                # Topic-based pub/sub registry + matching engine
├── utils.py                   # Plugin base class, Request, GeneratorRequest,
│                              #   ConfigUtil, LogUtil, FDRedirector, EndOfQueue
├── decorators.py              # Error handling decorators (sync, async, generators)
├── exceptions.py              # Custom exception classes
├── main_application.py        # Example application entry point
├── config.yml                 # Main configuration file
├── notes.txt                  # Development notes and TODOs
├── README.md                  # This documentation file
├── CLAUDE.md                  # Repo-wide guidance for AI coding assistants
├── copypasta/                 # Plugin templates and examples
│   ├── README.md              # Template usage guide
│   ├── AveragePlugin/         # Example plugin template
│   │   ├── plugin.py
│   │   └── plugin_config.yml
│   └── config_structures.txt  # Config file structure reference
├── plugins_test/              # Test and example plugins
│   ├── pluginA_v1/            # Async plugin calling another plugin
│   ├── PluginB/               # Async calculation plugin
│   ├── PluginC/               # Sync plugin calling an async plugin
│   ├── CLI/                   # Textual TUI dashboard (stats, plugin mgmt, config editor, logs)
│   ├── InteropTarget/         # Interop test target (sync/async/generators)
│   ├── InteropCaller/         # Interop test runner
│   └── NetTest/               # Network testing plugin (echo, big objects, streaming)
└── logs/                      # Auto-generated log files (AIO_AI_<timestamp>.log)
```

---

## Logging

The system uses a custom logging utility (`LogUtil` in `utils.py`) that provides:

- **Non-blocking I/O**: Uses `QueueHandler` and `QueueListener` for thread-safe, non-blocking log output
- **Colored console output**: Uses `colorama` for color-coded log levels and components
- **File logging**: Timestamped log files in the `logs/` directory with plain-text formatting
- **Independent levels**: `LogUtil.change_level()` adjusts the console level at runtime; `LogUtil.change_file_level()` adjusts the file level. The two are tracked separately so you can run a quiet console with verbose file logs (or the reverse).
- **OS-level fd capture (`FDRedirector`)**: Intercepts stdout/stderr at the file-descriptor level (fd 1/2) so log lines emitted by C extensions (HuggingFace, ONNX, llama.cpp, etc.) — which bypass Python's `sys.stdout` — are still routed into the logging pipeline. Includes mute/unmute hooks for terminal capture (e.g. when the Textual TUI takes over the screen).
- **`_MutableStream`**: Wraps `sys.stdout` / `sys.stderr` so Python-level prints can be switched between terminal output and the log pipeline at runtime, with per-thread exemptions (used by Textual's render thread).
- **Per-logger Level Control**: Independent thresholds per logger and per handler, fully driven from `config.yml` (see subsection below). Replaces the older hardcoded `propagate = False` block — noisy third-party libs are now clamped, not silenced, and the user can adjust them at any time.
- **Automatic cleanup**: `QueueListener` and `FDRedirector` are stopped via an `atexit` hook.

Log files are automatically created in the `logs/` directory with format: `AIO_AI_YYYY-MM-DD_HH-MM-SS.log`. Each plugin and component gets its own child logger via standard Python logger naming (e.g., `root.PluginA`, `root.networking`).

### Per-logger Level Control

Set thresholds per logger (and optionally split between console and file) under `general.logger_levels` in `config.yml`:

```yaml
general:
    console_log_level: "DEBUG"
    file_log_level: "DEBUG"
    logger_levels:
        asyncio: "MUTE"             # shorthand → both handlers
        urllib3: "MUTE"
        httpx: "WARNING"
        httpcore: "WARNING"
        psycopg: "WARNING"
        psycopg.pool: "WARNING"
        # explicit per-handler split:
        myverbose.lib:
            console: "WARNING"
            file: "DEBUG"
```

**Levels.** `DEBUG | INFO | WARNING | ERROR | CRITICAL | MUTE`. `MUTE` drops the record entirely (and skips even the queue, so muted high-volume loggers cost almost nothing).

**Prefix matching.** Keys match by prefix with a dot boundary — `httpx` covers `httpx`, `httpx.client`, `httpx.client.send`, but does NOT match `httpxlib`. Longest matching prefix wins, so `httpx.client: ERROR` overrides a broader `httpx: DEBUG` for that subtree. Use `httpx` (no `.*` suffix) — the prefix already covers all sub-loggers; wildcards are rejected at load time.

**Sources.** Two sources can set thresholds:
- **Config-driven** — entries under `general.logger_levels`. Replaced wholesale on every `load_config_yaml()` (including hot-reload via `async_load_config_yaml()`).
- **Plugin-driven** — runtime overrides set by plugins. These survive config reloads but auto-clear when the owning plugin is disabled, popped, purged, or shut down.

For the same prefix, plugin-driven wins over config-driven.

**Plugin API.** Inside any `Plugin` subclass:

```python
self.set_logger_level("myplugin.foo", console="ERROR")          # split is optional
self.set_logger_level("noisylib", console="MUTE", file="MUTE")
self.clear_logger_level("myplugin.foo")                         # or per-handler
levels = self.list_logger_levels()                              # debug snapshot
```

Owner identity is auto-filled from `self.plugin_name` / `self.plugin_uuid`; plugins never pass it manually. A plugin's overrides only persist while the plugin is loaded — hot-swap creates a fresh instance with a new uuid, so the old uuid's entries clear automatically. The new instance can re-set thresholds in `on_load` or `on_enable`.

**Migration note.** Earlier versions used a hardcoded `propagate = False` block in `LogUtil.create()` for `httpx`, `httpcore`, `psycopg`, `psycopg.pool`, `asyncio`, `urllib3` — that block fully silenced those libs. The new system replaces it with config-driven thresholds (defaults shown above ship in this repo's `config.yml`). If you keep a custom `config.yml`, copy the six default `logger_levels` entries into it on first upgrade — otherwise those libraries will become noisy on the first run after the upgrade. WARNING+ records that were previously invisible will now surface; this is intentional.

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
