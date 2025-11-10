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
    def __init__(self, IP: str, hostname: str, enabled: bool, auto_discoverable: bool):
        """Node class for the networking system that represents another device with this script running

        Args:
            IP (str): The IP address of the node
            hostname (str): The hostname of the node
            enabled (bool): Whether the node is enabled
            auto_discoverable (bool): Whether the node is auto-discoverable (can be discovered by other nodes with the automatic discovery feature)
        """
        self.IP = IP
        self.hostname = hostname
        self.enabled = enabled
        self.last_heartbeat = 0
        self.auto_discoverable = auto_discoverable

    def __str__(self) -> str:
        """String-representation of a node"""
        return f"   IP: {self.IP}\n     Hostname: {self.hostname}\n     Enabled: {self.enabled}\n    Last Heartbeat: {self.is_alive_sync()}\n     Discoverable: {self.auto_discoverable}\n"

    async def _to_tuple(self) -> Tuple[str, str]:
        """
        Returns:
            Tuple[str, str]: A tuple where:
                - The first element is the node's IP address.
                - The second element is the node's hostname.
        """
        return (self.IP, self.hostname)

    async def heartbeat(self):
        """Updates heartbeat timestamp"""
        self.last_heartbeat = int(time.time())

    async def update(self, response: dict, device_hostname: str):
        if response["hostname"] == device_hostname:
            self.enabled = False
            return

        self.hostname = response["hostname"]
        self.auto_discoverable = response["auto_discoverable"]

        await self.heartbeat()

    async def is_alive(self, timeout=30):
        """Returns True if last heartbeat was within timeout seconds"""
        return (int(time.time()) - self.last_heartbeat) < timeout

    def is_alive_sync(self, timeout=30):
        """Returns True if last heartbeat was within timeout seconds"""
        return (int(time.time()) - self.last_heartbeat) < timeout
