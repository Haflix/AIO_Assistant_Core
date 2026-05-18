# Plugin Authoring Guide

*Last updated for Plexus 0.40.0*

Write a plugin from scratch. This page walks through the moving parts in
the order an author meets them; reference details live in
[api reference](./api_reference.md), [notifier](./notifier.md), and
[configuration](./configuration.md).

The shipped `copypasta/AveragePlugin/` is the running example.

---

## Tier discipline (read this first)

Plexus organises plugins into three tiers. Stay inside one
tier per plugin — mixing them produces tangled dependencies that hot-swap
cannot rescue.

- **Tier 1 — Base plugins.** One protocol, one service, one device.
  DiscordBot client, an LLM adapter, a Postgres pool. No cross-plugin
  calls, no business logic.
- **Tier 2 — Extensions.** Wire two or more base plugins together with
  pure routing/translation. Example: a plugin that listens to Discord
  message events and writes them to Postgres.
- **Tier 3 — Orchestrators.** Business logic, AI decisions, workflow.
  Calls bases and extensions; never touches hardware or raw APIs
  directly.

If you cannot tell which tier you belong to, the design is not ready
yet. See [architecture](./architecture.md) for the full treatment.

---

## What a plugin is

A plugin is a Python class subclassing `utils.Plugin`, plus a
`plugin_config.yml` next to it. The folder layout is:

```
my_plugin/
    plugin.py                # class MyPlugin(Plugin): ...
    plugin_config.yml        # description, version, endpoints, events, ...
```

Register it in the top-level `config.yml`:

```yaml
plugins:
  - name: MyPlugin
    enabled: true
    path: ./plugins/my_plugin
```

If `path` is omitted, Plexus resolves it as
`{general.plugin_package}/{name}` — so a plugin named `MyPlugin` with
`general.plugin_package: plugins` auto-resolves to `plugins/MyPlugin/`.

You can load the same class twice under different names by adding two
entries with different `name` values pointing at the same `path`. The
two instances get independent `plugin_uuid` values; targeting one
specifically uses the `plugin_uuid` argument on `execute()`.

---

## The class skeleton

Every plugin must implement `on_load`, `on_enable`, and `on_disable`.
`Plugin.__init__` is `@final` — do not override it.

```python
from plexus.utils import Plugin
from plexus.decorators import log_errors, async_log_errors


class MyPlugin(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        # Sync only. Declare instance variables here.
        # Reads self.arguments via *args / **kwargs unpacking.
        self.connection = None
        self.task = None

    @async_log_errors
    async def on_enable(self):
        # Open connections, start tasks, register listeners.
        self.connection = await connect_somewhere()
        self.task = asyncio.create_task(self._loop())

    @async_log_errors
    async def on_disable(self):
        # Undo on_enable. The framework auto-unregisters declared subs
        # AFTER this returns - do not unsubscribe them here.
        if self.task is not None:
            self.task.cancel()
        if self.connection is not None:
            await self.connection.close()

    async def _loop(self):
        ...
```

Initialise everything to `None` (or a sentinel) in `on_load` and test
against that sentinel in `on_disable`. That gives `on_disable` something
to check if `on_enable` failed partway through.

### Hot-swap and `on_disable`

Hot-swap calls `on_disable` then re-creates the plugin from scratch —
new object, new `plugin_uuid`, fresh `on_load`. State that needs to
survive a reload must be persisted to a database or another plugin.
`on_disable` must release every resource opened by `on_enable`. The
default disable timeout is 30 seconds; tune it via
`general.plugin_disable_timeout`.

### What `arguments` looks like

The `arguments:` field of `plugin_config.yml` is forwarded to `on_load`
with this unpacking rule:

| `arguments:` value | `on_load` call |
|---|---|
| `[1, 2]` (list/tuple) | `on_load(1, 2)` |
| `{"x": 1, "y": 2}` (dict) | `on_load(x=1, y=2)` |
| `null` / missing | `on_load()` |
| anything else | `on_load()` (value dropped) |

Use `**kwargs` if you want robust handling regardless of YAML shape:

```python
def on_load(self, *args, **kwargs):
    self.host = kwargs.get("host", "localhost")
```

Outside `on_load`, the merged value is also available as `self.arguments`.

---

## `plugin_config.yml` schema

