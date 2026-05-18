# Configuration

*Last updated for Plexus 0.41.1*

Reference for the top-level `config.yml` — the file Plexus reads on
startup to find plugins, configure the runtime, and (when enabled) wire
up networking. The shipped `config.example.yml` is the starting point;
copy it to `config.yml` (which is gitignored) and edit.

For per-plugin manifests (`plugin_config.yml`), see
[plugin authoring](./plugin_authoring.md). For the operational side of
the cluster, see [networking](./networking.md). For framework
internals, see [architecture](./architecture.md).

---

## Top-level shape

```yaml
plugins:        # list of plugin entries
  - name: ...
    enabled: ...
    path: ...
    overrides: ...

general:        # framework-wide settings
  hostname: ...
  plugin_package: ...
  console_log_level: ...
  ...

networking:     # cluster topology
  enabled: ...
  port: ...
  peers: ...
  ...
```

All three keys are required. `ConfigUtil.check_config_integrity`
raises `ConfigException` on startup if any of the three is missing.

---

## `plugins:` (list of dicts)

Each entry describes one plugin to load. Order matters for shutdown:
plugins are disabled in **reverse config order**, so list dependencies
before their dependents.

```yaml
plugins:
  - name: PostgreSQL
    enabled: true
    path: ./plugins/PlexusPostgreSQL

  - name: AveragePlugin
    enabled: true
    path: ./copypasta/AveragePlugin
    overrides:
      version: "1.1.0+local"
      verbose_notifier: true
```

### Per-entry fields

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `name` | identifier | YES | — | Validated by `_validate_identifier_name`. Must be a valid Python identifier. Reserved names rejected: `system`, `general`, `any`, `remote`, `local`. |
| `enabled` | bool | YES | — | If `false`, the entry is parsed and immediately popped — the plugin is never loaded. |
| `path` | str | optional | `{plugin_package}/{name}` | If absent, auto-resolved from `general.plugin_package`. |
| `overrides` | dict / null | optional | `null` | Deep-merged into the plugin's own `plugin_config.yml`. See [overrides mechanics](#overrides-mechanics). |

Unknown entry-level keys log a warning and are ignored. A legacy
top-level `arguments:` field on the plugin entry (as opposed to inside
`overrides:`) is ignored with a renamed-key warning — `arguments:`
belongs in the plugin's own `plugin_config.yml` or in an `overrides:`
block.

### Multiple instances of the same class

A class can be loaded multiple times under different `name` values.
Each instance gets its own `plugin_uuid` and its own copy of state —
useful for two Discord bots, two MQTT clients, etc.

```yaml
plugins:
  - name: DiscordBotMain
    enabled: true
    path: ./_private/AI/DiscordBot
    overrides:
      arguments:
        token: "<prod-token>"

  - name: DiscordBotDev
    enabled: true
    path: ./_private/AI/DiscordBot
    overrides:
      arguments:
        token: "<dev-token>"
```

Other plugins target a specific instance by name (and optionally by
`plugin_uuid` if multiple instances share a name in advanced setups):

```python
# Target by name (the common case):
await self.execute("DiscordBotDev", "send_message", args=("hello",))

# Target by plugin_uuid (when name alone is ambiguous):
await self.execute("DiscordBot", "send_message",
                   args=("hello",), plugin_uuid=some_uuid)
```

---

## Overrides mechanics

Each `plugins:` entry can carry an `overrides:` block that deep-merges
into the plugin's own `plugin_config.yml` at load time. Use this when
you want to customize a single instance of a class without forking the
plugin folder. Implemented by `apply_overrides`.

```yaml
plugins:
  - name: DiscordBotMain
    enabled: true
    path: ./_private/AI/DiscordBot
    overrides:
      description: "Production Discord bot"
      version: "1.4.2"               # value-replace
      remote: false
      verbose_notifier: true
      arguments:                     # forwarded to on_load
        token: "<prod-token>"
        guild_id: 123456789
      endpoints:
        send_message:
          remote: true               # raise the per-endpoint remote flag
      events:
        new_message:
          enabled: false             # silence this event
      subscriptions:
        on_command:
          authors: ["AI_Interaction"]
```

Override semantics:

- **Plugin-level scalars** (`description`, `remote`, `version`,
  `prefix`, `verbose_notifier`) — value-replace.
- **`endpoints:`** — STRICT. Unknown sub-keys under an endpoint
  override are fail-load errors. Protects against typos that would
  silently change nothing. Driven by
  `_STRICT_OVERRIDE_SECTIONS = frozenset({"endpoints"})` on `Plexus`.
- **`arguments:`**, **`events:`**, **`subscriptions:`** — LENIENT.
  Unknown subkeys merge in.
