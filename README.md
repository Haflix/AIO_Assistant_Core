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

# Install dependencies
pip install pyyaml colorama
# For networking with auto-generated TLS certificates:
pip install cryptography
# For the CLI plugin:
pip install prompt_toolkit
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
    loop = asyncio.get_event_loop()
    try:
        loop.create_task(main())
        loop.run_forever()
    finally:
        loop.close()
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

### Endpoint Access Control

Endpoints have two access flags in `plugin_config.yml`:

- `accessible_by_other_plugins`: Controls whether other local plugins can call this endpoint. If `False`, only the plugin itself can invoke it.
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
        description: str
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

All decorators include automatic type checking -- using the wrong decorator (e.g., `@async_log_errors` on a sync function) raises `PluginTypeMissmatchError` with a message indicating the correct decorator.

**For sync functions:**

- `@log_errors` / `@log_errors()`: Log exceptions without stopping execution, then re-raise
- `@handle_errors(default_return=...)`: Catch exceptions, log them, and return a default value

**For async functions:**

- `@async_log_errors`: Log exceptions without stopping execution, then re-raise
- `@async_handle_errors(default_return=...)`: Catch exceptions, log them, and return a default value

**For sync generators:**

- `@gen_log_errors` / `@gen_log_errors()`: Log exceptions in sync generators, then re-raise
- `@gen_handle_errors(default_return=...)`: Catch exceptions in sync generators and stop the generator

**For async generators:**

- `@async_gen_log_errors` / `@async_gen_log_errors()`: Log exceptions in async generators, then re-raise
- `@async_gen_handle_errors(default_return=...)`: Catch exceptions in async generators and stop the generator

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
├── notes.txt                  # Development notes and TODOs
├── README.md                  # This documentation file
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
│   ├── CLI/                   # Interactive command-line interface plugin
│   ├── InteropTarget/         # Interop test target (sync/async/generators)
│   ├── InteropCaller/         # Interop test runner
│   └── NetTest/               # Network testing plugin (echo, big objects, streaming)
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
