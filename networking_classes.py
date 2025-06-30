import time
import asyncio
from typing import Any, Optional, Tuple, Union

class RemotePlugin:
    def __init__(self, name: str, version: str, uuid: str, enabled: bool, remote: bool, description: str, arguments: Union[list, dict, tuple]): #🤌
        self.plugin_name = name
        self.version = version
        self.plugin_uuid = uuid
        self.enabled = enabled
        self.remote = remote
        self.description = description
        self.arguments = arguments
        
    def to_dict(self):
        return {
            "name": self.plugin_name,
            "version": self.version,
            "uuid": self.plugin_uuid,
            "enabled": self.enabled,
            "remote": self.remote,
            "description": self.description,
            "arguments": self.arguments,
        }




class Node:
    def __init__(self, ip: str, status: bool, plugins: list[RemotePlugin], extend_network: bool):
        self.ip = ip
        self.status = status
        self.last_heartbeat = int(time.time())
        self.plugins_lock = asyncio.Lock() #NOTE: Implement 
        self.plugins = plugins
        self.extend_network = extend_network

    def heartbeat(self):
        """Updates heartbeat timestamp"""
        self.last_heartbeat = int(time.time())
        
    def add_remote_plugin(self):
        pass
    
    def update_remote_plugin(self):
        pass

    def is_alive(self, timeout=30):
        """Returns True if last heartbeat was within timeout seconds"""
        return (int(time.time()) - self.last_heartbeat) < timeout

    def get_remote_plugins(self):
        return [p for p in self.plugins if p.remote]