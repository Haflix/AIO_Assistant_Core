# Plugin Templates for AIO Assistant Core

This folder contains templates and examples for creating plugins in the AIO Assistant Core system.

## Contents

- **AveragePlugin/**: A complete example plugin demonstrating all core features
  - `plugin.py`: Plugin implementation with examples of different method types
  - `plugin_config.yml`: Configuration file with full endpoint documentation
- **config_structures.txt**: Documentation of config.yml and plugin_config.yml structures

## Quick Start: Creating a New Plugin

### 1. Copy the Template

```bash
# Copy the AveragePlugin folder to your plugins directory
cp -r copypasta/AveragePlugin plugins_test/MyNewPlugin
```

### 2. Update plugin.py

1. Rename the class from `AveragePlugin` to `MyNewPlugin` (must match your folder name)
2. Update the docstring to describe your plugin
3. Implement your plugin methods in `on_load()`, `on_enable()`, and `on_disable()`
4. Add your custom methods following the examples provided

### 3. Update plugin_config.yml

1. Change the `description` field to describe your plugin
2. Update the `version` field (use semantic versioning: MAJOR.MINOR.PATCH)
3. Set `remote: True` if you want the plugin accessible over the network, or `False` for local only
4. Update the `endpoints` section to match your plugin's methods:
   - Set `internal_name` to match the method name in your class
   - Set `access_name` to what other plugins will use to call it (usually the same)
   - Update `description` and `arguments` for each endpoint

### 4. Add to config.yml

Add your plugin to the main `config.yml` file:

```yaml
plugins:
  - name: MyNewPlugin
    enabled: true
    path: ./plugins_test/MyNewPlugin  # Optional if using default plugin_package
```

## Plugin Structure Overview

### Required Methods

Every plugin must implement these lifecycle methods:

- **`on_load(self, *args, **kwargs)`**: Called when plugin is first loaded (sync)
- **`on_enable(self)`**: Called when plugin is enabled (async)
- **`on_disable(self)`**: Called when plugin is disabled (async)

### Available Decorators

Import from `decorators` module:

- **`@log_errors`**: Logs exceptions for sync functions
- **`@handle_errors(default_return=value)`**: Logs and handles exceptions for sync functions
- **`@async_log_errors`**: Logs exceptions for async functions
- **`@async_handle_errors(default_return=value)`**: Logs and handles exceptions for async functions
- **`@async_gen_log_errors`**: Logs exceptions for async generators
- **`@async_gen_handle_errors(default_return=value)`**: Logs and handles exceptions for async generators
- **`@gen_log_errors`**: Logs exceptions for sync generators
- **`@gen_handle_errors(default_return=value)`**: Logs and handles exceptions for sync generators

### Calling Other Plugins

From within your plugin:

```python
# Call another plugin's method (direct — you must know the plugin name)
result = await self.execute("PluginName", "method_name", args, host="any")

# Host options:
# - "local": Only call locally loaded plugins
# - "remote": Only call plugins on remote nodes
# - "any": Try local first, then remote
# - "hostname": Call on specific node by hostname

# Stream from another plugin
async for item in self.execute_stream("PluginName", "stream_method", args, host="any"):
    print(item)
```

### Topic-Based Communication (Notifier System)

Use topics to decouple plugins — the caller doesn't need to know which plugin handles the request.

```python
# Fire-and-forget (one-to-many) — all subscribers are called
count = await self.notify("sensor/temperature", {"value": 22.5})

# Request-by-topic (one-to-one with response) — first matching handler
result = await self.request_topic("ai/chat", {"message": "hello"})

# Streaming request-by-topic
async for chunk in self.request_topic_stream("ai/stream", args):
    print(chunk)

# Sync variants available too:
self.notify_sync("sensor/temperature", {"value": 22.5})
result = self.request_topic_sync("ai/chat", {"message": "hello"})
```

**Subscribing to topics — two ways:**

1. **Config-driven** (in plugin_config.yml):
```yaml
endpoints:
  - internal_name: _handle_chat
    access_name: handle_chat
    topic: "ai/chat"            # auto-subscribed on plugin load
    remote: True
    accessible_by_other_plugins: True
```

2. **Code-driven** (at runtime, typically in `on_enable`):
```python
async def on_enable(self):
    self._sub_id = await self.subscribe("events/*", self._on_event)

async def on_disable(self):
    await self.unsubscribe(self._sub_id)
```

**Topics** use `/` as separator. Single-level wildcard `*` matches one segment:
- `sensor/*/temperature` matches `sensor/bathroom/temperature`
- `sensor/*` does NOT match `sensor/bathroom/temperature` (different segment count)

### Accessing Plugin Properties

- **`self._logger`**: Logger instance for your plugin
- **`self._plugin_core`**: Reference to the PluginCore instance
- **`self.plugin_name`**: Your plugin's name
- **`self.enabled`**: Whether the plugin is currently enabled

## Configuration Files

### plugin_config.yml Structure

```yaml
description: str                 # What your plugin does
version: str                     # Semantic version (e.g., "1.0.0")
remote: boolean                  # Allow remote access
arguments:                       # Optional: Load-time arguments
endpoints:
  - internal_name: method_name   # Method in your class
    access_name: method_name     # Name others use to call it
    topic: "some/topic"          # Optional: Subscribe to a notifier topic
    tags: []                     # Optional categorization tags
    remote: boolean              # Allow remote calls
    accessible_by_other_plugins: boolean
    description: str             # What the method does
    arguments:
      - name: param_name
        type: str                # int, str, dict, list, any, bool, float
        description: str
```

### config.yml Plugin Entry

```yaml
plugins:
  - name: YourPluginName          # Must match class name
    enabled: true                 # Load on startup
    path: ./path/to/plugin        # Optional: explicit path
    arguments:                    # Optional: Pass data to plugin
      key: value
```

## Examples

### Simple Async Method

```python
@async_log_errors
async def my_method(self, arg1, arg2):
    """Do something with arguments."""
    result = arg1 + arg2
    return result
```

### Method with Error Handling

```python
@async_handle_errors(default_return=0)
async def safe_method(self, value):
    """This returns 0 if an error occurs."""
    return value * 2
```

### Streaming Method

```python
@async_gen_log_errors
async def my_stream(self, count):
    """Yield multiple results."""
    for i in range(count):
        await asyncio.sleep(0.1)
        yield f"Item {i}"
```

### Calling Another Plugin

```python
@async_log_errors
async def call_other(self):
    """Call another plugin's method."""
    result = await self.execute("OtherPlugin", "method_name", {"arg": "value"}, host="any")
    return result
```

## Tags

Tags are arbitrary strings you can assign to endpoints for categorization. Other plugins can use `find_endpoints_by_tag(tag)` on the PluginCore to discover endpoints with a specific tag. This is useful for building systems where plugins need to dynamically discover each other's capabilities.

The AI system uses mode-based tags to discover tools at runtime:
- `AI-minimum` — Available in all modes (device control, weather)
- `AI-conversation` — Available in conversation+ modes (memory, tasks, appointments, sessions)
- `AI-working` — Available in working+ modes (documents, web search, notes, locations)
- `AI-debug` — Available only in debug mode (all raw CRUD endpoints)

```yaml
tags: ["AI-minimum", "AI-conversation"]  # Available in minimum, conversation, working, and debug modes
```

## Best Practices

1. **Always use decorators** on your methods for proper error handling and logging
2. **Use async methods** for I/O-bound operations (network, file I/O, etc.)
3. **Document your endpoints** thoroughly in plugin_config.yml
4. **Version your plugins** using semantic versioning
5. **Clean up resources** in `on_disable()` (close files, cancel tasks, etc.)
6. **Test locally first** before enabling remote access
7. **Use meaningful names** for methods and arguments
8. **Return JSON-serializable data** (dicts, lists, strings, numbers, bools, None)

## Troubleshooting

### Plugin Not Loading

- Check that the class name matches the plugin name in config.yml
- Verify that plugin.py and plugin_config.yml are in the same folder
- Check logs for specific error messages

### Method Not Callable

- Verify the method is listed in the `endpoints` section of plugin_config.yml
- Check that `accessible_by_other_plugins` is set to `True`
- Ensure the method name matches both `internal_name` and the actual method

### Remote Access Not Working

- Verify `networking.enabled: True` in config.yml
- Check that `remote: True` in both plugin_config.yml (top level) and endpoint level
- Ensure firewall allows connections on the configured port

## Additional Resources

See the main README.md in the project root for:
- Full API reference
- Networking configuration
- Advanced features
- More examples
