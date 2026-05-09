# API Reference

*Last updated for AIO Assistant Core 0.22.3*

Reference manual for the public surface of `utils.Plugin` — the methods and attributes a plugin author calls from inside their own class. Methods on `PluginCore` itself are covered at the end for tooling and harness authors.

All entries cite the source file and line number. Argument types use Python conventions; `Any` means no constraint. For tutorials and patterns, see [plugin authoring](./plugin_authoring.md). For event-system semantics, see [notifier](./notifier.md).

---

## Table of contents

- [Lifecycle hooks](#lifecycle-hooks)
- [Public attributes](#public-attributes)
- [Cross-plugin call methods](#cross-plugin-call-methods)
  - [`execute`](#await-selfexecuteplugin-method-argsnone-plugin_uuid-hostsany-blocked_hostsnone-authorsystem-author_idsystem-timeoutnone---any)
  - [`execute_sync`](#selfexecute_syncplugin-method-argsnone-plugin_uuid-hostsany-blocked_hostsnone-authorsystem-author_idsystem-timeoutnone---any)
  - [`execute_stream`](#async-for-chunk-in-selfexecute_streamplugin-method-argsnone-plugin_uuid-hostsany-blocked_hostsnone-authorsystem-author_idsystem-timeoutnone)
  - [`execute_stream_sync`](#for-chunk-in-selfexecute_stream_syncplugin-method-argsnone-plugin_uuid-hostsany-blocked_hostsnone-authorsystem-author_idsystem-timeoutnone)
- [Event API](#event-api)
  - [`publish_event`](#await-selfpublish_eventevent_id-payloadnone-topic_varsnone-hostsnone-blocked_hostsnone---int)
  - [`publish_event_sync`](#selfpublish_event_syncevent_id-payloadnone-topic_varsnone-hostsnone-blocked_hostsnone---int)
  - [`request_event`](#await-selfrequest_eventevent_id-payloadnone-topic_varsnone-hostsnone-blocked_hostsnone-timeoutnone---any)
  - [`request_event_sync`](#selfrequest_event_syncevent_id-payloadnone-topic_varsnone-hostsnone-blocked_hostsnone-timeoutnone---any)
  - [`request_event_stream`](#async-for-chunk-in-selfrequest_event_streamevent_id-payloadnone-topic_varsnone-hostsnone-blocked_hostsnone-timeoutnone)
  - [`request_event_stream_sync`](#for-chunk-in-selfrequest_event_stream_syncevent_id-payloadnone-topic_varsnone-hostsnone-blocked_hostsnone-timeoutnone)
  - [`topic_vars` constraints](#topic_vars-constraints)
- [Subscribe / unsubscribe at runtime](#subscribe--unsubscribe-at-runtime)
- [Logger administration](#logger-administration)
- [Decorators](#decorators)
- [The `Event` object](#the-event-object)
- [Exceptions](#exceptions)
- [Argument-shape contract (canonical)](#argument-shape-contract-canonical)
- [PluginCore methods](#plugincore-methods-for-tooling-harnesses-cli-authors)
- [Quick reference card](#quick-reference-card)

---

## Lifecycle hooks

These are abstract — every concrete plugin must define them. See [architecture](./architecture.md) for the lifecycle contract and [plugin authoring](./plugin_authoring.md) for examples.

`Plugin.__init__` is `@final` (`utils.py:1172-1223`). Subclasses MUST NOT override it — declare instance state in `on_load` instead.

### `on_load(self, *args, **kwargs) -> None`

Source: `utils.py:1737-1739`. Synchronous. Called inside `Plugin.__init__`. Must NOT be `async def`.

The arguments come from `plugin_config.yml`'s `arguments:` field, unpacked by shape: list/tuple → `*args`, dict → `**kwargs`, anything else → no args.

### `on_enable(self) -> None`

Source: `utils.py:1743-1745`. May be `async def` or `def`. Called once, after framework registration. Sync versions run on the framework's plugin executor.

### `on_disable(self) -> None`

Source: `utils.py:1749-1751`. May be `async def` or `def`. The framework wraps the call in `asyncio.wait_for` with a configurable runtime budget (`general.plugin_disable_timeout`, default 30.0s) for `pop_plugin` / `_disable_plugin` paths. The shutdown path in `PluginCore.close()` (`PluginCore.py:797`) hardcodes a separate 30.0s cap that is NOT controlled by the same setting — these are independent timeouts.

---

## Public attributes

Set by `Plugin.__init__` (`utils.py:1172-1223`) before `on_load` runs, then partially overwritten by the framework after `on_load` returns.

| Name                 | Type             | Description                                                                                                       |
|----------------------|------------------|-------------------------------------------------------------------------------------------------------------------|
| `self._plugin_core`  | `PluginCore`     | Back-reference to the running core. Prefer the wrapper methods below over reaching into it directly.              |
| `self._logger`       | `logging.Logger` | Plugin-scoped logger (`{root}.{plugin_name}`).                                                                    |
| `self.plugin_name`   | `str`            | Name from `config.yml` (NOT the class name). Set after `on_load` returns.                                         |
| `self.plugin_uuid`   | `str`            | uuid4 hex; unique per instance, regenerated on every load and reload.                                             |
| `self.arguments`     | `Any`            | Raw merged `arguments:` from manifest plus overrides.                                                             |
| `self.endpoints`     | `dict`           | Same dict the framework uses. Keyed by access_name.                                                               |
| `self.events`        | `dict`           | Parsed `events:` block (post-load-time templating).                                                               |
| `self.subscriptions` | `dict`           | Parsed `subscriptions:` block.                                                                                    |
| `self.prefix`        | `str`            | Resolved prefix for `{prefix}` substitution. Defaults to `plugin_name`.                                           |
| `self.verbose_notifier` | `bool`        | When `true`, notifier dispatch logs include match counts.                                                         |
| `self.enabled`       | `bool` (read-only `@property` since v0.26.0) | `True` for state in `{ENABLING, ENABLED}`. Direct writes raise `AttributeError`. Use `pc.enable_plugin(name)` / `pc.disable_plugin(name)` to change state. |
| `self.remote`        | `bool`           | Plugin-level remote flag from manifest.                                                                            |
| `self.description`   | `str`            | From manifest.                                                                                                    |
| `self.version`       | `str`            | From manifest.                                                                                                    |
| `self.event_loop`    | event loop       | The framework's `main_event_loop`.                                                                                |
| `self.ready`         | `asyncio.Event`  | AUTHOR-controlled readiness gate. Defaults SET. Clear in `on_enable` if you have async setup work to finish.      |
| `self._lifecycle_ready` | `asyncio.Event` | FRAMEWORK-controlled. Authors must NOT touch.                                                                   |
| `self._sub_uuids`    | `list[str]`      | Subs registered on this plugin's behalf. Authors should treat as read-only.                                       |

---

## Cross-plugin call methods

Each method below has signature, args, return, raises, and behaviour notes. The `args` parameter has a unified shape contract across all of them:

| `args=` value | Endpoint receives |
|---------------|-------------------|
| `tuple`       | `func(*args)`     |
| `dict`        | `func(**args)`    |
| `None`        | `func()`          |
| anything else | `func(args)`      |

---

### `await self.execute(plugin, method, args=None, plugin_uuid="", hosts="any", blocked_hosts=None, author="system", author_id="system", timeout=None) -> Any`

Source: `utils.py:1278-1319`.

| Argument | Type | Default | Notes |
|---|---|---|---|
| `plugin` | `str` | — | Target plugin's `plugin_name`. |
| `method` | `str` | — | Target endpoint's `access_name`. |
| `args` | `tuple \| dict \| None \| Any` | `None` | See shape contract above. |
| `plugin_uuid` | `str` | `""` | Pin to a specific instance. Empty = any instance with this name. |
| `hosts` | `str \| list \| None` | `"any"` | `"any"`, `"local"`, `"remote"`, hostname, or list. |
| `blocked_hosts` | `str \| list \| None` | `None` | Same shape as `hosts`. |
| `author` | `str` | `"system"` | Caller-side author identity for filter chains. |
| `author_id` | `str` | `"system"` | Caller-side author uuid. |
| `timeout` | `float \| None` | `None` | Per-call deadline. Framework-level default if `None`. |

**Returns** Whatever the endpoint returns.

**Raises** `RequestException` on any error (target not found, target not ready, type mismatch, target raised, network failure). `NetworkRequestException` and `NoLocalSubException` are subclasses, so a single `except RequestException` covers both.

**Decorator** `@async_log_errors`.

---

### `self.execute_sync(plugin, method, args=None, plugin_uuid="", hosts="any", blocked_hosts=None, author="system", author_id="system", timeout=None) -> Any`

Source: `utils.py:1322-1368`.

Synchronous bridge. Calls `_check_framework_started()` first; raises `RequestException` if no event loop is bound yet. Detects circular sync calls via a per-thread chain and raises `RequestException("Circular sync call: ...")` rather than deadlocking the executor. Bridges to the loop via `asyncio.run_coroutine_threadsafe`. Same args, same return, same `RequestException` on error.

**Decorator** `@log_errors`.

---

### `async for chunk in self.execute_stream(plugin, method, args=None, plugin_uuid="", hosts="any", blocked_hosts=None, author="system", author_id="system", timeout=None)`

Source: `utils.py:1371-1413`.

Async generator. Yields each chunk produced by the target generator/async-generator method. If the producer raises mid-stream, the call surfaces as `RequestException`.

No top-level decorator (the `@async_log_errors` line in source is commented out at `utils.py:1370`).

---

### `for chunk in self.execute_stream_sync(plugin, method, args=None, plugin_uuid="", hosts="any", blocked_hosts=None, author="system", author_id="system", timeout=None)`

Source: `utils.py:1416-1464` (with inner generator at 1466-1491).

Sync generator. Pre-start guard fires at call time, not at first iteration.

**Decorator** `@log_errors`.

---

## Event API

These methods are how plugins emit and request events.

### `await self.publish_event(event_id, payload=None, topic_vars=None, hosts=None, blocked_hosts=None) -> int`

Source: `utils.py:1551-1567`.

Fire-and-forget 1:N broadcast. Returns the number of subscribers (local + remote) the dispatch was scheduled for.

| Argument | Type | Default | Notes |
|---|---|---|---|
| `event_id` | `str` | — | Key in this plugin's `events:` manifest block. |
| `payload` | `Any` | `None` | Becomes `Event.payload`. |
| `topic_vars` | `Dict[str, str] \| None` | `None` | Fills runtime `{var}` placeholders. See constraints below. |
| `hosts` | `str \| list \| None` | `None` | Override the manifest's `hosts:`. |
| `blocked_hosts` | `str \| list \| None` | `None` | Override the manifest's `blocked_hosts:`. |

**Returns** `int` — count of subscribers scheduled.

**Raises** `ValueError` / `TypeError` for malformed `topic_vars` (see [`topic_vars` constraints](#topic_vars-constraints)). `RequestException` for unknown `event_id` or other framework-level errors. Subscriber-side errors are logged, not raised. Disabled events silently drop with a debug log and return `0`.

**Decorator** `@async_log_errors`.

---

### `self.publish_event_sync(event_id, payload=None, topic_vars=None, hosts=None, blocked_hosts=None) -> int`

Source: `utils.py:1570-1587`. Sync equivalent. Pre-start guard included.

---

### `await self.request_event(event_id, payload=None, topic_vars=None, hosts=None, blocked_hosts=None, timeout=None) -> Any`

Source: `utils.py:1590-1608`.

1:1 ask. Returns the FIRST matching handler's result, where matching order is the insertion order in the topic registry (YAML declaration order plus runtime registrations as they happen).

Local subs are tried first, in insertion order. On no local match, remote candidates are tried in advert-insertion order. Remote candidates that fail with `NoLocalSubException` (peer says "no local sub matched") or `NetworkRequestException` (transport-level failure) are SKIPPED, and the next candidate is tried. A generic `RequestException` from the handler itself PROPAGATES — fall-through stops the moment a handler runs and either returns or raises non-network errors.

| Argument | Type | Default | Notes |
|---|---|---|---|
| `event_id` | `str` | — | Key in this plugin's `events:` manifest block. |
| `payload` | `Any` | `None` | Becomes `Event.payload`. |
| `topic_vars` | `Dict[str, str] \| None` | `None` | See constraints below. |
| `hosts` | `str \| list \| None` | `None` | Override the manifest's `hosts:`. |
| `blocked_hosts` | `str \| list \| None` | `None` | Override the manifest's `blocked_hosts:`. |
| `timeout` | `float \| None` | `None` | Per-call deadline. |

**Returns** Whatever the matched handler returns.

**Raises** `RequestException` if no subscriber matches and no remote candidate succeeds. `RequestException("event ... disabled (C2)")` if the publisher's declared event is disabled.

**Decorator** `@async_log_errors`.

---

### `self.request_event_sync(event_id, payload=None, topic_vars=None, hosts=None, blocked_hosts=None, timeout=None) -> Any`

Source: `utils.py:1611-1630`. Sync equivalent.

---

### `async for chunk in self.request_event_stream(event_id, payload=None, topic_vars=None, hosts=None, blocked_hosts=None, timeout=None)`

Source: `utils.py:1632-1651`.

Streaming 1:1 ask. Same selection rules as `request_event`. Pre-first-chunk fall-through is identical (skip `NoLocalSubException` / `NetworkRequestException`, propagate other `RequestException`). Once the first chunk yields, the consumer is committed to that producer — no fall-through past the first yield. The `timeout` is a whole-stream budget enforced by the producer's monotonic deadline.

---

### `for chunk in self.request_event_stream_sync(event_id, payload=None, topic_vars=None, hosts=None, blocked_hosts=None, timeout=None)`

Source: `utils.py:1654-1689`. Sync equivalent. Pre-start guard fires at call time.

---

### `topic_vars` constraints

Validated in `PluginCore.py:3949-3997`.

- Type: `Dict[str, str]` or `None`. A non-dict, non-None value raises `TypeError`.
- Keys must be `str` (`TypeError` otherwise) and must NOT be in `{"prefix", "plugin_name", "hostname", "plugin_uuid"}` (`ValueError`).
- Values must be `str` (`TypeError` otherwise), must NOT contain `/`, must not be empty or whitespace-only, must not have leading/trailing whitespace (`ValueError`).
- Missing keys for `{var}` placeholders → `ValueError`.
- Extra keys not used by the template → warning logged.
- Static topic with non-empty `topic_vars` → warning logged.

These exceptions are NOT wrapped — `@async_log_errors` re-raises whatever was raised (`decorators.py:209`). The caller sees the raw `ValueError` / `TypeError`.

---

## Subscribe / unsubscribe at runtime

### `await self.subscribe(topic, target_access_name, *, target_plugin=None, target_plugin_uuid=None, hosts="any", blocked_hosts=None, authors=None, blocked_authors=None) -> str`

Source: `utils.py:1495-1530`.

Register a subscription at runtime (in addition to the declarative `subscriptions:` block).

| Argument | Type | Default | Notes |
|---|---|---|---|
| `topic` | `str` | — | Topic pattern. Wildcards (`*` per segment) allowed. |
| `target_access_name` | `str` | — | Endpoint that receives the dispatched `Event`. Must be non-empty. |
| `target_plugin` | `str \| None` | `None` | Owner plugin (defaults to self-routing). |
| `target_plugin_uuid` | `str \| None` | `None` | Optional instance pin. |
| `hosts` | `str \| list \| None` | `"any"` | Receiver-side host filter. |
| `blocked_hosts` | `str \| list \| None` | `None` | Receiver-side host filter. |
| `authors` | `str \| list \| None` | `None` | Author whitelist. |
| `blocked_authors` | `str \| list \| None` | `None` | Author blacklist. |

**Returns** `str` — the new `sub_uuid`. Pass this back to `unsubscribe`.

**Raises**
- `TypeError` if `target_access_name` is empty or non-string (`utils.py:1514-1518`).
- `ValueError` for malformed topic patterns or filter values — validated by `_validate_subscription_topic` (`PluginCore.py:5048`) and the `target_access_name` re-check at `PluginCore.py:5059-5064`. Topic and filter values are validated identically to YAML load.

> **Do not use** the legacy `handler=` keyword form — it was removed. Runtime subs always route to a NAMED endpoint via `target_access_name`.

---

### `await self.unsubscribe(subscription_id) -> bool`

Source: `utils.py:1532-1534`. Removes by `sub_uuid`. Returns `True` if a sub was removed, `False` otherwise.

---

### `self.subscribe_sync(...)` and `self.unsubscribe_sync(...)`

Source: `utils.py:1692-1733`. Sync equivalents that bridge to the event loop via `run_coroutine_threadsafe`.

---

## Logger administration

Set per-logger thresholds at runtime. Plugin-source overrides survive config reloads but are auto-cleared on `on_disable`, `pop_plugin`, `purge_plugins`, or shutdown.

### `self.set_logger_level(name, *, console=None, file=None) -> None`

Source: `utils.py:1237-1255`. Override threshold on a specific logger. `name` is the dotted logger name (e.g. `"httpx"`). Pass `console=` and/or `file=` strings (`"DEBUG"`, `"INFO"`, `"WARNING"`, `"ERROR"`, `"CRITICAL"`, `"MUTE"`).

### `self.clear_logger_level(name, *, console=True, file=True) -> None`

Source: `utils.py:1257-1271`. Remove this plugin's overrides on the named logger. Other plugins' overrides on the same logger survive.

### `self.list_logger_levels() -> dict`

Source: `utils.py:1273-1275`. Snapshot of all per-logger thresholds.

---

## Decorators

All eight error-handling decorators live in `decorators.py`. They come in matched (sync / async / generator / async generator) × (`log_errors` / `handle_errors`) pairs.

| Decorator                              | Source                | Function shape       | Behaviour on raise                                          |
|----------------------------------------|-----------------------|----------------------|-------------------------------------------------------------|
| `log_errors(logger=None)`              | `decorators.py:67-119` | sync                 | Log via injected logger or `args[0]._logger`. Re-raise.     |
| `handle_errors(default_return=None, logger=None)` | `decorators.py:122-172` | sync         | Log. Swallow. Return `default_return`.                      |
| `async_log_errors`                     | `decorators.py:175-211` | async                | Log. Re-raise. (Bare decorator — no parameters.)            |
| `async_handle_errors(default_return=None)` | `decorators.py:214-273` | async             | Log. Swallow. Return `default_return`. **`RequestException` always propagates** so callers can still catch plugin-call errors. |
| `gen_log_errors(logger=None)`          | `decorators.py:276-337` | sync generator       | Log. Re-raise.                                              |
| `gen_handle_errors(default_return=None, logger=None)` | `decorators.py:340-401` | sync generator | Log. Stop the generator (does NOT yield default).         |
| `async_gen_log_errors(logger=None)`    | `decorators.py:404-464` | async generator      | Log. Re-raise.                                              |
| `async_gen_handle_errors(default_return=None, logger=None)` | `decorators.py:467-533` | async gen | Log. Stop the generator.                                |

All decorators run a `_check_type` check (`decorators.py:9-64`) up front, so applying e.g. `@log_errors` to an `async def` raises `PluginTypeMissmatchError` with a hint to use `@async_log_errors` instead.

`log_errors`, `handle_errors`, `async_handle_errors`, `gen_log_errors`, and `async_gen_log_errors` accept the no-parens form (`@log_errors` works) — they detect the callable-instead-of-logger argument and rewrap. `async_log_errors` is bare by design (`def async_log_errors(func)`, no parameters at all).

**When to use which**

- `*_log_errors` is the standard for endpoints: log and re-raise so the caller's `RequestException` handler sees the failure.
- `*_handle_errors(default_return=...)` is for fire-and-forget code where exceptions must not bubble — background tasks, optional callbacks. Note `async_handle_errors` still propagates `RequestException` so plugin-call failures are not silently swallowed.

---

## The `Event` object

Source: `utils.py:1754-1807`.

Subscriber handlers receive ONE positional argument: an `Event`. Endpoints called via `execute()` are NOT wrapped — they get whatever the caller passed, unpacked per the [argument-shape contract](#argument-shape-contract-canonical).

| Field             | Type    | Description                                                                                            |
|-------------------|---------|--------------------------------------------------------------------------------------------------------|
| `topic`           | `str`   | The literal topic that fired (post-resolution).                                                        |
| `payload`         | `Any`   | Whatever the publisher passed as `payload`.                                                            |
| `author`          | `str`   | Publisher's `plugin_name` (or `"system"` for framework-originated calls).                              |
| `author_id`       | `str`   | Publisher's `plugin_uuid`.                                                                             |
| `author_host`     | `str`   | Publisher's hostname.                                                                                  |
| `subscription_id` | `str`   | `declared_id` for YAML subs, `sub_uuid` for runtime subs.                                              |
| `timestamp`       | `float` | Epoch seconds at publish time.                                                                         |

---

## Exceptions

Source: `exceptions.py`.

| Class                     | Base               | Raised when                                                                                                                                              |
|---------------------------|--------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------|
| `ConfigException`         | `Exception`        | `ConfigUtil.check_config_integrity` finds a missing required section.                                                                                    |
| `RequestException`        | `Exception`        | Any plugin-call failure: endpoint not found, target not ready, method-shape mismatch, target raised, circular sync call, no event subscriber matches, event disabled, framework-not-started guard. |
| `NetworkRequestException` | `RequestException` | Network-level failure during a remote dispatch (connection error, peer error, timeout).                                                                  |
| `NoLocalSubException`     | `RequestException` | Peer signals "no local sub matched" on a remote `request_event` / `request_event_stream`. Distinct subclass so request-event fall-through preserves order. |
| `NodeException`           | `Exception`        | Generic node-level error (e.g. unknown / disabled node).                                                                                                 |
| `PluginTypeMissmatchError`| `Exception`        | A `decorators.py` decorator is applied to a function whose sync/async/gen/async-gen kind does not match.                                                 |

In practice, catch `RequestException` — it covers `execute*`, `request_event*`, and their network counterparts.

---

## Argument-shape contract (canonical)

From `_call_endpoint` (`PluginCore.py:2838-2868`). Restated here for skim-readers:

| `args=` value | Endpoint receives                  |
|---------------|------------------------------------|
| `tuple`       | `func(*args)` (unpacked positional) |
| `dict`        | `func(**args)` (unpacked keyword)   |
| `None`        | `func()` (no arguments)             |
| anything else | `func(args)` (single positional)    |

Subscriber handlers (publish_event / request_event) bypass this rule: they always receive a single `Event` object regardless of the publisher's `payload` shape.

---

## PluginCore methods (for tooling, harnesses, CLI authors)

The methods below are on `PluginCore` itself. Plugin authors use the `Plugin` wrappers above; tooling that drives the framework from outside uses these. All are on `pc: PluginCore`.

### Lifecycle

| Method | Source | Purpose |
|--------|--------|---------|
| `PluginCore(config_path: str)` | `PluginCore.py:526` | Constructor. Loads config. Does NOT load plugins or start networking. |
| `await pc.start()` | `PluginCore.py:674-711` | Initialise background tasks, load plugins, start networking. |
| `await pc.wait_until_ready()` | `PluginCore.py:624-672` | Idempotent variant — for callers that want lazy init. |
| `await pc.close()` | `PluginCore.py:713-827` | Graceful shutdown. |
| `await pc.graceful_shutdown()` | `PluginCore.py:1670-1675` | Alias for `close()`. |

### Config

| Method | Source | Purpose |
|--------|--------|---------|
| `pc.load_config_yaml(path)` | `PluginCore.py:829-848` | Re-read, validate, re-apply (sync). |
| `await pc.async_load_config_yaml(path)` | `PluginCore.py:850-852` | Async wrapper. |
| `pc.list_config_files() -> Dict[str, str]` | `PluginCore.py:894-907` | Paths to main + per-plugin configs. |
| `pc.read_config_file(path) -> str` | `PluginCore.py:909-929` | Read a known config file. |
| `pc.save_config_file(path, content, backup=True)` | `PluginCore.py:931-965` | Validate YAML, save. Does NOT auto-reload. |
| `pc.is_main_config(path) -> bool` | `PluginCore.py:967-969` | |

### Plugin management

| Method | Purpose |
|--------|---------|
| `await pc.load_plugins()` | Load and enable every configured plugin. |
| `await pc.get_plugins()` | Load (without enabling). |
| `await pc.start_plugins()` | Enable all loaded plugins concurrently. |
| `await pc.load_plugin_with_conf(entry)` | Load one plugin from a config dict. |
| `await pc.enable_plugin(plugin_name)` | Public-facing enable; transitions `INACTIVE → ENABLING → ENABLED`. UNLOADED / FAILED_LOAD plugins silently no-op — call `_reload_plugin(name)` first to (re-)instantiate. (v0.26.0) |
| `await pc.disable_plugin(plugin_name)` | Public-facing disable; transitions `ENABLED` → `DISABLING` → `INACTIVE`. (v0.26.0) |
| `await pc.pop_plugin(plugin_name)` | Disable, remove, unsubscribe. State becomes `UNLOADED` if config still references the plugin, else entry removed from `plugin_states`. |
| `await pc.purge_plugins()` | Pop all. |
| `await pc.purge_plugins_except(excluded_names)` | Pop all except listed. |
| `await pc._reload_plugin(plugin_name)` | Hot-swap entry point. State sequence: `ENABLED → DISABLING → INACTIVE → UNLOADED → INACTIVE → ENABLING → ENABLED` (or shorter for non-enabled source). |
| `await pc.get_unloaded_metadata(name) -> Optional[dict]` | Read on-disk `plugin_config.yml` for an UNLOADED plugin. Returns `None` for any other state. (v0.26.0) |

### Introspection

| Method | Source | Purpose |
|--------|--------|---------|
| `await pc.get_plugin_info(plugin_name) -> Optional[dict]` | `PluginCore.py:1610-1626` | name/version/uuid/enabled/remote/description/arguments. |
| `await pc.get_plugin_endpoints(plugin_name) -> Optional[List[dict]]` | `PluginCore.py:1628-1655` | Per-endpoint metadata. |
| `await pc.list_plugins_state() -> List[dict]` | `PluginCore.py:1657-1668` | name/enabled/description for every plugin. |
| `await pc.find_endpoint(access_name, hosts, blocked_hosts, plugin_uuid, requester_id, target_plugin)` | `PluginCore.py:2468-2649` | Endpoint lookup with access control. Returns `(plugin, endpoint, node)` or `(None, None, None)`. |
| `await pc.find_endpoints_by_tag(tag) -> Optional[List]` | `PluginCore.py:2431-2466` | Tag-based discovery (local + remote). |

### Events and subscriptions (low-level)

| Method | Source | Purpose |
|--------|--------|---------|
| `await pc.publish_event(publisher, event_id, ...)` | `PluginCore.py:4060-4279` | Underlying publish path. |
| `pc.publish_event_sync(...)` | `PluginCore.py:4409-4437` | Sync. |
| `await pc.request_event(publisher, event_id, ...)` | `PluginCore.py:4440-4629` | Underlying request path. |
| `pc.request_event_sync(...)` | as above | Sync. |
| `await pc.request_event_stream(publisher, event_id, ...)` | `PluginCore.py:4655-4954` | Streaming request path. |
| `await pc.subscribe_event(topic, plugin_name, plugin_uuid, target_access_name, ...)` | `PluginCore.py:5023-5122` | Runtime sub registration with full validation and delta broadcast. |
| `await pc.unsubscribe_event(sub_uuid) -> bool` | `PluginCore.py:5124-5157` | With remove-delta broadcast. |

### Read-mostly attributes

| Attribute | Description |
|-----------|-------------|
| `pc.plugins` | `dict[name, Plugin]` — only plugins with a live instance (NOT including UNLOADED / FAILED_LOAD entries). |
| `pc.plugins_by_uuid` | `dict[uuid, Plugin]` |
| `pc.plugin_states` | `dict[name, PluginState]` — superset of `pc.plugins` keys; includes UNLOADED / FAILED_LOAD entries. v0.26.0. **Iteration contract:** snapshot via `dict(pc.plugin_states)` before iterating; concurrent `pop_plugin` may `del` entries. Single-key lookup via `.get(name)` / `[name]` is GIL-atomic and safe. |
| `pc.hostname` | This node's hostname. |
| `pc.network` | `NetworkManager` or `None`. |
| `pc.networking_enabled` | `bool` |
| `pc.topic_registry` | The `TopicRegistry` instance. |
| `pc.sync_dispatcher` | The `SyncDispatcher` instance. |
| `pc.requests` | In-flight + recently-collected request map. |
| `pc.main_event_loop` | The bound asyncio loop. |

---

## Quick reference card

```python
# Direct call
result = await self.execute("Plugin", "method", args=value)
result = await self.execute("Plugin", "method", args=(a, b))         # *args
result = await self.execute("Plugin", "method", args={"x": 1})       # **kwargs

# Streaming call
async for chunk in self.execute_stream("Plugin", "method", args=...):
    ...

# Publish (fire-and-forget)
n = await self.publish_event("event_id", payload=data)

# Request (1:1)
result = await self.request_event("event_id", payload=data, timeout=5.0)

# Streaming request
async for chunk in self.request_event_stream("event_id", payload=data):
    ...

# Runtime subscription
sub_uuid = await self.subscribe("messages/*", target_access_name="handler")
await self.unsubscribe(sub_uuid)
```
