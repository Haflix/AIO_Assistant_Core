"""Networking-side data classes — RemotePlugin and Node.

Note (v0.26.0): the Plugin state machine added in Session 3 applies to
LOCAL plugins only. ``RemotePlugin.enabled`` is the snapshot of a remote
plugin's enabled state at advertisement time — a distinct concept from
the local state machine. ``RemotePlugin.enabled`` and ``Node.enabled``
remain mutable attributes.
"""

import time
import asyncio
from typing import Any, Optional, Tuple, Union


class RemotePlugin:
    def __init__(
        self,
        name: str,
        version: str,
        uuid: str,
        enabled: bool,
        remote: bool,
        description: str,
        arguments: Union[list, dict, tuple],
        hostname: str,
    ):
        self.plugin_name = name
        self.version = version
        self.plugin_uuid = uuid
        self.enabled = enabled
        self.remote = remote
        self.description = description
        self.arguments = arguments
        self.hostname = hostname


class Node:
    def __init__(
        self,
        IP: str,
        hostname: str,
        enabled: bool,
        auto_discoverable: bool,
        port: Optional[int] = None,
    ):
        """Node class for the networking system that represents another device with this script running

        Args:
            IP (str): The IP address of the node
            hostname (str): The hostname of the node
            enabled (bool): Whether the node is enabled
            auto_discoverable (bool): Whether the node is auto-discoverable (can be discovered by other nodes with the automatic discovery feature)
            port (Optional[int]): Per-node port; None means use the cluster default port.
        """
        self.IP = IP
        self.hostname = hostname
        self.enabled = enabled
        # B-072 fix: None sentinel for "never received a heartbeat".
        # Previously 0 doubled as "never" and "received at epoch 0";
        # ``is_alive*`` short-circuits on None so a freshly-created
        # Node correctly reads as not-alive until the first successful
        # heartbeat sets an int timestamp.
        self.last_heartbeat: Optional[int] = None
        self.auto_discoverable = auto_discoverable
        self.port = port  # None → use NetworkManager.port default

    def __str__(self) -> str:
        """String-representation of a node."""
        port_str = f":{self.port}" if self.port is not None else ""
        # W1-A5: the prior implementation called the sync liveness probe
        # with no timeout argument, hardcoding the 30s default and
        # ignoring NetworkManager.liveness_timeout. __str__ now shows the
        # raw last_heartbeat int instead; callers / operators apply
        # whatever liveness predicate they want.
        return (
            f"   IP: {self.IP}{port_str}\n"
            f"     Hostname: {self.hostname}\n"
            f"     Enabled: {self.enabled}\n"
            f"    Last Heartbeat: {self.last_heartbeat}\n"
            f"     Discoverable: {self.auto_discoverable}\n"
        )

    async def _to_tuple(self) -> Tuple[str, Optional[int], str]:
        """
        Returns:
            Tuple[str, Optional[int], str]: A 3-tuple of:
                - IP address
                - port (None if the node uses the cluster default port)
                - hostname
        """
        return (self.IP, self.port, self.hostname)

    async def heartbeat(self):
        """Updates heartbeat timestamp.

        C-012: uses ``time.monotonic()`` (not ``time.time()``) so NTP
        wall-clock adjustments cannot make a dead peer appear alive
        (backward step) or trigger a mass-liveness false-positive
        (forward step). The value is local-only and never serialized,
        so a monotonic timebase is safe.

        R2-LL-6: stored as a float (no truncation). The prior
        whole-second cast on the monotonic read quantised elapsed time,
        so a sub-second ``liveness_timeout`` would flip alive/dead at
        integer boundaries instead of at the configured threshold.
        """
        self.last_heartbeat = time.monotonic()

    async def update(self, response: dict, device_hostname: str):
        if response["hostname"] == device_hostname:
            self.enabled = False
            return

        self.hostname = response["hostname"]
        self.auto_discoverable = response["auto_discoverable"]

        await self.heartbeat()

    async def is_alive(self, timeout=30):
        """Returns True if last heartbeat was within timeout seconds.

        B-072 fix: short-circuit on None — a Node that has never
        received a heartbeat is not alive, regardless of timeout.
        Without this guard, ``time.monotonic() - None`` would raise
        TypeError.

        C-012: uses ``time.monotonic()`` paired with the write in
        :meth:`heartbeat`. Wall-clock-based liveness math is vulnerable
        to NTP step adjustments (backward step → ``is_alive`` returns
        True forever; forward step → mass false-positive).

        R2-LL-6: compare as float (no ``int()`` truncation). The prior
        cast quantised the delta to whole seconds, so sub-second
        ``timeout`` configs flipped alive/dead at integer boundaries.
        """
        if self.last_heartbeat is None:
            return False
        return (time.monotonic() - self.last_heartbeat) < timeout

    def is_alive_sync(self, timeout=30):
        """Returns True if last heartbeat was within timeout seconds.

        B-072 fix: short-circuit on None — symmetric with ``is_alive``.
        C-012: monotonic timebase — see :meth:`is_alive` for rationale.
        R2-LL-6: float compare — see :meth:`is_alive` for rationale.
        """
        if self.last_heartbeat is None:
            return False
        return (time.monotonic() - self.last_heartbeat) < timeout
