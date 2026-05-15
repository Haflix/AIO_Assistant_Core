# Notifier and Events

*Last updated for AIO Assistant Core 0.22.3*

Deep dive on the topic-based event system. The user-facing Plugin
methods are covered in [api_reference.md](./api_reference.md); this page
explains how those methods work, the matching algorithm, the filter
chain, and the topic-templating rules.

For the cross-node side of fan-out and `request_event` fall-through,
see [networking.md](./networking.md). For where subscriptions and events
sit in a plugin's lifecycle, see [plugin_authoring.md](./plugin_authoring.md).

---

## The three call shapes

| Shape | Method | Returns | Failure mode |
|---|---|---|---|
| 1:N fire-and-forget | `publish_event` | count of subs scheduled (int) | Errors inside subscribers are logged, never raised. Returns 0 on no match. |
| 1:1 ask | `request_event` | first matching handler's return value | Raises `RequestException` if no subscriber matches. |
| 1:1 stream | `request_event_stream` | async generator of chunks | Pre-first-chunk: same fall-through as `request_event`. Post-first-chunk: committed to that producer. |

All three resolve their topic from a publisher-declared `events:` entry —
the `event_id` argument is a key into that block. The resulting topic
flows through the matching algorithm to find candidate subscriptions,
applies the filter chain, then dispatches.

---

## Topic syntax

A topic is a `/`-separated string of segments.

- `messages/incoming` — two segments.
- `sensor/livingroom/temperature` — three segments.

A subscription pattern can use `*` to match exactly one segment:

- `messages/*` matches `messages/incoming`, `messages/outgoing`. Does NOT
  match `messages/incoming/private` (different segment count).
- `sensor/*/temperature` matches `sensor/kitchen/temperature` and
  `sensor/livingroom/temperature`.

Validation rules:

- Empty middle segments are rejected (`a//b` is invalid).
- Mid-segment `*` is rejected (`mes*ages` is not a wildcard).
- Event topics (publisher-declared) MAY NOT contain `*`. Subscription
  topics MAY.

The matcher (`notifier.py:164-188`, `_topic_matches`) splits both
strings on `/` and matches segment-by-segment, requiring identical
segment counts. `*` matches exactly one non-empty segment.

### Wildcards in events vs subscriptions

`events:` topics MAY NOT contain `*`. Subscription topics MAY. The
validator runs at YAML load and at runtime `subscribe(...)`.

---

## YAML-declared events and subscriptions

A publisher declares events in `plugin_config.yml`:

```yaml
events:
  message_received:
    topic: "{prefix}/messages/incoming"
    hosts: "any"
    enabled: true
```

A subscriber declares subscriptions:

```yaml
subscriptions:
  on_incoming:
    topic: "ChatPlugin/messages/incoming"
    target_access_name: handle_incoming
    hosts: "any"
    authors: ["ChatPlugin"]
```

`target_access_name` must name a declared endpoint on this plugin (or, if
`target_plugin` is set, on the named plugin). When `target_plugin` is
unset or empty, the sub self-routes — the framework substitutes the
owner's `plugin_name` (`notifier.py:237`,
`effective_target_plugin = target_plugin or plugin_name`).

Subscriptions are registered with the topic registry BEFORE `on_enable`
runs, by `_register_yaml_subscriptions` (`core.py:2108-2145`).
Subscriptions added at runtime via `await self.subscribe(...)` follow
the YAML registrations in insertion order.

---

## Topic templating

Two kinds of placeholders are supported in event and subscription topics.

### Load-time placeholders (resolved when the YAML is parsed)

Resolved once, when the plugin loads, by `_resolve_load_time_template`
(`core.py:155-186`):

| Placeholder | Substituted with |
|---|---|
| `{prefix}` | The plugin's `prefix` field (defaults to `plugin_name`). |
| `{plugin_name}` | The plugin's name from `config.yml`. |
| `{hostname}` | The node's hostname (from `general.hostname` or `socket.gethostname()`). |
| `{plugin_uuid}` | The plugin instance's `plugin_uuid` (regenerated on every load/reload). |

These resolve once, at plugin load. After load, `plugin.events` and
`plugin.subscriptions` hold concrete topics — no placeholders remain
except runtime ones.

### Runtime placeholders (resolved per-publish via `topic_vars`)

ANY other `{var}` placeholder is filled at publish time from the
`topic_vars` argument:

```yaml
events:
  user_message:
    topic: "messages/{user_id}/incoming"
```