Top-level fields:

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `description` | str | recommended | `"UNKNOWN"` | Human-readable. |
| `version` | str | recommended | `"0.0.0 - not given"` | Bump on every change to the plugin. |
| `remote` | bool | recommended | `false` | Plugin-level remote flag. Required *together with* the per-endpoint `remote` flag for a peer to reach an endpoint. |
| `arguments` | dict / null | recommended | `null` | Forwarded to `on_load`. Must be dict-or-null at the YAML level. |
| `endpoints` | dict keyed by access_name | recommended | `{}` | See below. List form is rejected. |
| `events` | dict keyed by event_id | optional | `{}` | Events this plugin publishes. See [notifier](./notifier.md). |
| `subscriptions` | dict keyed by declared_id | optional | `{}` | Topics this plugin listens for. See below. |
| `prefix` | str | optional | plugin name | Resolved value used to substitute `{prefix}` in event/subscription topic templates. |
| `verbose_notifier` | bool | optional | `false` | When true, dispatch logs include match counts. |

### `endpoints:` entry fields

```yaml
endpoints:
  my_method:                          # access_name (the dict key)
    internal_name: my_method          # actual class method (defaults to access_name)
    remote: false                     # endpoint-level remote flag
    accessible_by_other_plugins: true # local access gate
    description: "Does the thing."
    tags: ["math", "stateless"]       # for find_endpoints_by_tag
    arguments:                        # metadata only - not validated
      - name: value
        type: int
        description: "What to operate on"
```

Field details:

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `<key>` (access_name) | identifier | YES | — | Must be a valid Python identifier and not in `{"system", "general", "any", "remote", "local"}`. |
| `internal_name` | str | optional | access_name | The real method name on the class. Use it when the public name differs from the implementation name. Must be non-empty ASCII. |
| `remote` | bool | YES | — | Required even when `plugin.remote: true`. Both flags must be true for a peer to reach the endpoint. |
| `accessible_by_other_plugins` | bool | YES | — | When `false`, only the plugin itself (matched by `plugin_uuid`) can call. |
| `description` | str | optional | `""` | Surfaced via `Plexus.get_plugin_endpoints`. |
| `tags` | list[str] | optional | `[]` | Used by `find_endpoints_by_tag` for tag-based discovery. |
| `arguments` | list[dict] | optional | absent | Pure metadata for introspection (CLI, AI tool schema builders). The framework does not validate the shape of individual arg specs. |

### `events:` entry fields

A publisher must declare the events it intends to publish. Each entry
maps an `event_id` (used at call time) to a topic and optional
publisher-side default filters.

```yaml
events:
  message_received:                   # event_id (the dict key)
    topic: "{prefix}/messages/incoming"
    hosts: "any"                      # default publish-side host filter
    blocked_hosts: null
    enabled: true                     # disable to silence the publisher temporarily
```

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `<key>` (event_id) | identifier | YES | — | How your code refers to the event in `publish_event(event_id, ...)`. |
| `topic` | str | YES | — | The topic to publish under. `{prefix}`, `{plugin_name}`, `{hostname}`, and `{plugin_uuid}` are resolved at load time; user `{var}` placeholders stay intact and are filled at runtime via `topic_vars`. Wildcards (`*`) are NOT allowed in event topics. |
| `hosts` | str / list / null | optional | `null` | Default publisher-side hosts filter. Same shape as a sub's `hosts`. |
| `blocked_hosts` | str / list / null | optional | `null` | Default publisher-side blocked hosts. |
| `enabled` | bool | optional | `true` | When false, `publish_event` silently drops; `request_event` raises. |

### `subscriptions:` entry fields

Each subscription binds a topic pattern to a target endpoint on this
plugin (or, for orchestrator routing, on another plugin). When an event
fires that matches the topic and survives the filter chain, the framework
delivers an `Event` object to that endpoint via `execute()`.

```yaml
subscriptions:
  on_message:                         # declared_id (the dict key)
    topic: "messages/*"
    target_access_name: handle_message
    hosts: "any"
    blocked_hosts: null
    authors: null                     # whitelist of publisher plugin_names
    blocked_authors: null
    enabled: true
```

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `<key>` (declared_id) | identifier | YES | — | Becomes `Event.subscription_id` when the handler runs. |
| `topic` | str | YES | — | Topic pattern. `*` matches one segment (e.g. `sensor/*/temperature`). `{prefix}`, `{plugin_name}`, `{hostname}`, `{plugin_uuid}` are substituted at load. User `{var}` placeholders are REJECTED on subscription topics — use a wildcard. |
| `target_access_name` | str | YES | — | Endpoint that receives the dispatched `Event`. |
| `target_plugin` | str | optional | this plugin's name | For cross-plugin orchestrator subs. |
| `target_plugin_uuid` | str / null | optional | `null` | Pin to a specific instance. |
| `hosts` | str / list / null | optional | `"any"` | Receiver-side host filter. |
| `blocked_hosts` | str / list / null | optional | `null` | Receiver-side host filter. |
| `authors` | str / list / null | optional | `null` | Whitelist of publisher plugin_names. |
| `blocked_authors` | str / list / null | optional | `null` | Blacklist. The pseudo-author `"system"` (used for framework-originated calls) is whitelisted by default unless explicitly named here. |
| `enabled` | bool | optional | `true` | Disabled subs stay registered (visible to introspection) but are skipped at match time. |

