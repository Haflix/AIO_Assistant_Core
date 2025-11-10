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
10. [Examples](#examples)

---

## Overview

**AIO Assistant Core** is a powerful plugin loader and management system designed to make connecting scripts (both synchronous and asynchronous) easier across multiple devices. It provides a unified framework for plugin communication, whether locally or over a network.

### Key Features

- **Hot-Pluggable Plugins**: Plugins can be loaded, enabled, disabled, and removed dynamically
- **Synchronous & Asynchronous Support**: Seamless integration between sync and async code
- **Network Communication**: Optional networking layer for distributed plugin execution across devices
- **Simple API**: One-liner syntax for executing plugin methods
- **Streaming Support**: Support for generator-based data streams
- **Error Handling**: Built-in error handling and logging system

---

## Getting Started

### Installation

```bash
# Clone the repository
git clone <repository-url>
cd AIO_Assistant_Core

# Install dependencies
pip install -r requirements.txt  # (if available)
```

### Basic Usage

```python
import asyncio
from PluginCore import PluginCore

async def main():
    # Initialize the plugin system
    plugin_core = PluginCore("config.yml")
    
    # Wait for plugins to be loaded
    await plugin_core.wait_until_ready()
    
    # Execute a plugin method
    result = await plugin_core.execute("PluginB", "calculate_square", 6, host="local")
    print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Architecture

### High-Level Flow

```
1. main_application.py loads PluginCore with config.yml
2. PluginCore loads plugins based on configuration
3. PluginCore initializes NetworkManager (if enabled)
4. Application can execute plugin methods via PluginCore.execute()
5. PluginCore creates Request objects to manage plugin calls
6. Results are returned to the caller
```

### Component Interaction

```
┌─────────────────┐
│  main_application│
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   PluginCore    │◄───────┐
└────────┬────────┘        │
         │                 │
         ├─────────────────┤
         │                 │
    ┌────▼────┐      ┌─────▼─────┐
    │ Plugins │      │  Network  │
    └─────────┘      │  Manager  │
                     └───────────┘
```

---

## Core Components

### PluginCore

The main class that manages all plugins and facilitates communication between them.

**Location**: `PluginCore.py`

**Key Responsibilities**:
- Loading and managing plugins
- Handling plugin requests
- Managing plugin lifecycle (enable/disable)
- Coordinating with NetworkManager for remote execution
- Request processing and result management

**Key Methods**:
- `execute()` - Execute a plugin method asynchronously
- `execute_sync()` - Execute a plugin method synchronously
- `execute_stream()` - Execute a generator/streaming plugin method
- `wait_until_ready()` - Wait for plugins to finish loading
- `get_plugins()` - Load plugins from configuration
- `start_plugins()` - Enable all loaded plugins
- `purge_plugins()` - Unload all plugins
- `pop_plugin()` - Remove a specific plugin

### NetworkManager

Manages network communication between multiple nodes for distributed plugin execution.

**Location**: `networking.py`

**Key Features**:
- Node discovery (auto-discovery and manual node IPs)
- FastAPI-based REST API for plugin communication
- Plugin availability checking across nodes
- Remote plugin execution
- Streaming support for remote generators

**HTTP Endpoints**:
- `GET /ping` - Health check
- `GET /has_plugin` - Check if a plugin exists
- `POST /info` - Exchange node information
- `POST /execute` - Execute a plugin method remotely
- `POST /execute_stream` - Execute a streaming plugin method remotely

### Plugin Base Class

All plugins must inherit from the `Plugin` base class.

**Location**: `utils.py`

**Required Methods**:
- `on_load(*args, **kwargs)` - Called when plugin is loaded
- `on_enable()` - Called when plugin is enabled (async)
- `on_disable()` - Called when plugin is disabled (async)

**Properties**:
- `plugin_name` - Name of the plugin
- `version` - Plugin version
- `plugin_uuid` - Unique identifier for the plugin instance
- `enabled` - Whether the plugin is currently enabled
- `remote` - Whether the plugin supports remote execution
- `description` - Plugin description
- `endpoints` - Dictionary of exposed methods/endpoints

---

## Plugin System

### Creating a Plugin

1. **Create a plugin directory** (e.g., `plugins_test/MyPlugin/`)

2. **Create `plugin.py`**:
```python
from utils import Plugin
from decorators import async_log_errors

class MyPlugin(Plugin):
    """Example plugin."""
    
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

1. **Loading**: Plugin class is instantiated, `on_load()` is called
2. **Enabling**: `on_enable()` async method is called
3. **Active**: Plugin is ready to receive requests
4. **Disabling**: `on_disable()` async method is called
5. **Unloading**: Plugin instance is removed from memory

### Calling Other Plugins

From within a plugin:

```python
# Async execution
result = await self.execute("PluginName", "method_name", args_tuple, host="local")

# With keyword arguments
result = await self.execute("PluginName", "method_name", {"key": "value"}, host="any")

# Host options: "local", "remote", "any", or specific hostname
```

### Streaming/Generator Support

For plugins that yield results:

```python
# In the calling code
async for item in plugin_core.execute_stream("PluginA", "streaming_method", args):
    print(item)
```

---

## Networking

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
```

### Node Discovery

- **Manual Nodes**: Specify IP addresses in `node_ips`
- **Auto Discovery**: Set `discover_nodes: True` for automatic network discovery
- **Direct Discoverable**: Allows other nodes to discover this node directly
- **Auto Discoverable**: Enables automatic discovery broadcasting

### Remote Execution

When networking is enabled, you can execute plugins on remote nodes:

```python
# Execute on any available node (local or remote)
result = await plugin_core.execute("RemotePlugin", "method", args, host="any")

# Execute only on remote nodes
result = await plugin_core.execute("RemotePlugin", "method", args, host="remote")

# Execute on specific node by hostname
result = await plugin_core.execute("RemotePlugin", "method", args, host="hostname")
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
    hostname: ""  # Unique identifier for this node
    plugin_package: plugins_test  # Base directory for plugins
    console_log_level: "INFO"  # DEBUG, INFO, WARNING, ERROR

# Networking Configuration
networking:
    enabled: False
    node_ips: []  # List of known node IP addresses
    port: 2510
    discover_nodes: True
    direct_discoverable: True
    auto_discoverable: True
```

### Configuration Options

**General**:
- `hostname`: Unique identifier for this node (used in network communication)
- `plugin_package`: Default directory where plugins are located if path is not specified
- `console_log_level`: Logging level for console output

**Plugins**:
- `name`: Plugin name (must match class name)
- `enabled`: Whether to load and enable this plugin
- `path`: Optional explicit path to plugin directory

**Networking**:
- `enabled`: Enable/disable networking
- `node_ips`: List of known node IP addresses for manual connection
- `port`: Port number for network communication
- `discover_nodes`: Enable automatic node discovery
- `direct_discoverable`: Allow direct connection from other nodes
- `auto_discoverable`: Broadcast availability for auto-discovery

---

## API Reference

### PluginCore Methods

#### `execute(plugin, method, args=None, plugin_uuid="", host="any", ...)`

Execute a plugin method asynchronously.

**Parameters**:
- `plugin` (str): Name of the plugin
- `method` (str): Method name to execute
- `args` (tuple/dict/None): Arguments to pass to the method
- `plugin_uuid` (str): Optional UUID to target specific plugin instance
- `host` (str): "local", "remote", "any", or hostname
- `author` (str): Name of the caller
- `timeout` (float): Optional timeout in seconds

**Returns**: Result from the plugin method

**Raises**: `RequestException` if execution fails

#### `execute_sync(plugin, method, args=None, ...)`

Synchronous wrapper for `execute()`. Uses `asyncio.run_coroutine_threadsafe()`.

**Parameters**: Same as `execute()`

**Returns**: Result from the plugin method

#### `execute_stream(plugin, method, args=None, ...)`

Execute a generator/streaming plugin method.

**Parameters**: Same as `execute()`

**Returns**: Async generator yielding (result, error, request_id) tuples

#### `wait_until_ready()`

Wait for plugins and network to finish initialization.

**Returns**: None (coroutine)

#### `get_plugins()`

Load plugins from configuration.

**Returns**: None (coroutine)

#### `start_plugins()`

Enable all loaded plugins.

**Returns**: None (coroutine)

#### `purge_plugins()`

Unload all plugins.

**Returns**: None (coroutine)

#### `pop_plugin(plugin_name)`

Remove a specific plugin by name.

**Parameters**:
- `plugin_name` (str): Name of plugin to remove

**Returns**: None (coroutine)

---

## Error Handling

### Exception Classes

**Location**: `exceptions.py`

- `ConfigException`: Configuration-related errors
- `RequestException`: Plugin request execution errors
- `NetworkRequestException`: Network communication errors
- `NodeException`: Node-related errors

### Error Decorators

**Location**: `decorators.py`

- `@log_errors`: Log exceptions without stopping execution
- `@handle_errors(default_return=...)`: Catch exceptions and return default value
- `@async_log_errors`: Async version of `@log_errors`
- `@async_handle_errors(default_return=...)`: Async version of `@handle_errors`

### Usage

```python
from decorators import async_handle_errors

@async_handle_errors(default_return=None)
async def my_method(self):
    # If an exception occurs, returns None instead of raising
    return risky_operation()
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
    
    # Execute a simple calculation
    result = await plugin_core.execute(
        "PluginB", 
        "calculate_square", 
        6, 
        host="local"
    )
    print(f"Square of 6: {result}")

asyncio.run(main())
```

### Example 2: Streaming Results

```python
async def main():
    plugin_core = PluginCore("config.yml")
    await plugin_core.wait_until_ready()
    
    # Process streaming results
    async for item in plugin_core.execute_stream(
        "PluginA", 
        "perform_operation_stream_ABC", 
        (9), 
        host="any"
    ):
        print(f"Received: {item}")
```

### Example 3: Plugin-to-Plugin Communication

```python
from utils import Plugin
from decorators import async_log_errors

class PluginA(Plugin):
    def on_load(self, *args, **kwargs):
        self.plugin_name = "PluginA"
        
    @async_log_errors
    async def on_enable(self):
        # Call another plugin from within this plugin
        result = await self.execute(
            "PluginB", 
            "calculate_square", 
            5, 
            host="local"
        )
        self._logger.info(f"Got result: {result}")
        
    async def process_data(self, data):
        # Your plugin logic here
        return processed_data
```

### Example 4: Error Handling

```python
from decorators import async_handle_errors

class SafePlugin(Plugin):
    @async_handle_errors(default_return=0)
    async def safe_calculation(self, x):
        # If this raises an exception, returns 0 instead
        return x / 0  # This would normally crash
        
    @async_log_errors
    async def logged_operation(self):
        # Exceptions are logged but still raised
        return risky_operation()
```

### Example 5: Remote Execution

```python
async def main():
    plugin_core = PluginCore("config.yml")
    await plugin_core.wait_until_ready()
    
    # Execute on any available node (will find remote if local not available)
    result = await plugin_core.execute(
        "RemotePlugin", 
        "remote_method", 
        args, 
        host="any"
    )
```

---

## Interop Test Plugins

Two plugins validate sync/async calls and streaming (generators):

- `InteropTarget`: exposes endpoints
  - `it_sync_add(a, b=1)`, `it_async_add(a, b=1)`
  - `it_sync_gen(n=3, prefix="g", delay_ms=10)`, `it_async_gen(n=3, prefix="ag", delay_ms=10)`
- `InteropCaller`: runs the matrix via `interop_run(host="any")` and logs results.

Usage:

```python
# Async context
result = await self.execute("InteropCaller", "interop_run", {"host": "any"})

# Sync context
result = self.execute_sync("InteropCaller", "interop_run_sync", {"host": "any"})

# Cross-device
result = await self.execute("InteropCaller", "interop_run", {"host": "remote"})
```

Host options: "any", "local", "remote", or a specific hostname.

---

## File Structure

```
AIO_Assistant_Core/
├── PluginCore.py          # Main plugin management class
├── networking.py           # Network communication manager
├── networking_classes.py   # Node and RemotePlugin classes
├── utils.py                # Utilities: Plugin base class, Request, ConfigUtil, LogUtil
├── decorators.py           # Error handling decorators
├── exceptions.py           # Custom exception classes
├── main_application.py     # Example application entry point
├── config.yml              # Main configuration file
├── docs.md                 # This documentation file
├── README.md               # Project overview
├── plugins_test/           # Example plugins directory
│   ├── PluginA/
│   ├── PluginB/
│   └── PluginC/
```

---

## Logging

The system uses a custom logging utility (`LogUtil`) that provides:

- **Colored console output** for better readability
- **File logging** with timestamped log files in `logs/` directory
- **Hierarchical loggers** for different components
- **Configurable log levels** via configuration

Log files are automatically created in the `logs/` directory with format: `AIO_AI_YYYY-MM-DD_HH-MM-SS.log`

---

## Future Plans

The following features are planned but not yet implemented:

- **Hot-swapping plugins**: Dynamically reload plugins without restarting (
- **Data streams**: Enhanced support for `yield` and streaming operations
- **Plugin caching**: Cache plugin locations on remote devices for faster lookup
- **Security features**: Authentication and authorization for remote plugin execution

---

## Contributing

When creating plugins or contributing to the core:

1. Follow the plugin structure outlined in the [Plugin System](#plugin-system) section
2. Use appropriate error handling decorators
3. Document your plugin methods clearly
4. Test both local and remote execution (if applicable)
5. Update this documentation if adding new features

---

## License

See `LICENSE` file for details.

---

## Support

For issues, questions, or contributions, please refer to the project repository.

