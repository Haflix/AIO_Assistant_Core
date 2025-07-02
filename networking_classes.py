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
    def __init__(self, ip: str, hostname: str, alive: bool, enabled: bool, plugins: dict, discoverable: bool):
        self.ip = ip
        self.hostname = hostname
        self.alive = alive
        self.enabled = enabled
        self.last_heartbeat = int(time.time())
        self.plugins_lock = asyncio.Lock() #FIXME: Implement 
        self.plugins = plugins
        self.discoverable = discoverable

    async def _to_dict(self):
        """"""
        return

    async def heartbeat(self):
        """Updates heartbeat timestamp"""
        self.last_heartbeat = int(time.time())
        
    async def update(self, response: dict, device_hostname: str):
        if response["hostname"] == device_hostname:
            self.enabled = False
            return
        
        self.hostname = response["hostname"]
        self.discoverable = response["discoverable"]
        
        async with self.plugins_lock:
            self.plugins = response.get("plugins")
            
        await self.heartbeat()
        
        
    def add_remote_plugin(self):
        pass
    
    def update_remote_plugin(self):
        pass

    def is_alive(self, timeout=30):
        """Returns True if last heartbeat was within timeout seconds"""
        return (int(time.time()) - self.last_heartbeat) < timeout

    def get_remote_plugins(self):
        return [p for p in self.plugins if p.remote]