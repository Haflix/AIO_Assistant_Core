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

from .core import Plexus
from .utils import (
    Plugin,
    Request,
    GeneratorRequest,
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
    PluginTypeMissmatchError,
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
    SyncDispatcher,
)

__all__ = [
    "Plexus",
    "Plugin",
    "Request",
    "GeneratorRequest",
    "Event",
    "LogUtil",
    "ConfigUtil",
    "ConfigException",
    "RequestException",
    "NetworkRequestException",
    "NoLocalSubException",
    "NodeException",
    "PluginTypeMissmatchError",
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
    "State",
    "Phase",
    "ErrorRecord",
    "PluginState",
    "Node",
    "RemotePlugin",
    "TopicRegistry",
    "Subscription",
    "SyncDispatcher",
]