- **`__replace__: true`** in any sub-mapping triggers wholesale
  replace of that sub-mapping instead of merge.

```yaml
overrides:
  arguments:
    __replace__: true                # wipe the original arguments
    api_key: "..."
    region: "eu-west-1"
```

Without `__replace__`, the override is merged on top of the original.

### `endpoints:` strict-failure example

The strict mode catches override typos that would otherwise change
nothing silently. Suppose `plugin_config.yml` declares an endpoint
named `send_message`, and the override misspells it as `send_mesage`:

```yaml
overrides:
  endpoints:
    send_mesage:                     # typo — no such endpoint in base
      remote: true
```

Load fails with a `ConfigException` naming the unknown subkey. The
same applies to typos *inside* a known endpoint:

```yaml
overrides:
  endpoints:
    send_message:
      remoet: true                   # typo for "remote" — fail-load
```

Use this strictness deliberately: misspelled override keys never reach
production silently. Reserved endpoint `access_name` values follow the
same forbidden list as plugin names: `system`, `general`, `any`,
`remote`, `local`.

---

## `general:` (dict)

Framework-wide settings. Read by `ConfigUtil.apply_configvalues` and `Plexus.__init__`.

```yaml
general:
  hostname: ""
  plugin_package: plugins
  console_log_level: "DEBUG"
  file_log_level: "DEBUG"
  asyncio_debug: false
  logger_levels:
    asyncio: "MUTE"
    httpx: "WARNING"
  plugin_ready_timeout: 60.0
  plugin_disable_timeout: 30.0
  sync_dispatcher_workers: 4
```

### Reference

| Key | Type | Default | Notes |
|---|---|---|---|
| `hostname` | str | `socket.gethostname()` if empty | This node's identity for `local` / `remote` host filtering and for the advert protocol. |
| `plugin_package` | str | `"plugins"` | Default base directory for plugin entries that omit `path:`. |
| `console_log_level` | str | `"DEBUG"` | Level for the stdout/stderr handler. One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, `MUTE`. |
| `file_log_level` | str | `"DEBUG"` | Level for the file handler. Same valid set. |
| `logger_levels` | dict | `{}` | Per-logger thresholds. See below. |
| `asyncio_debug` | bool | `false` | Enables `loop.set_debug(True)` and `slow_callback_duration=0.5`. |
| `plugin_ready_timeout` | float | `60.0` | Cross-plugin readiness gate budget. |
| `plugin_disable_timeout` | float | `30.0` | Per-plugin `on_disable` cap during runtime disable / pop / reload. |
| `sync_dispatcher_workers` | int | `4` | Workers in the dedicated `SyncDispatcher` thread pool. |

### `plugin_ready_timeout` (default 60.0)

Budget for `_wait_for_plugin_ready`. When plugin A calls
`await self.execute("B", ...)`, the call blocks until plugin B's
`_lifecycle_ready` (framework-set after `on_enable` returns) AND
`ready` (author-controlled) flags are both set — or this timeout
elapses, in which case the call raises `RequestException`.

Don't drop below ~30s in normal operation. A bad value falls back to
the default with a warning.

### `plugin_disable_timeout` (default 30.0)

Per-plugin cap on `on_disable` execution during runtime
disable / pop / reload (`_disable_plugin`, `_pop_plugin_under_lock`).
Shutdown's per-plugin cap is hardcoded 30s separately and is not
affected by this knob.

If `on_disable` raises, times out, or returns, the framework still
flips `enabled = False` and unregisters the plugin's subs — bookkeeping
is in `try/finally`. The timeout exists so a hanging `on_disable` does
not block reload of other plugins.

### `sync_dispatcher_workers` (default 4)

Workers in the dedicated `SyncDispatcher` thread pool used for **sync
subscriber handlers** (sync `def` methods invoked through the
`publish_event` / `request_event` dispatch). Sync `execute()` endpoints
use a separate shared pool.

- Min 1. Bad values warn and fall back to 4.
- `workers=1` serializes all sync subscriber handlers — useful when
  handlers share non-thread-safe state.

### `logger_levels` — per-logger thresholds

Per-logger threshold overrides. Matches a logger by name with
prefix-and-dot-boundary semantics — longest prefix that matches a
logger's name wins. Implementation in
`LogUtil.apply_logger_levels_config`.

The value can be a single level string (applied to both console and
file handlers) or a split dict `{console: ..., file: ...}` for an
independent split:

```yaml
general:
  logger_levels:
    asyncio: "MUTE"                  # both handlers
    urllib3: "MUTE"
    httpx: "WARNING"
    psycopg: "WARNING"
    psycopg.pool: "DEBUG"            # longer prefix wins for psycopg.pool.X

    PluginA:                         # split form
      console: "INFO"                # only console raised
      file: "DEBUG"                  # file still records all
```

