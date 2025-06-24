from utils import Plugin
import time 

class RemotePlugin(Plugin):
    def __init__(self, *args, remote=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.remote = remote


class Node:
    def __init__(self, ip: str, status: bool, plugins: list[RemotePlugin], extend_network: bool):
        self.ip = ip
        self.status = status
        self.last_heartbeat = int(time.time())
        self.plugins = plugins
        self.extend_network = extend_network

    def heartbeat(self):
        """Updates heartbeat timestamp"""
        self.last_heartbeat = int(time.time())

    def is_alive(self, timeout=30):
        """Returns True if last heartbeat was within timeout seconds"""
        return (int(time.time()) - self.last_heartbeat) < timeout

    def get_remote_plugins(self):
        return [p for p in self.plugins if p.remote]