Topic syntax, filter semantics, and `topic_vars` are documented in detail
in [notifier](./notifier.md).

---

## Walkthrough — building AveragePlugin from scratch

### Step 1 — minimal class

```python
# copypasta/AveragePlugin/plugin.py
from plexus.utils import Plugin
from plexus.decorators import log_errors, async_log_errors


class AveragePlugin(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        self.state = {}

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("AveragePlugin enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("AveragePlugin disabled")
```

```yaml
# copypasta/AveragePlugin/plugin_config.yml
description: Example plugin
version: 1.0.0
remote: false
arguments:

endpoints: {}
```

Add to `config.yml` and run. Plexus loads, enables, disables on
shutdown. Nothing exposed yet.

### Step 2 — expose an endpoint

Add a method:

```python
@async_log_errors
async def example_method(self, value):
    return value * 2
```

Declare it in `plugin_config.yml`:

```yaml
endpoints:
  example_method:
    internal_name: example_method
    remote: false
    accessible_by_other_plugins: true
    description: Doubles the input value.
    arguments:
      - name: value
        type: any
        description: Input value to process
```

From any other plugin:

```python
result = await self.execute("AveragePlugin", "example_method", args=21)
# result == 42
```

The single-value `args=21` is shorthand: a non-tuple, non-dict, non-None
value is forwarded as a single positional argument. Pass `args=(a, b)`
for positional unpacking, `args={"value": 21}` for keyword unpacking.

To pass a single dict as one positional argument, wrap it:
`args=(my_dict,)` — otherwise the dict is unpacked as keyword arguments.

### Step 3 — call another plugin

```python
@async_handle_errors(default_return=None)
async def call_other_plugin(self, plugin_name, method_name, args):
    return await self.execute(plugin_name, method_name, args, hosts="any")
```

`hosts="any"` accepts both local and remote. Use `"local"` to force
this-node-only, `"remote"` to force any other node, or pass a hostname /
list of hostnames.

`@async_handle_errors(default_return=None)` swallows non-`RequestException`
errors and returns `None`. `RequestException` (and its subclasses
`NetworkRequestException`, `NoLocalSubException`) always propagates so
callers can react to legitimate plugin/network failures.

### Step 4 — stream results

```python
@async_gen_log_errors
async def example_stream(self, count):
    for i in range(count):
        await asyncio.sleep(0.1)
        yield f"Item {i + 1} of {count}"
```

```yaml
example_stream:
    internal_name: example_stream
    remote: true
    accessible_by_other_plugins: true
    description: Yields sequential items.
    arguments:
      - name: count
        type: int
        description: Number of items
```

Caller:

```python
async for chunk in self.execute_stream("AveragePlugin", "example_stream", args=5):
    print(chunk)
```

### Step 5 — subscribe to a topic

A topic-subscribed endpoint receives a single `Event` object — never raw
args. The `Event` exposes `topic`, `payload`, `author`, `author_id`,
`author_host`, `subscription_id`, and `timestamp`. Add the handler:

```python
@async_log_errors
async def handle_event(self, event):
    self._logger.info(f"Got event: {event.payload} from {event.author}")
    return {"received": event.payload, "handled_by": self.plugin_name}
```

Declare both the endpoint AND the subscription:

```yaml
subscriptions:
  handle_event_sub:
    topic: "example/event"
    target_access_name: handle_event
    hosts: "local"

endpoints:
  handle_event:
    internal_name: handle_event
    remote: true
    accessible_by_other_plugins: true
    description: Receives events on "example/event".
    arguments:
      - name: event
        type: any
        description: Event object received from the topic
```

Now any plugin that publishes an event whose declared `topic` matches
`example/event` will dispatch through `handle_event`.

