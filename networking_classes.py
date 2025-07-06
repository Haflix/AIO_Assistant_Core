import time
import asyncio
from typing import Any, Optional, Tuple, Union

class RemotePlugin:
    def __init__(self, name: str, version: str, uuid: str, enabled: bool, remote: bool, description: str, arguments: Union[list, dict, tuple], hostname: str):
        self.plugin_name = name
        self.version = version
        self.plugin_uuid = uuid
        self.enabled = enabled
        self.remote = remote
        self.description = description
        self.arguments = arguments
        self.hostname = hostname
        
#    def to_dict(self):
#        return {
#            "name": self.plugin_name,
#            "version": self.version,
#            "uuid": self.plugin_uuid,
#            "enabled": self.enabled,
#            "remote": self.remote,
#            "description": self.description,
#            "arguments": self.arguments,
#            "hostname": self.
#        }




class Node:
    def __init__(self, IP: str, hostname: str, enabled: bool, discoverable: bool):
        self.IP = IP
        self.hostname = hostname
        self.enabled = enabled
        self.last_heartbeat = 0
        self.discoverable = discoverable

    def __str__(self) -> str:
        """String-representation of a node"""
        return f"   IP: {self.IP}\n     Hostname: {self.hostname}\n     Enabled: {self.enabled}\n    Last Heartbeat: {self.is_alive_sync()}\n     Discoverable: {self.discoverable}\n"

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
        self.discoverable = response["discoverable"]
            
        await self.heartbeat()
        
        
    def add_remote_plugin(self):
        pass
    
    def update_remote_plugin(self):
        pass

    async def is_alive(self, timeout=30):
        """Returns True if last heartbeat was within timeout seconds"""
        #return (int(time.time()) - self.last_heartbeat) < timeout
        return True #FIXME: Please PLEASE 🙏🙏 remove later
    
    def is_alive_sync(self, timeout=30):
        """Returns True if last heartbeat was within timeout seconds"""
        #return (int(time.time()) - self.last_heartbeat) < timeout
        return True #FIXME: Please PLEASE 🙏🙏 remove later

    def get_remote_plugins(self):
        return [p for p in self.plugins if p.remote]