```python
await self.publish_event(
    "user_message",
    payload="hello",
    topic_vars={"user_id": "alice"},
)
# Resolves to topic "messages/alice/incoming".
```

Validation of `topic_vars`:

- Type: `Dict[str, str]` or `None`.
- Keys must NOT collide with reserved load-time names (`prefix`,
  `plugin_name`, `hostname`, `plugin_uuid`).
- Values must NOT contain `/`, must not be empty / whitespace-only, must
  not have leading/trailing whitespace.
- Missing keys for `{var}` placeholders raise `ValueError`.
- Extra keys not used by the template log a warning.
- Static topic + non-empty `topic_vars` logs a warning (likely confused
  `payload` and `topic_vars`).

### Why subscriptions reject runtime `{var}`

Runtime placeholders only make sense on the publish side — the
publisher knows what value to fill in. On the subscribe side, `{var}`
cannot be resolved at load time and would never match anything at
dispatch time. The subscription validator
(`core.py:247-263`, `_validate_subscription_topic`) rejects them.
A subscriber that wants to handle every user uses a wildcard:

```yaml
subscriptions:
  on_user_message:
    topic: "messages/*/incoming"
    target_access_name: handle_user_message
```

---

## The Subscription dataclass

Every YAML or runtime sub becomes a `Subscription` in the topic
registry. Defined in `notifier.py:66-119`.

| Field | Type | Notes |
|---|---|---|
| `sub_uuid` | `str` | Canonical identity. uuid4 hex. |
| `declared_id` | `Optional[str]` | YAML key for declared subs; `None` for runtime. Becomes `Event.subscription_id` for declared subs. |
| `topic_pattern` | `str` | The literal or wildcard topic pattern. |
| `plugin_name` / `plugin_uuid` | str / str | OWNER (the plugin that declared/registered the sub). |
| `target_plugin` / `target_access_name` | str / str | Routing target. Defaults to self-routing (`target_plugin == plugin_name`) when `target_plugin` is unset. |
| `target_plugin_uuid` | `Optional[str]` | Optional instance pin. |
| `hosts` | `str / list / None` | Receiver-side host filter. Default `"any"` (`notifier.py:108`). |
| `blocked_hosts` | `str / list / None` | Receiver-side host blacklist. |
| `authors` / `blocked_authors` | str / list / None | Author whitelist / blacklist. |
| `enabled` | `bool` | Default `True`. Disabled subs stay registered but are skipped at match time. |

**Owner vs target.** The owner is the plugin that wrote the YAML or
called `subscribe()`. The target is the plugin whose endpoint actually
receives the dispatched `Event`. For most subs they are the same.

Cross-plugin orchestrator subs set `target_plugin` to a different
plugin — useful for an orchestrator that wants to route certain topics
to a specific base plugin's endpoint without that base plugin declaring
the subscription itself.

---

## The matching algorithm

The topic registry (`TopicRegistry`, `notifier.py:122-367`) stores
subscriptions in a single insertion-ordered dict keyed by `sub_uuid`.

- `find_all(topic)` iterates all subs in insertion order, returns every
  sub whose `topic_pattern` matches.
- `find_first(topic)` iterates in insertion order, returns the first
  match.

There is **no exact-then-wildcard split.** Insertion order alone
determines tie-breaks. For example, with two subs in this order:

1. `messages/*` (registered first)
2. `messages/incoming` (registered second)

A publish to `messages/incoming` matches both. `find_first` returns
sub 1 (the wildcard) because it was registered first. If you want the
exact match to win, register it first.

Disabled subs (`enabled: false`) are skipped at match time but stay in
the registry for advert / introspection.

---

## The filter chain

For every candidate subscription found by `find_all` / `find_first`, the
framework runs a chain of filters. Any filter that rejects drops the
candidate; only candidates that survive every filter actually receive
the event. Each filter is a separate predicate so the rules compose
cleanly.

### 1. Publisher hosts gate

`_publisher_targets_local` (`core.py:3737-3784`) decides whether
this publish should target local subs at all. The publisher's effective
`hosts` and `blocked_hosts` (manifest, optionally overridden per-call)
gate this. Default `hosts="local"` if the publisher omits it. `"any"`,
`"local"`, the publisher's own hostname, or a list containing any of
those accepts. `blocked_hosts` excludes.

### 2. Sub-level local accept