A note on the flags here: the subscription's `hosts: "local"` restricts
this sub to events published on this node — so the endpoint's
`remote: true` flag is moot for sub-driven traffic. The endpoint's
`remote` flag still matters for direct `execute()` calls from peer
plugins.

### Step 6 — publish events

A publisher must declare the events it emits. In the publisher's
`plugin_config.yml`:

```yaml
events:
  greet:
    topic: "example/event"
```

In code:

```python
# Fire-and-forget. Returns the count of subs the dispatch was scheduled for.
n = await self.publish_event("greet", payload={"hello": "world"})

# Ask-by-topic. Returns the first matching handler's result.
reply = await self.request_event("greet", payload={"hello": "world"})
```

`request_event` raises `RequestException` if no subscriber matches.

If your event topic contains user `{var}` placeholders that survive
load time, fill them at publish time with `topic_vars`:

```python
await self.publish_event(
    "per_user_msg",
    payload={"text": "hi"},
    topic_vars={"user_id": "u_42"},
)
```

`topic_vars` constraints:

- Reserved keys (`prefix`, `plugin_name`, `hostname`, `plugin_uuid`) are
  forbidden — those are load-time only.
- Values cannot contain `/`.
- Values cannot be empty or whitespace-only (and cannot have leading or
  trailing whitespace).
- Missing keys for placeholders raise `ValueError`.
- Extra keys not used by the template log a warning.
- A static topic (no placeholders) plus non-empty `topic_vars` logs a
  warning — you probably meant `payload`.

### Step 7 — subscribe at runtime

Sometimes the subscription set is dynamic (e.g. plugin discovery). Use
`await self.subscribe(...)` from within `on_enable` (or any async
context):

```python
@async_log_errors
async def on_enable(self):
    self._sub_id = await self.subscribe(
        "messages/*",
        target_access_name="handle_message",
    )

@async_log_errors
async def on_disable(self):
    # framework auto-clears declared and runtime subs after `on_disable` returns
    pass
```

`subscribe()` returns a `sub_uuid` you can later pass to
`await self.unsubscribe(sub_uuid)`. Filters (`hosts`, `blocked_hosts`,
`authors`, `blocked_authors`) accept the same values as the YAML form.

`target_access_name` must be a non-empty string — runtime subs route to a
*declared endpoint*, never to a free-floating callable.

---

## Useful attributes inside any plugin

| Attribute | Type | Purpose |
|---|---|---|
| `self._plexus` | `Plexus` | Back-reference. Prefer the `Plugin`-level wrappers. |
| `self._logger` | `Logger` | Plugin-scoped logger. |
| `self.plugin_name` | str | Name from `config.yml` (NOT the class name). |
| `self.plugin_uuid` | str | uuid4 hex; unique per instance, regenerated on each (re)load. |
| `self.arguments` | Any | Raw merged `arguments` from `plugin_config` + overrides. |
| `self.endpoints` | dict | Parsed `endpoints:` block. |
| `self.events` | dict | Parsed `events:` block (post-load templating). |
| `self.subscriptions` | dict | Parsed `subscriptions:` block. |
| `self.prefix` | str | Resolved prefix (defaults to plugin_name). |
| `self.verbose_notifier` | bool | Verbose dispatch logging flag. |
| `self.enabled` | bool (read-only `@property` since v0.26.0) | True for state in `{ENABLING, ENABLED}`. Direct writes raise `AttributeError`. |
| `self.remote` | bool | Plugin-level remote flag. |
| `self.description` | str | From `plugin_config`. |
| `self.version` | str | From `plugin_config`. |
| `self.event_loop` | event loop | The framework's main loop. |
| `self.ready` | `asyncio.Event` | Author-controlled readiness flag (defaults SET — see below). |

### Optional: control your own readiness

Cross-plugin callers block on `self.ready` AND on the framework's internal
`_lifecycle_ready` flag, both gated by `general.plugin_ready_timeout`
(default 60 s). If your plugin needs background-task setup before it can
serve calls, do this:

```python
async def on_enable(self):
    self.ready.clear()
    self.task = asyncio.create_task(self._slow_setup())
    # ... start anything else fast ...

async def _slow_setup(self):
    await self._connect()
    await self._warmup_cache()
    self.ready.set()
```

Other plugins calling `await self.execute("MyPlugin", ...)` will block at
the readiness gate until `self.ready` is set.

`_lifecycle_ready` is framework-controlled — never touch it.

---

## Lifecycle states (v0.26.0)

Each plugin tracked by the framework follows a 6-state machine:

