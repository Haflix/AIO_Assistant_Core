from logging import Logger
from fastapi import FastAPI, Request
import uvicorn
import httpx
import socket
import ipaddress
import asyncio
from decorators import *


class NetworkManager:
    def __init__(self, plugin_core, logger: Logger, network_ip: str = socket.gethostbyname(socket.gethostname()), port=2510):
        self.plugin_core = plugin_core
        self._logger = logger
        self.network_ip = network_ip
        self.port = port
        self.nodes = []
        self.app = FastAPI()
        self._setup_routes()

    def _setup_routes(self):
        @self.app.get("/plugins")
        async def list_plugins():
            return list(self.plugin_core.plugins.keys())

        @self.app.post("/execute")
        async def execute_plugin(request: Request):
            data = await request.json()
            plugin = data.get("plugin")
            method = data.get("method")
            args = data.get("args")
            plugin_id = data.get("plugin_id", "")
            author = data.get("author", "remote")
            author_id = data.get("author_id", "remote")
            timeout = data.get("timeout")

            try:
                result = await self.plugin_core.execute(
                    plugin, method, args, plugin_id, "local", author, author_id, timeout
                )
                return {"result": result, "error": False}
            except Exception as e:
                return {"result": str(e), "error": True}

    async def start(self):
        """Starts FastAPI server without blocking the main loop."""
        config = uvicorn.Config(
            self.app,
            host="0.0.0.0",
            port=self.port,
            log_level="info",
            loop="asyncio",
            lifespan="on",
        )
        server = uvicorn.Server(config)

        await server.serve()
        #await asyncio.to_thread(server.run)

    async def execute_remote(self, host: str, plugin: str, method: str, args=None, plugin_id="", author="remote", author_id="remote", timeout=5):
        url = f"http://{host}:{self.port}/execute"
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json={
                "plugin": plugin,
                "method": method,
                "args": args,
                "plugin_id": plugin_id,
                "author": author,
                "author_id": author_id,
                "timeout": timeout
            })
            return response.json()

    def execute_remote_sync(self, host: str, plugin: str, method: str, args=None, plugin_id="", author="remote", author_id="remote", timeout=5):
        url = f"http://{host}:{self.port}/execute"
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json={
                "plugin": plugin,
                "method": method,
                "args": args,
                "plugin_id": plugin_id,
                "author": author,
                "author_id": author_id,
                "timeout": timeout
            })
            return response.json()

#    async def discover_nodes(self, cidr_range=None):
#        if not cidr_range:
#            hostname = socket.gethostname()
#            local_ip = socket.gethostbyname(hostname)
#            cidr_range = ipaddress.ip_network(local_ip + '/24', strict=False)
#
#        sem = asyncio.Semaphore(20)  # Limit to 20 requests at a time for testing
#
#        async def probe(ip):
#            async with sem:
#                try:
#                    async with httpx.AsyncClient(timeout=1.0) as client:
#                        response = await client.get(f"http://{ip}:{self.port}/plugins")
#                        if response.status_code == 200:
#                            return str(ip)
#                except:
#                    return None
#
#        results = await asyncio.gather(*(probe(ip) for ip in cidr_range.hosts()))
#        self.nodes = [ip for ip in results if ip]
#        return self.nodes

    async def discover_nodes(self, extend_network, IP_list):
        """
        1. Get theoretical host list (with each host being a child of the Host class)
        2. Pass it to method that checks one of them at a time --> returns availability 
        --> (if extend_network is true, then get the following stuff) list of hosts that have extend network enabled as well from their uvicorn or something like that + all the plugins that it has from uvicorn with their parameters 
        3. Change the status of the available hosts to true
        4. Pass the new hosts from extend network to the host registry and go back to step 2
        """
        pass


#    async def discover_nodes(self, cidr_range=None, timeout=10):
#        if cidr_range is None:
#            #hostname = socket.gethostname()
#            #local_ip = socket.gethostbyname(hostname)
#            networks = [ipaddress.ip_network(f"{self.network_ip}/24", strict=False)]
#        else:
#            networks = [ipaddress.ip_network(cidr_range, strict=False)]
#
#        ips_to_scan = [str(ip) for network in networks for ip in network.hosts()]
#        sem = asyncio.Semaphore(20)
#        active_nodes = []
#
#        @async_handle_errors(None)
#        async def _probe_node(ip, client, sem):
#            async with sem:
#                try:
#                    self._logger.debug(f"Trying to find node on http://{ip}:{self.port}/plugins")
#                    response = await client.get(
#                        f"http://{ip}:{self.port}/plugins"
#                    )
#                    if response.status_code == 200:
#                        return ip
#                except (httpx.ConnectError, httpx.TimeoutException):
#                    pass
#                except Exception:
#                    self._logger.warning(f"Exception while trying to find node on http://{ip}:{self.port}/plugins: ")
#                    pass
#            return None
#
#        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout=timeout)) as client:
#        #async with httpx.AsyncClient(timeout=httpx.Timeout(connect=0.1, read=0.2, write=0.2, pool=0.5)) as client:
#            tasks = [asyncio.create_task(_probe_node(ip, client, sem)) for ip in ips_to_scan]
#
#            for task in asyncio.as_completed(tasks):
#                result = await task
#                if result:
#                    active_nodes.append(result)
#                    self._logger.info(f"[DISCOVERY] Found active node: {result}")
#
#        self.nodes = active_nodes
#        return active_nodes


        


    async def find_plugin_on_nodes(self, plugin_name: str):
        """Check all known nodes for the requested plugin."""
        found_hosts = []
        for node in self.nodes:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.get(f"http://{node}:{self.port}/plugins")
                    if response.status_code == 200:
                        if plugin_name in response.json():
                            found_hosts.append(node)
            except:
                continue
        return found_hosts