`_sub_accepts_local` (`core.py:3786-3814`). Whether the
subscriber wants local events. The sub's `hosts` must accept `"local"`,
own hostname, or `"any"`; the sub's `blocked_hosts` must not block
them. Default sub `hosts="any"` accepts everything.

### 3. Sub-level remote-publisher accept

`_sub_accepts_remote_publisher` (`core.py:3816-3871`). For
inbound peer publishes only. A sub with `hosts="local"` rejects remote
publishers. Otherwise the sub's `hosts` / `blocked_hosts` are checked
against the remote publisher's `author_host`.

### 4. Author filter

`_sub_accepts_author` (`core.py:3873-3906`). `authors` is a
whitelist; `blocked_authors` is a blacklist. The publisher's
`plugin_name` is checked against both.

### The `"system"` author bypass

The pseudo-author `"system"` is used for framework-originated calls
(e.g. an `execute()` made with default `author="system"`). A sub's
`authors:` whitelist accepts `"system"` automatically — UNLESS
`"system"` is explicitly named in `blocked_authors`. This is
intentional: it lets framework dispatch reach legitimately gated subs
without the author having to remember to include `"system"` in every
whitelist.

```yaml
# Whitelist that ALSO accepts "system":
authors: ["AI_Interaction"]

# Whitelist that REJECTS "system":
authors: ["AI_Interaction"]
blocked_authors: ["system"]
```

---

## Disabled events vs disabled subs

Both have an `enabled: false` knob. Behaviour is symmetric but not
identical.

**Disabled events** (publisher side):

- `publish_event` silently drops and returns 0.
- `request_event` raises `RequestException("event ... disabled (C2)")`.
- `request_event_stream` raises the same.

Useful when a plugin's publisher should be turned off in some
deployment without removing the YAML.

**Disabled subs** (subscriber side):

- Stay in the registry — visible to introspection and advertised to
  peers.
- Skipped by `find_all` and `find_first` at match time.

Useful for feature flags and for advertising future bindings without
activating them yet.

---

## What handlers receive

Subscriber endpoints receive ONE positional argument: an `Event`
dataclass (`utils.py:1754-1807`).

```python
@async_log_errors
async def handle_event(self, event):
    # event.topic           -- literal topic that fired
    # event.payload         -- whatever the publisher passed
    # event.author          -- publisher plugin_name (or "system")
    # event.author_id       -- publisher plugin_uuid
    # event.author_host     -- publisher hostname
    # event.subscription_id -- declared_id (YAML) OR sub_uuid (runtime)
    # event.timestamp       -- epoch seconds at publish time
    return {"ok": True}
```

| Field | Type | Notes |
|---|---|---|
| `topic` | `str` | The literal topic that fired (post-resolution). |
| `payload` | `Any` | Whatever was passed to `publish_event` / `request_event`. |
| `author` | `str` | Publisher plugin_name (or `"system"`). |
| `author_id` | `str` | Publisher plugin_uuid. |
| `author_host` | `str` | Publisher hostname. |
| `subscription_id` | `str` | `declared_id` for YAML subs, `sub_uuid` for runtime subs. |
| `timestamp` | `float` | Epoch seconds at publish time. |

Note: endpoints called via `execute()` receive raw unpacked args, NOT
an `Event`. The same method can serve both call paths — but it must
handle a single `Event` argument when invoked through publish/request,
and the unpacked arguments when invoked through `execute()`. In practice,
endpoints are usually one or the other.

---

## Declarative YAML vs runtime subscribe

Use YAML when the subscription set is static — known at plugin load
time. The framework registers YAML subs before `on_enable` runs
(`core.py:2108-2145`), so the subscription is live from the
moment the plugin enables.

Use `await self.subscribe(...)` (`utils.py:1495-1530`) when the
subscription set is dynamic — e.g. an orchestrator that subscribes to a
per-user topic when a user appears.

```python
async def on_enable(self):
    # Register dynamic subs.
    self._sub = await self.subscribe(
        "messages/*",
        target_access_name="handle_message",
        hosts="any",
        authors=["ChatPlugin"],
    )
```

`subscribe()` returns a `sub_uuid`. `unsubscribe(sub_uuid)` removes it.
Runtime subs use `sub_uuid` as `Event.subscription_id`; declared subs
use the YAML key (`declared_id`).

