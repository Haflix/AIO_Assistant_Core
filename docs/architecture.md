# Architecture

*Last updated for AIO Assistant Core 0.22.0*

This document describes the runtime shape of an AIO Assistant Core process: how PluginCore loads plugins, how the lifecycle hooks fire, what guarantees the framework gives during hot-swap and shutdown, and how the three-tier discipline organises the plugins themselves.

---

## The big picture

```
                              +-----------------------+
                              |     config.yml        |
                              |  plugins / general /  |
                              |       networking      |
                              +-----------+-----------+
                                          |
                                          v
+---------------+   start()    +------------------------+   peers/mTLS    +----------------+
|   process     +------------->|       PluginCore       |<--------------->| NetworkManager |
|  (asyncio     |              |                        |                 |  (peer node)   |
|   loop)       |<-------------+ plugins  events  subs  +---------------->|                |
+---------------+   close()    +-----+--------+--------++                 +----------------+
                                     |        |         |
                                     v        v         v
                                  Plugin   Plugin    Plugin
                                  (Base)  (Extension) (Orchestrator)
```

PluginCore is the single piece of the framework you talk to. It owns:

- The asyncio event loop binding (`main_event_loop`).
- A registry of every loaded plugin, by name (`plugins`) and by uuid (`plugins_by_uuid`).
- The `TopicRegistry` (insertion-ordered subscription store) — every YAML-declared subscription and every runtime subscription lives here, keyed by `sub_uuid`.
- A `SyncDispatcher` thread pool for synchronous subscriber handlers (default 4 workers; see [configuration](./configuration.md)).
- A general-purpose plugin executor for synchronous plugin endpoints.
- Optionally, a `NetworkManager` that bridges calls to peer nodes over mTLS.

Plugins themselves are subclasses of `utils.Plugin`. They never construct PluginCore — they receive a back-reference at load time as `self._plugin_core` and rely on the wrapper methods on the `Plugin` base class for everything they do.

---

## Plugin layout

A plugin is a directory containing exactly two files:

```
MyPlugin/
    plugin.py             # one class subclassing utils.Plugin
    plugin_config.yml     # description, version, endpoints, events, subscriptions
```

The directory name is conventional — what binds the plugin into the running process is the entry in `config.yml`:

```yaml
plugins:
  - name: MyPlugin                     # required
    enabled: true                      # required
    path: ./plugins/MyPlugin           # optional (auto-resolves to {plugin_package}/{name})
    overrides:                         # optional, deep-merged into plugin_config.yml
      version: "1.4.0-local"
```

A single class can be loaded multiple times under different `name` values — useful for running two Discord bots simultaneously, or two LLM adapters with different model configs. Each instance gets its own `plugin_uuid` and its own slot in `PluginCore.plugins`.

---

## Lifecycle

`Plugin.__init__` is `@final` — subclasses must not override it. State goes in `on_load`, not `__init__`. All three lifecycle hooks (`on_load`, `on_enable`, `on_disable`) are abstract on `utils.Plugin`; subclasses must implement them.

Every plugin moves through three hooks. The framework pre-initialises a fixed set of attributes before any user code runs (see [api reference](./api_reference.md) for the full list).

### `on_load(self, *args, **kwargs)` — synchronous

Called inside `Plugin.__init__`, immediately after the framework attributes are set up. The arguments come from the manifest's `arguments:` field, unpacked by shape:

| `arguments:` value | Call site                |
|--------------------|--------------------------|
| list / tuple       | `on_load(*args)`         |
| dict               | `on_load(**kwargs)`      |
| anything else      | `on_load()` (no args)    |

Constraints:

- Must be `def`, not `async def`. The framework does not await it.
- No event-loop access. The loop may not be running yet when `on_load` fires.
- No external connections. Open those in `on_enable`.
- No `*_sync` calls. `execute_sync`, `publish_event_sync`, and `request_event_sync` will raise `RequestException("Framework not started — sync APIs require running event loop")` until the main event loop is bound.
- Use it to declare instance variables (empty containers, defaults).

After `on_load` returns, PluginCore overwrites `plugin_name`, `version`, `remote`, `description`, `arguments`, `prefix`, `verbose_notifier`, `endpoints`, `events`, and `subscriptions` from the merged manifest plus `overrides:` block. So `on_load` sees framework defaults; everything outside `on_load` sees the real values.

### `on_enable(self)` — async or sync

Called once the plugin is registered. May be `async def` or plain `def`; PluginCore branches on `asyncio.iscoroutinefunction`. Sync versions run on the framework's plugin executor.

Order of operations inside `_enable_plugin_under_lock`:

1. The plugin's per-name lifecycle lock is acquired.
2. The YAML `subscriptions:` block is registered with the `TopicRegistry` BEFORE `on_enable` runs. This means published events can already match the plugin's subscriptions while it is still mid-startup — the readiness gate (see below) is what blocks dispatch from completing.
3. The framework flips `enabled = True`.
4. Subscription add-deltas are broadcast to peers (no-op when networking is disabled or the manager is not ready).
5. `on_enable` is called.
6. On success, the framework sets `_lifecycle_ready`. Cross-plugin callers waiting on the readiness gate proceed.
7. On failure (raise or cancel), `_lifecycle_ready` stays cleared, `ready` is reset, `on_disable` is called defensively, subscriptions are unregistered, and `enabled` is set back to `False`.

Use `on_enable` to:

- Open network connections, database pools, message-bus clients.
- Start background tasks via `asyncio.create_task(...)` — keep references so you can cancel them in `on_disable`.
- Register external listeners (Discord bot connect, Telegram poll, websocket).

Whatever you do here must be undoable by `on_disable`. Nothing more, nothing less.

### `on_disable(self)` — async or sync

Called on shutdown, on `pop_plugin`, or on hot-swap. Must reverse exactly what `on_enable` did. The framework wraps user code in `asyncio.wait_for(timeout=plugin_disable_timeout)` (default 30s; see [configuration](./configuration.md)).

Order of operations inside `_disable_plugin_under_lock`:

1. `_lifecycle_ready` is cleared BEFORE `on_disable` runs, so any in-flight readiness gate begins to time out.
2. `on_disable` is invoked under the timeout.
3. Whether `on_disable` returns, raises, or times out, the framework guarantees:
   - Subscriptions are unregistered from the `TopicRegistry`. This sweep covers BOTH YAML-declared subs and runtime subs created via `self.subscribe(...)` — both are keyed by `plugin_uuid`. Authors only need to unsubscribe manually if they want to remove a subscription mid-lifecycle.
   - `enabled` is set to `False`.
   - The plugin's per-logger threshold overrides are cleared.

Caveat: a synchronous `on_disable` cannot be hard-interrupted; `wait_for` cancels the awaitable that wraps the worker thread, but the underlying thread keeps running until the user code returns. Framework bookkeeping still completes; only the user code keeps spinning.

### Readiness gate

While a plugin is starting up but its `on_enable` has not yet returned, cross-plugin callers that do `await self.execute("ThatPlugin", ...)` block on a readiness gate (`_wait_for_plugin_ready`). The gate waits on two events with a single budget (`general.plugin_ready_timeout`, default 60s):

- `_lifecycle_ready` — framework-controlled. Set after `on_enable` returns.
- `ready` — author-controlled. Defaults SET. Clear it at the top of `on_enable` if you have async setup work after `on_enable` returns that must complete before the plugin is ready to take calls; set it again when ready.

If both events are not set within the budget, the caller's `execute()` raises `RequestException`.

### Lifecycle in pictures

```
   load_plugin_with_conf
            |
            v
   importlib import plugin.py
            |
            v
   PluginClass.__init__  (final, framework-owned)
            |
            v
   on_load(*args, **kwargs)              <-- sync, declare state
            |
            v
   parse plugin_config.yml + overrides,
   attach plugin_name / endpoints / ...
            |
            v
   register in PluginCore.plugins[name]
            |
            v
   _enable_plugin_under_lock
        |
        | (1) acquire lifecycle_lock
        | (2) register YAML subscriptions
        | (3) flip enabled = True
        | (4) broadcast sub-add deltas to peers
        | (5) call on_enable (async or sync)
        | (6) on success: _lifecycle_ready.set()
        v
   plugin running
        |
        v
   _disable_plugin_under_lock
        |
        | (1) clear _lifecycle_ready
        | (2) await on_disable with timeout
        | (3) unregister all subs (YAML + runtime)
        | (4) flip enabled = False
        v
   plugin offline
```

---

## Hot-swap

`PluginCore._reload_plugin(plugin_name)` swaps a running plugin without restarting the process. Under the per-plugin lifecycle lock so concurrent enables on the same name cannot interleave:

```
_reload_plugin(name)
  |
  +-- snapshot was_enabled
  |
  +-- _pop_plugin_under_lock(name)
  |     |
  |     +-- fail in-flight requests targeting this plugin
  |     +-- run on_disable with timeout
  |     +-- remove from plugins / plugins_by_uuid
  |     +-- unregister subscriptions
  |     +-- clear logger-level overrides
  |
  +-- load_plugin_with_conf(entry)
  |     |
  |     +-- import plugin.py fresh
  |     +-- build a brand-new class instance (on_load runs again)
  |     +-- register in plugins / plugins_by_uuid (new plugin_uuid)
  |
  +-- if was_enabled: _enable_plugin_under_lock(name)
```

