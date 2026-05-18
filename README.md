# Plexus

*Last updated for Plexus 0.40.0*

An async Python plugin framework with multi-node mTLS networking and pub/sub
event routing. PyPI package: [`plexus-core`](https://pypi.org/project/plexus-core/).

`Plexus` loads small, single-responsibility Python classes — *plugins* — from
disk, drives a deterministic `on_load` / `on_enable` / `on_disable` lifecycle,
and gives them three ways to talk to each other:

- direct method calls — `await self.execute("OtherPlugin", "method", args=...)`
- 1:N events — `await self.publish_event("event_id", payload=...)`
- 1:1 ask-by-topic — `result = await self.request_event("event_id", payload=...)`

The runtime can stretch across multiple nodes over an mTLS-pinned wire
protocol (`NetworkManager`), so the same `execute` / `publish_event` /
`request_event` calls transparently fan out to peer machines whose plugins
are flagged `remote: true`.

A typical deployment in conversational AI, data-pipeline, or event-driven
domains wires together base plugins (a Discord bot, a Postgres adapter, an
LLM adapter, a TTS pipeline) with orchestrator plugins that hold the
business logic — each one a class plus a YAML manifest.

---

## Highlights

- **One-class-per-plugin layout.** A `plugin.py` plus a declarative
  `plugin_config.yml` describing endpoints, events, and subscriptions.
- **Three call styles.** Direct endpoint calls (`execute`), 1:N
  fire-and-forget events (`publish_event`), and 1:1 topic-routed requests
  with optional streaming (`request_event`, `request_event_stream`).
- **Hot-swap reloading.** Any plugin can be disabled, re-instantiated from
  config, and re-enabled at runtime — its sockets, background tasks, and
  event subscriptions tear down and rebuild on a fresh class instance with
  no downtime for the rest of the cluster.
- **Three-tier discipline (project policy).** *Base* plugins wrap one
  protocol or service; *Extension* plugins glue bases together; *Orchestrator*
  plugins hold business logic.
- **Multi-node clustering.** Pinned-fingerprint mTLS between peers; remote
  endpoints and remote subscribers are reachable through the same Plugin
  base methods that drive local calls.
- **Sync and async surfaces.** Every cross-plugin call has both an `async`
  form and a sync form that bridges to the loop, so plugins written against
  blocking libraries do not have to twist themselves into coroutines.
- **Strict but small.** Around fifteen public methods on the Plugin base
  class; everything else is YAML.

---

## Install

Requires Python 3.11+.

From PyPI (recommended for using Plexus as a library):

```bash
pip install plexus-core
```

From source (for developing on the framework itself):

```bash
git clone https://github.com/Haflix/AIO_Assistant_Core.git
cd AIO_Assistant_Core
python -m venv .venv
.venv/Scripts/activate          # Windows
# source .venv/bin/activate     # Linux / macOS
pip install -e .
cp config.example.yml config.yml
```

`config.yml` is gitignored. Edit it to enable the plugins you want.

---

## Quickstart

A plugin lives in its own folder containing exactly two files: `plugin.py`
(the class) and `plugin_config.yml` (declarative metadata).

The shipped example (`copypasta/AveragePlugin/`) demonstrates every moving
part: lifecycle hooks, a regular endpoint, a streaming endpoint, a
cross-plugin call, and a topic-subscribed endpoint.

### `copypasta/AveragePlugin/plugin.py` (excerpt)

```python
from plexus.utils import Plugin
from plexus.decorators import (
    log_errors, async_log_errors,
    async_handle_errors, async_gen_log_errors,
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
        return value * 2

    @async_handle_errors(default_return=None)
    async def call_other_plugin(self, plugin_name, method_name, args):
        return await self.execute(plugin_name, method_name, args, hosts="any")

    @async_log_errors
    async def handle_event(self, event):
        # event.topic, event.payload, event.author, event.author_host
        return {"received": event.payload, "handled_by": self.plugin_name}

    @async_gen_log_errors
    async def example_stream(self, count):
        for i in range(count):
            await asyncio.sleep(0.1)
            yield f"Item {i + 1} of {count}"
```

### `copypasta/AveragePlugin/plugin_config.yml`

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
    internal_name: example_method
    remote: True
    accessible_by_other_plugins: True
    description: Doubles the input value.
    arguments:
      - name: value
        type: any
        description: Input value to process

  example_stream:
    internal_name: example_stream
    remote: True
    accessible_by_other_plugins: True
    description: Streaming method that yields sequential results.
    arguments:
      - name: count
        type: int
        description: Number of items to yield

  handle_event:
    internal_name: handle_event
    remote: True
    accessible_by_other_plugins: True
    description: Endpoint subscribed to "example/event" via the subscriptions block.
    arguments:
      - name: event
        type: any
        description: Event object received from the topic
```

### Register and run

Add the plugin to `config.yml`:

```yaml
plugins:
  - name: AveragePlugin
    enabled: true
    path: ./copypasta/AveragePlugin
```

Then launch:

```bash
python main_application.py
```

`Plexus` will load and enable AveragePlugin. From any other plugin you
can now call:

```python
result = await self.execute("AveragePlugin", "example_method", args=21)
# -> 42

async for chunk in self.execute_stream("AveragePlugin", "example_stream", args=3):
    print(chunk)
```

To fire the topic that AveragePlugin is subscribed to, the publishing
plugin declares the event in its own `plugin_config.yml`:

```yaml
events:
  greet:
    topic: "example/event"
```

```python
count = await self.publish_event("greet", payload={"hello": "world"})
# count == number of subscribers the dispatch was scheduled for
```

That is the entire surface to write something useful. The rest is just
more endpoints, more events, more subscriptions.

---

## Documentation

| File | Audience | Focus |
|---|---|---|
| [docs/architecture.md](docs/architecture.md) | new readers | mental model, lifecycle, three-tier discipline, hot-swap, shutdown order |
| [docs/plugin_authoring.md](docs/plugin_authoring.md) | plugin authors | full `plugin_config.yml` schema and a tutorial-style walkthrough |
| [docs/api_reference.md](docs/api_reference.md) | reference users | every `Plugin`-base method (signature, args, raises) plus decorators |
| [docs/notifier.md](docs/notifier.md) | event-system users | publish vs request, topic templating, filter chain, runtime subs |
| [docs/networking.md](docs/networking.md) | multi-node operators | mTLS, peers, cert pinning, remote semantics, wire protocol |
| [docs/configuration.md](docs/configuration.md) | operators | full `config.yml` reference and per-plugin overrides |

---

## Status

Active development. Public API of the `Plugin` base class is stable;
internal `Plexus` helpers (`_*` prefix) are not. See the wire-protocol
table in [docs/networking.md](docs/networking.md) for cross-version
compatibility.

## License

See [LICENSE](LICENSE).