Both YAML and runtime subs are auto-cleared by
`_unregister_plugin_subscriptions` when the owner is disabled or
hot-swapped — cleanup is keyed by `plugin_uuid`, so you do not need to
manually `unsubscribe` in `on_disable` for lifecycle parity. Call
`unsubscribe` only when you want a sub removed earlier than the
plugin's own teardown.

The legacy `handler=...` kwarg form was removed. Runtime subs always
route to a declared endpoint named via `target_access_name`.

---

## Insertion-order tie-break (request_event)

`request_event` returns the first matching handler's result. With
multiple matching subs, insertion order picks the winner.

Local subs are tried first. On no local match, remote candidates are
tried in advert insertion order — the order in which peers told us about
their subs. A remote candidate that fails with `NoLocalSubException` (the
peer signaled "no sub matched on my side either") or `NetworkRequestException`
(connection error) is skipped and the next candidate is tried. A
generic `RequestException` from a remote peer propagates — the candidate
matched but its handler raised, so we surface that error rather than
papering over it with another peer's response.

This fall-through preserves strict semantics: a `request_event` either
returns a real handler's result or raises; it never silently continues
past a real failure to a second-best peer.

For `request_event_stream`, fall-through applies pre-first-chunk only.
Once the producer yields its first chunk, the consumer is committed —
later errors do not redirect to a different peer.

---

## SyncDispatcher

A subscriber endpoint can be `async def` or plain `def`. The framework
runs each kind on a different executor:

- Async handlers run on the main event loop directly.
- Sync handlers are submitted to a dedicated `SyncDispatcher`
  (`notifier.py:24-63`) — a
  `ThreadPoolExecutor(max_workers=N, thread_name_prefix="sync-notifier")`
  separate from the framework's general-purpose plugin executor.

- Default workers: 4. Configurable via `general.sync_dispatcher_workers`
  in `config.yml`.
- Min 1 worker (clamped via `max(1, int(workers))` at
  `notifier.py:46`). With `workers=1` you get serialization of all sync
  subscriber handlers — useful when handlers share non-thread-safe state.
- Sync `execute()` endpoints use a SEPARATE shared thread pool
  (`_plugin_executor`). The two pools do not contend, so a slow sync
  subscriber cannot starve sync `execute()` calls.
- Shutdown happens AFTER the 30 s in-flight drain in
  `Plexus.close()`, with a 30 s budget; falls back to `wait=False`
  on timeout.

---

## Logging

Subscribe / unsubscribe events ALWAYS log at INFO regardless of
`verbose_notifier`. Dispatch logging (per-publish match details, target
counts) is gated by `plugin.verbose_notifier`. Set
`verbose_notifier: true` on a plugin temporarily when debugging why a
particular event isn't reaching a particular sub. It is noisy in
production.

---

## Cross-node behaviour (preview)

Each subscription registered locally is advertised to peers via the
wire-protocol advert messages. When a publisher fires, the framework
fans out to local subscribers AND schedules `MSG_PUBLISH_EVENT` to peers
whose advertised subs match. For `request_event`, on no local match,
the framework iterates advertised subs in insertion order (across
peers) and tries each candidate over the wire. See
[networking.md](./networking.md) for the full picture.

---

## Decision tree

```
   Need to send data from plugin A to plugin B?
   |
   |-- Do I know B by name and want a direct call?
   |       --> await self.execute("B", "method", args=...)
   |
   |-- Does the message have N potential listeners (any number, including zero)?
   |       --> await self.publish_event("event_id", payload=...)
   |
   |-- Do I want one answer, but I don't care which subscriber gives it?
   |       --> result = await self.request_event("event_id", payload=...)
   |
   |-- Do I want a stream of chunks back?
   |       --> async for chunk in self.request_event_stream("event_id", ...):
   |
   `-- Do I want to register a listener at runtime (not via YAML)?
           --> sub_uuid = await self.subscribe("topic/*", target_access_name="handler")
```

---

## Quick checklist for getting an event to fire

1. Publisher's manifest has an `events:` entry with the right `topic:`.
2. Publisher calls `await self.publish_event(event_id, payload=...)`
   (or `request_event` / `request_event_stream`).
3. Subscriber's manifest has a `subscriptions:` entry whose `topic:`
   matches (literal or wildcard).
4. Subscriber's manifest has the matching endpoint under `endpoints:`
   with the same name as `target_access_name`.
5. Both subs and event are `enabled: true` (the default).
6. The filter chain accepts: publisher hosts, sub hosts, author filter.
7. The plugins are loaded and enabled (`enabled: true` in `config.yml`).