| State | Meaning |
|---|---|
| `UNLOADED` | Config has the entry but no instance exists. Created when `enabled: false` in config or after `pop_plugin`. |
| `INACTIVE` | Instance exists, `on_load` ran, plugin is not enabled. Default post-load and post-disable state. |
| `ENABLING` | `on_enable` in progress. |
| `ENABLED` | `on_enable` returned. Endpoints dispatchable. |
| `DISABLING` | `on_disable` in progress. |
| `FAILED_LOAD` | `on_load` raised. No instance. `last_errors[Phase.LOAD]` populated. |

Read state with `plx.plugin_states[name].state`. The state enum lives in
[`plexus/plugin_state.py`](../plexus/plugin_state.py); import as
`from plexus.plugin_state import State`.

`Plugin.enabled` is a read-only `@property` returning `True` for state in
`{ENABLING, ENABLED}`. Author code MUST NOT write `self.enabled = ...` —
the override raises `AttributeError`. Use `plx.enable_plugin(name)` /
`plx.disable_plugin(name)` to change state.

**Subclass init contract:** subclasses must call `super().__init__(...)`
BEFORE reading `self.enabled` — the property depends on
`self._plexus` being bound, which the parent `__init__` does at the
end. Reading the property earlier in subclass init returns `False` even
for an enabled plugin.

**Re-entrant lifecycle warning:** calling `plx.disable_plugin(self.plugin_name)`
from inside your own `on_load` / `on_enable` / handler body re-enters
the per-plugin lifecycle lock and **deadlocks**. If you need to
self-disable, schedule it on a separate task:
```python
asyncio.create_task(self._plexus.disable_plugin(self.plugin_name))
```

### Observers of `_core/plugin/state_changed`

Internal event-bus observers (registered via `plx.internal_observe(...)`)
receive a synchronous callback for every state transition with payload
`(name, from_state, to_state, ts)` (state strings are the enum
`.value`). Observer contract:

- Sync only — observers MUST return in `<1ms`. Heavy work goes to
  `asyncio.create_task(...)`.
- Observers MUST NOT acquire `plugin_lock` / `lifecycle_lock` /
  `request_lock` — the dispatch happens inside one of these locks; recursive
  acquisition deadlocks.
- Observers MUST NOT call `plx.enable_plugin(name)` /
  `plx.disable_plugin(name)` for the SAME plugin whose state just changed —
  same lock-recursion deadlock. For a DIFFERENT plugin, defer with
  `asyncio.create_task(...)`.

### Observable transition sequences

Some operations emit multiple state-change events in rapid succession:

| Operation | Sequence |
|---|---|
| `plx.disable_plugin(name)` on ENABLED | `ENABLED → DISABLING → INACTIVE` |
| `plx.pop_plugin(name)` on ENABLED | `ENABLED → DISABLING → INACTIVE → UNLOADED` |
| `plx.pop_plugin(name)` on INACTIVE | `INACTIVE → UNLOADED` |
| `plx._reload_plugin(name)` on ENABLED | `ENABLED → DISABLING → INACTIVE → UNLOADED → INACTIVE → ENABLING → ENABLED` |
| `plx.enable_plugin(name)` on UNLOADED | `UNLOADED → INACTIVE → ENABLING → ENABLED` |

Observers reacting to INACTIVE alone may take action assuming the plugin
is just disabled (re-enableable) and then immediately see UNLOADED.
Treat the sequence as a whole, not individual events.

---

## Decorators

