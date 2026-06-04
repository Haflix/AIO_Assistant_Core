"""Plexus — plugin framework core.

Plexus is the orchestrator class at the heart of the framework. It loads
plugin classes from disk, drives the on_load / on_enable / on_disable
lifecycle, and gives plugins three ways to talk: direct execute() calls,
1:N publish_event(), and 1:1 request_event() with optional streaming.

A minimal plugin looks like:

    from plexus import Plugin, log_errors, async_log_errors

    class MyPlugin(Plugin):
        @log_errors
        def on_load(self, *args, **kwargs): ...

        @async_log_errors
        async def on_enable(self): ...

        @async_log_errors
        async def on_disable(self): ...

A minimal application entry point:

    from plexus import Plexus
    plx = Plexus("config.yml")
    await plx.wait_until_ready()

Submodules:
    core                — Plexus class (config, lifecycle, dispatch orchestrator)
    utils               — Plugin base, Request, Event, ConfigUtil, LogUtil
    networking          — NetworkManager + mTLS + advert protocol
    notifier            — TopicRegistry + Subscription + SyncDispatcher
    decorators          — @log_errors / @handle_errors family
    exceptions          — framework exception hierarchy
    serialization       — SafeUnpickler + Serializable mixin
    plugin_state        — State / Phase enums + PluginState dataclass
    networking_classes  — Node + RemotePlugin data classes
"""

__version__ = "0.41.1"

from .core import Plexus
from .utils import (
    Plugin,
    Request,
    GeneratorRequest,
    EndOfQueue,
    Event,
    LogUtil,
    ConfigUtil,
)
from .exceptions import (
    ConfigException,
    RequestException,
    NetworkRequestException,
    NoLocalSubException,
    NodeException,
    PluginTypeMismatchError,
    PluginDependencyError,
)
from .decorators import (
    log_errors,
    handle_errors,
    async_log_errors,
    async_handle_errors,
    gen_log_errors,
    gen_handle_errors,
    async_gen_log_errors,
    async_gen_handle_errors,
)
from .serialization import (
    Serializable,
    SerializableException,
    safe_loads,
)
from .plugin_state import (
    State,
    Phase,
    ErrorRecord,
    PluginState,
)
from .networking_classes import (
    Node,
    RemotePlugin,
)
from .notifier import (
    TopicRegistry,
    Subscription,
)
# C-170: pull in the public networking + serialization symbols that
# operators reach for from sibling-repo plugins and migration docs but
# that the old __all__ omitted. SyncDispatcher is removed from the
# public surface — it is framework-internal and was leaked by accident.
from .networking import (
    AdvertSub,
    PeerSpec,
)
from .serialization import (
    FINGERPRINT_CLI_CMD,
)

__all__ = [
    "Plexus",
    "Plugin",
    "Request",
    "GeneratorRequest",
    # R4-XX-9: EndOfQueue is the drain sentinel for GeneratorRequest
    # queues. Advanced streaming consumers that bypass execute_stream
    # in favour of raw GeneratorRequest.get_queue_stream() / .result_queue
    # need it to detect stream end.
    "EndOfQueue",
    "Event",
    "LogUtil",
    "ConfigUtil",
    "ConfigException",
    "RequestException",
    "NetworkRequestException",
    "NoLocalSubException",
    "NodeException",
    "PluginTypeMismatchError",
    "PluginDependencyError",
    "log_errors",
    "handle_errors",
    "async_log_errors",
    "async_handle_errors",
    "gen_log_errors",
    "gen_handle_errors",
    "async_gen_log_errors",
    "async_gen_handle_errors",
    "Serializable",
    "SerializableException",
    "safe_loads",
    # C-170: serialization helpers used in migration instructions.
    "FINGERPRINT_CLI_CMD",
    "State",
    "Phase",
    "ErrorRecord",
    "PluginState",
    "Node",
    "RemotePlugin",
    # C-170: networking dataclasses referenced in public networking
    # docs (peers: schema, advert protocol). Operators sometimes need
    # to construct PeerSpec for tests / fixtures.
    "AdvertSub",
    "PeerSpec",
    "TopicRegistry",
    "Subscription",
    # C-170: SyncDispatcher intentionally NOT exported — framework
    # internal, no plugin-facing API. Reach via plexus.notifier if you
    # really need it from a test.
]