Valid levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`, `MUTE`.
Wildcards are NOT accepted. Dot boundary is required for prefix
matching: `"httpx"` matches `httpx` and `httpx.SOMETHING`, but NOT
`httpxSOMETHING`.

#### Runtime overrides — `set_logger_level` / `clear_logger_level`

In addition to config-driven `logger_levels`, plugins can adjust
thresholds at runtime via two helpers on the `Plugin` base class:

```python
self.set_logger_level("noisy_lib", console="MUTE", file="DEBUG")
self.clear_logger_level("noisy_lib")
```

Survival semantics:

- **Plugin-source runtime overrides survive config reloads** — a
  `load_config_yaml` does not wipe them.
- They ARE auto-cleared on `on_disable`, hot-swap, pop, purge, and
  shutdown — the framework tracks them by `(plugin_name, plugin_uuid)`.
- Each plugin's overrides are independent — clearing one plugin's
  override does not affect another plugin's override of the same
  logger.

`self.list_logger_levels()` returns a snapshot of all configured
thresholds (config + plugin sources combined).

---

## `networking:` (dict)

Cluster topology. See [networking](./networking.md) for the
operational reference (trust model, peer setup, advert protocol). This
section documents the YAML keys.

When `enabled: false`, the rest of this section is mostly inert —
`NetworkManager` is never created, and only `enabled` is consulted.

```yaml
networking:
  enabled: true
  port: 2510
  hostname: ""
  keys_dir: "_keys"
  pool_size: 5
  discover_nodes: true
  direct_discoverable: true
  auto_discoverable: false
  peers:
    - hostname: "node-b"
      address: "10.0.0.2"
      cert_file: "_keys/node-b.cert.pem"
      fingerprint: "sha256:abcd..."
```

### Top-level networking keys

| Key | Type | Default | Notes |
|---|---|---|---|
| `enabled` | bool | `false` | Toggles all networking. When `false`, `NetworkManager` is not created. |
| `port` | int | `2510` | Cluster default TCP port for the mTLS server. Per-peer port overrides allowed via `address: ip:port`. |
| `hostname` | str | `socket.gethostname()` | Node's networking hostname. Separate from `general.hostname` if needed for tests. |
| `keys_dir` | str | `"_keys"` | Where this node's `cert.pem` / `key.pem` live (auto-generated on first run). Resolved relative to the config file's directory if not absolute. |
| `peers` | list[dict] | `[]` | Peer trust list. **Required (non-empty)** when networking is enabled. See below. |
| `pool_size` | int | `5` | Connection-pool depth per `(ip, port)`. |
| `discover_nodes` | bool | `false` | Run periodic node-lookup loop (`update_all_nodes`). |
| `direct_discoverable` | bool | `false` | Allow peers that explicitly know this node's IP to connect. Auto-coerced to `true` when `auto_discoverable=true`. |
| `auto_discoverable` | bool | `false` | Allow peers to find this node via subnet scan. Forces `direct_discoverable=true`. |
| `heartbeat_interval` | float | `10.0` | Seconds between heartbeat ticks. Each tick pings every peer; on failure the peer is marked dead. Bad values fall back to default with a warning. |
| `lookup_interval` | float | `60.0` | Seconds between discovery / `update_all_nodes` loop ticks. Re-resolves peer addresses and reaps unreachable nodes. Bad values fall back to default with a warning. |
| `liveness_timeout` | float | `30.0` | A peer whose last successful heartbeat is older than this is considered dead. Should be `>= heartbeat_interval`; 2-3× is typical. Bad values fall back to default with a warning. |

The validators check `enabled`, `port`, `auto_discoverable`,
`direct_discoverable`, and `discover_nodes` for presence (warn on
missing).

### Per-peer fields (`peers:` entries)

| Field | Type | Required | Notes |
|---|---|---|---|
| `hostname` | str | YES | Canonical key for advert state and routing. |
| `address` | str | YES | `ip`, `ip:port`, or `[ipv6]:port`. Bare IP uses cluster default port. |
| `cert_file` | str | one of | Path to the peer's PEM-encoded certificate, relative to the config file. |
| `cert_pem` | str | one of | Inline PEM string. Pick `cert_file` OR `cert_pem`. |
| `fingerprint` | str | optional | `sha256:<hex>` of the SubjectPublicKeyInfo DER. Derived from the cert at parse time; if supplied, must match (mismatch is a hard error). |
| `system_caller` | bool | optional | When `true`, this peer's calls inherit `"system"` author privileges (bypasses author whitelists). Default `false`. |

```yaml
peers:
  - hostname: alpha
    address: "10.0.0.1"              # bare IP -> cluster default port
    cert_file: _keys/peers/alpha.pem
    system_caller: false

  - hostname: beta
    address: "10.0.0.2:2511"         # explicit port override
    cert_pem: |
      -----BEGIN CERTIFICATE-----
      ...
      -----END CERTIFICATE-----
    fingerprint: "sha256:abcd..."