Eight decorators in `decorators.py`, four sync and four async, each with a
`*_log_errors` ("log + re-raise") and a `*_handle_errors` ("log + swallow,
return default") variant.

| Function kind | Log + re-raise | Log + swallow |
|---|---|---|
| sync `def` | `@log_errors` | `@handle_errors(default_return=...)` |
| `async def` | `@async_log_errors` | `@async_handle_errors(default_return=...)` |
| sync generator | `@gen_log_errors` | `@gen_handle_errors(default_return=...)` |
| async generator | `@async_gen_log_errors` | `@async_gen_handle_errors(default_return=...)` |

Apply the variant that matches your function kind — applying
`@log_errors` to an `async def` raises `PluginTypeMissmatchError` at
import time.

`@async_handle_errors` is special: it lets `RequestException` (and its
subclasses) propagate so callers can react to plugin/network failures
directly. Other exceptions are logged and replaced with `default_return`.

For most endpoint methods on a plugin: `@async_log_errors`.

---

## Testing your plugin

The repo ships an in-process test framework under `plugins_test/` —
`TestRunner` orchestrates the suites; individual `Test*Suite` plugins
exercise specific subsystems. See `plugins_test/test_suite_plan.md` for
the full layout.

For your own work:

- Bump `version` in `plugin_config.yml` on every change.
- Use the project's `test_config.yml` (or your own copy) so tests
  never run against production configuration.

---

## Common pitfalls

- **`async def on_load`.** Not allowed. `on_load` runs synchronously.
- **Calling `execute_sync` from `on_load`.** Raises
  `RequestException("Framework not started")` — the event loop is not
  running yet.
- **Forgetting both `remote` flags.** Plugin-level `remote: true` is not
  enough; each endpoint also needs its own `remote: true` to be reachable
  by peers.
- **Touching `self._lifecycle_ready`.** It is framework-only. Use
  `self.ready` for author-controlled readiness.
- **Manually unsubscribing in `on_disable`.** The framework auto-clears
  declared and runtime subs after `on_disable` returns. Doing it
  manually is harmless but redundant.
- **List-form `endpoints:`.** Rejected at load. Use the dict form keyed
  by `access_name`.
- **Passing a dict where you meant one positional.** `args=my_dict`
  unpacks as `**kwargs`. Wrap it: `args=(my_dict,)`.

---

## Things to leave out

The framework has shed several legacy patterns. None of these work any
more — do NOT use them in new code:

- **No `topic:` field on an endpoint.** The auto-subscribing endpoint
  field is gone. Subscriptions go in the dedicated `subscriptions:`
  block.
- **No `handler=` kwarg on `self.subscribe(...)`.** Runtime subs route
  to a NAMED endpoint via `target_access_name`. If you need a private
  handler, declare it as an endpoint with
  `accessible_by_other_plugins: false`.
- **No list-form `endpoints:`.** Rejected at load. Use the dict form
  keyed by `access_name`.
- **No `_lifecycle_ready` poking.** Framework-controlled. Use
  `self.ready` for author-controlled readiness.
- **No `async def on_load`.** `on_load` is sync only and runs inside
  `__init__`. Async setup belongs in `on_enable`.
- **No `Plugin.__init__` override.** It is `@final`.

---

## Full example: AveragePlugin assembled

A single copy-paste reference combining all seven steps.

`copypasta/AveragePlugin/plugin.py`:

```python
from plexus.utils import Plugin
from plexus.decorators import (
    log_errors,
    async_log_errors,
    async_handle_errors,
    async_gen_log_errors,
)
import asyncio


class AveragePlugin(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        self._logger.debug("AveragePlugin loaded")
        self.state = {}

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("AveragePlugin enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("AveragePlugin disabled")

    @async_log_errors
    async def example_method(self, value):
        self._logger.info(f"Processing value: {value}")
        return value * 2

    @async_handle_errors(default_return=None)
    async def call_other_plugin(self, plugin_name, method_name, args):
        return await self.execute(plugin_name, method_name, args, hosts="any")

    @async_log_errors
    async def handle_event(self, event):
        self._logger.info(f"Received event payload: {event.payload}")
        return {"received": event.payload, "handled_by": self.plugin_name}

    @async_gen_log_errors
    async def example_stream(self, count):
        for i in range(count):
            await asyncio.sleep(0.1)
            yield f"Item {i + 1} of {count}"
```

`copypasta/AveragePlugin/plugin_config.yml`:

```yaml
description: Example plugin demonstrating the Plexus plugin structure
version: 1.2.0
remote: True
arguments:

subscriptions:
  handle_event_sub:
    topic: "example/event"
    target_access_name: handle_event
    hosts: "local"

endpoints:
  example_method:
    remote: True
    accessible_by_other_plugins: True
    description: Doubles the input value

  call_other_plugin:
    remote: True
    accessible_by_other_plugins: True
    description: Forwards a call to another plugin

  example_stream:
    remote: True
    accessible_by_other_plugins: True
    description: Yields a sequence of strings

  handle_event:
    remote: True
    accessible_by_other_plugins: True
    description: Receives Events from the example/event topic
```

---

## Where to go next

- [api reference](./api_reference.md) — every method on `Plugin`.
- [notifier](./notifier.md) — events, topics, filtering, `topic_vars`.
- [networking](./networking.md) — make your plugin reachable from peers.
- [configuration](./configuration.md) — `config.yml` reference and
  per-plugin `overrides:`.
- [architecture](./architecture.md) — three-tier discipline and the
  full system shape.