Nothing leaks across the swap. The instance attributes a plugin set in its previous `on_load` are simply gone with the old object; `__init__` runs again on a fresh instance, so `on_load` runs against framework defaults — no carry-over from the prior incarnation. Other plugins that hold the OLD `plugin_uuid` will fail on calls that pin to it; they should target by name when they want "whichever instance is current".

---

## Shutdown order

`PluginCore.close()` walks a deterministic sequence so dependents wind down before their dependencies:

1. Cancel the maintenance loop.
2. Wait up to 30 seconds for in-flight tracked tasks; cancel survivors.
3. Shut the `SyncDispatcher` down with `wait=True` and a 30-second budget. Falls back to `wait=False` on timeout.
4. **Disable plugins in REVERSE config order.** Each `on_disable` gets a 30-second cap (hardcoded for shutdown). Different plugins do not block each other — their per-plugin lifecycle locks are independent.
5. Sweep stranded plugin-source per-logger thresholds.
6. Stop `NetworkManager` if present.
7. Shut down the plugin executor with `wait=False`.

Reverse-order shutdown is deliberate: an orchestrator that depends on `Postgres` is disabled before `Postgres` is, so it has a chance to flush state cleanly.

---

## The three tiers

The framework does not enforce these — they are project policy, but they are how the codebase stays maintainable as it grows. If you cannot decide which tier a plugin belongs to, the design probably needs sharpening before any code is written.

### Tier 1 — Base plugins

Single-responsibility wrappers around one protocol, one service, one piece of hardware.

- DiscordBot — just the bot client.
- PostgreSQL — connection pool plus query helpers.
- WakeWord — detection only.
- LLM adapter — raw API calls and provider format translation. No prompt engineering, no tool routing, no conversation management.

Rules:

- Do one thing. Expose endpoints. Optionally subscribe to topics published by orchestrators (e.g. "send this Discord message").
- No `execute()` calls to other plugins.
- No business logic, no AI decisions, no workflow choices.

### Tier 2 — Extensions

Glue between two or more bases. No business logic — pure routing and translation.

Example: a `DiscordDB` extension subscribes to Discord-message events and writes them to PostgreSQL. It knows nothing about user intent or AI; it just translates one base's output into another base's input.

Rules:

- May call bases via `execute()` and may subscribe to their topics.
- No business logic. If a decision is being made, it belongs in tier 3.
- The plugin's `description:` should name the bases it depends on.

### Tier 3 — Orchestrators

Business logic, AI decisions, workflows.

- AI_Interaction — picks tools, manages conversation, calls the LLM.
- Reminder — runs scheduled tasks, evaluates conditions.
- DataCollection — orchestrates ingest pipelines.

Rules:

- Coordinates bases and extensions through `execute()` and `request_event()`.
- Does NOT touch hardware, raw APIs, or databases directly. If you find yourself importing `discord.py` from an orchestrator, the cut is wrong — push it into a base.
- Holds the workflow state and the prompt logic.

```
   Tier 3 (Orchestrators):  AI_Interaction, Reminders, DataCollection
                |     calls
                v
   Tier 2 (Extensions):     DiscordDB, MemoryRouter
                |     calls
                v
   Tier 1 (Base plugins):   DiscordBot, PostgreSQL, WakeWord, LLM, ...
                |     calls
                v
   the outside world
```

When an upward call would be tempting (a base plugin calling an orchestrator), invert it: have the orchestrator subscribe to a topic the base plugin publishes.

---

## Where state lives

Per-plugin state lives on the plugin instance — `self.something`, declared in `on_load`. PluginCore itself holds no plugin state. After a hot-swap the plugin object is gone and replaced; anything that has to survive must be persisted externally — in another plugin (e.g. a database adapter), in a database, or on disk.

Cross-plugin state — anything multiple plugins read or write — should live in a base plugin and be reached through its endpoints.

---

## Where to go next

- [plugin authoring](./plugin_authoring.md) walks you through writing a plugin step by step, using AveragePlugin as the running example.
- [api reference](./api_reference.md) is the dictionary lookup for every method on `Plugin`.
- [notifier](./notifier.md) explains the topic/event system in detail.
- [networking](./networking.md) covers multi-node setups.
- [configuration](./configuration.md) is the `config.yml` schema reference.