```

When `networking.enabled: true` and `peers` is empty,
`NetworkManager.start()` raises a fail-fast error with migration
guidance — an empty trust store would otherwise reject every connection
with an opaque OpenSSL error.

### Removed / legacy fields

| Key | Status | Migration |
|---|---|---|
| `node_ips` | REMOVED. Hard error if present. | Use `peers:`. |
| `secret` | accepted but no longer used | Remove from config. |
| `cert_file` (top-level, not per-peer) | accepted but no longer used | Remove from config. |
| `key_file` (top-level, not per-peer) | accepted but no longer used | Remove from config. |

When migrating an older config, replace the top-level `node_ips:` list
with a `peers:` list (one entry per peer with `hostname`, `address`,
and either `cert_file` or `cert_pem`).

---

## A complete minimal `config.yml`

```yaml
plugins:
  - name: AveragePlugin
    enabled: true
    path: ./copypasta/AveragePlugin

general:
  hostname: ""              # empty = system hostname
  plugin_package: plugins
  console_log_level: "INFO"
  file_log_level: "DEBUG"
  asyncio_debug: false
  plugin_ready_timeout: 60.0
  plugin_disable_timeout: 30.0
  sync_dispatcher_workers: 4
  logger_levels:
    asyncio: "MUTE"
    httpx: "WARNING"

networking:
  enabled: false
  port: 2510
  discover_nodes: false
  direct_discoverable: false
  auto_discoverable: false
  peers: []                 # required non-empty when networking.enabled is true
```

This is enough to boot. Defaults handle everything else: hostname is
`socket.gethostname()`, log level is `DEBUG`, no networking, no
multi-node concerns.

---

## A complete two-node `config.yml`

```yaml
plugins:
  - name: AveragePlugin
    enabled: true
    path: ./copypasta/AveragePlugin

general:
  hostname: alpha
  plugin_package: plugins
  console_log_level: "INFO"
  file_log_level: "DEBUG"
  asyncio_debug: false
  plugin_ready_timeout: 60.0
  plugin_disable_timeout: 30.0
  sync_dispatcher_workers: 4

networking:
  enabled: true
  hostname: alpha
  port: 2510
  keys_dir: "_keys"
  pool_size: 5
  discover_nodes: true
  direct_discoverable: true
  auto_discoverable: false
  peers:
    - hostname: beta
      address: "10.0.0.2:2510"
      cert_file: "_keys/peers/beta.pem"
    - hostname: gamma
      address: "10.0.0.3:2511"
      cert_pem: |
        -----BEGIN CERTIFICATE-----
        MIIBIjANBgk...
        -----END CERTIFICATE-----
      system_caller: false
```

The corresponding `config.yml` on `beta` would have `hostname: beta`
and a `peers:` entry pointing at `alpha` with `alpha`'s cert. See
[networking](./networking.md) for the operator workflow that produces
those cert files.

---

## Hot-reloading config

`Plexus.load_config_yaml(path)` re-reads the config file,
validates it, and re-applies it. The async wrapper is
`async_load_config_yaml(path)`. Behaviour:

- Top-level `plugins:` entries that newly appear get loaded.
- Entries that disappear get popped.
- Entries whose `overrides` change materially trigger a hot-swap of the
  affected plugin (via `_reload_plugin` — the `on_disable` + fresh
  `__init__` + `on_enable` path).
- Plugin-source per-logger runtime overrides survive the reload (they
  are auto-cleared only on disable / pop / purge / shutdown).

`save_config_file(path, content, backup=True)` validates a YAML
payload and writes it to disk (with a `.bak` if `backup=True`). It
does NOT trigger a reload — call `load_config_yaml` afterwards if
that's what you want.

For a forced full-cycle reload of one plugin without changing config,
use `Plexus._reload_plugin(name)` directly.

---

## What's NOT a config knob

A few things that look like they ought to be tunable but aren't:

- The **shutdown-time per-plugin `on_disable` budget** is hardcoded to
  30 seconds. Use `plugin_disable_timeout` for the runtime per-plugin
  cap (which `_disable_plugin` / `_pop_plugin_under_lock` honour).
- The **30-second wait for in-flight tracked tasks at `close()` time**
  is hardcoded.

These are deliberate caps. If a deployment needs them tunable, raise
it as a feature request.
