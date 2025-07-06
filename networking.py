from logging import Logger
from fastapi import FastAPI, Request
import uvicorn
import httpx
import socket
import asyncio
from decorators import async_log_errors, async_handle_errors
from exceptions import NetworkRequestException
from networking_classes import Node
from networking_classes import RemotePlugin
from typing import List, Union, Optional
    

class NetworkManager:
    def __init__(self, plugin_core, logger: Logger, node_ips: list, discover_nodes:bool, discoverable: bool, port=2510):
        self.plugin_core = plugin_core
        self._logger = logger
        
        
        self.node_ips = node_ips
        self.discover_nodes = discover_nodes
        self.discoverable = discoverable
        self.port = port
        self.nodes: list[Node] = []
        self.app = FastAPI()
        self._setup_routes()
    

    def _setup_routes(self):
        @self.app.get("/plugins")
        async def list_plugins():
            return {
            "plugins": [
                await plugin._to_dict()
                for plugin in self.plugin_core.plugins.values()
            ]
        }

        @self.app.get("/has_plugin")
        async def has_plugin(name: str, plugin_uuid: Union[str, None] = None):
            self._logger.debug(f"Remote request to find {name} {plugin_uuid}")
            
            plugin_uuid = plugin_uuid if plugin_uuid != "None" else None #NOTE: HORRIBLE SOLUTION
            plugin, node = await self.plugin_core.find_plugin(name=name, host="local", plugin_uuid=plugin_uuid)
            
            if not plugin:
                self._logger.debug(f"Couldnt find for remote request: {name} {plugin_uuid}")
            else:
                self._logger.debug(f"Found plugin for remote request: {plugin.plugin_name} {plugin.plugin_uuid}")
            return {
                "available": plugin is not None,
                "remote": plugin.remote if plugin else None,
                "hostname": self.plugin_core.hostname,
                "plugin_uuid": plugin.plugin_uuid if plugin else None
            }

        
        @self.app.get("/ping")
        async def ping():
            return {"status": "ok"}
        
        @self.app.post("/info")
        async def info(request: Request):
            
            data = await request.json()
            hostname = data.get("hostname")
            discover_nodes_info = data.get("discover_nodes_info")
            
            if type(hostname) != str:
                return NetworkRequestException("The var hostname is not the correct type. Must be str.")
            
            if type(discover_nodes_info) != bool:
                return NetworkRequestException("The var discover_nodes is not the correct type. Must be bool.")
            
            
            return {
                    "hostname": self.plugin_core.hostname,
                    "discoverable": self.discoverable,
                    "nodes": [await node._to_tuple() for node in self.nodes if node.discoverable and not node.hostname == hostname and discover_nodes_info and node.enabled and node.alive]
                    }

        @self.app.post("/execute")
        async def execute_plugin(request: Request):
            data = await request.json()
            plugin = data.get("plugin")
            method = data.get("method")
            args = data.get("args")
            plugin_uuid = data.get("plugin_uuid", None)
            author = data.get("author", "remote")
            author_id = data.get("author_id", "remote")
            timeout = data.get("timeout")
            author_host = data.get("author_host")

            try:
                result = await self.plugin_core.execute(
                    plugin, method, args, plugin_uuid, "local", author, author_id, timeout, author_host
                )
                
                return {"result": result, "error": False}
            except Exception as e:
                return {"result": e, "error": True}
            except NetworkRequestException as e:
                return {"result": e, "error": True}

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

    async def execute_remote(self, IP: str, plugin: str, method: str, args=None, plugin_uuid="", author="remote", author_id="remote", timeout=5):
        url = f"http://{IP}:{self.port}/execute"
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json={
                "plugin": plugin,
                "method": method,
                "args": args,
                "plugin_uuid": plugin_uuid,
                "author": author,
                "author_id": author_id,
                "timeout": timeout,
                "author_host": self.plugin_core.hostname
            })
            return response.json()

#    def execute_remote_sync(self, host: str, plugin: str, method: str, args=None, plugin_uuid="", author="remote", author_id="remote", timeout=5):
#        url = f"http://{host}:{self.port}/execute"
#        with httpx.Client(timeout=timeout) as client:
#            response = client.post(url, json={
#                "plugin": plugin,
#                "method": method,
#                "args": args,
#                "plugin_uuid": plugin_uuid,
#                "author": author,
#                "author_id": author_id,
#                "timeout": timeout
#            })
#            return response.json()

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

    @async_handle_errors(None)
    async def update_all_nodes(self, additional_IP_list: list[str] = [], timeout: int = 5, ignore_enabled_status: bool = False) -> List[Node]:
        
        self.node_ips.extend(additional_IP_list)
        
        await self._create_nodes(self.node_ips)
        

        tasks = [self.update_single(IP, timeout) for IP in self.node_ips if await self._get_node(IP).enabled or ignore_enabled_status]
        results = await asyncio.gather(*tasks)
        
        return self.nodes

    @async_log_errors
    async def update_single(self, IP: str, timeout: int = 5):
        
        await self._create_new_node(IP)
                
        try:
            response = await self._get_ip_info(IP, timeout=timeout)
            
            if not response:
                raise NetworkRequestException("Couldnt reach host")
            
            response = response.json()
            
            await (await self._get_node(IP)).update(response, self.plugin_core.hostname)
            
            for sub_node in response.get("nodes"):
                if sub_node[0] not in self.node_ips and sub_node[1] != self.plugin_core.hostname:
                    await self._add_ip(sub_node[0])
                    
                    await self._create_new_node(sub_node[0], sub_node[1])
                    
                    await self.update_single(sub_node[0])
                    
            self._logger.info(f"[DISCOVERY] Node found at {IP}")
                
        except Exception as e:
            self._logger.debug(f"[DISCOVERY] Failed to reach {IP}: {e}")


    @async_log_errors
    async def _add_ip(self, IP):
        await self.node_ips.append(IP)

    @async_log_errors
    async def _create_nodes(self, IP_list: list):
        for IP in IP_list:
            await self._create_new_node(IP)

    @async_log_errors
    async def _create_new_node(self, IP: str, hostname: Union[str, None] = None):
        if not await self.node_exists(IP):
            
            self.nodes.append(
                            Node(
                                IP=IP,
                                hostname=hostname,
                                enabled=True,
                                discoverable=False
                                )
                            )
    
    @async_handle_errors(None)
    async def _get_ip_info(self, IP: str, timeout: Union[int, float] = 5):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"http://{IP}:{self.port}/info",
                    json={
                        'hostname': self.plugin_core.hostname,
                        'discover_nodes_info': self.discover_nodes  
                    },
                    timeout=timeout
                )
        except Exception as e:
            self._logger.debug(f"[GET_INFO] Failed to reach {IP}: {e}")
            return
            
        return response
    
    @async_log_errors
    async def _delete_node(self, IP: str):
        self.nodes.remove(await self._get_node(IP))
        
    @async_log_errors
    async def _enable_node(self, IP: str):
        (await self._get_node(IP)).enabled = True
        
    @async_log_errors
    async def _disable_node(self, IP: str):
        (await self._get_node(IP)).enabled = False

    @async_log_errors
    async def node_exists(self, IP: str):
        for node in self.nodes:
            if node.IP == IP:
                return True
            
        return False
    
    @async_log_errors
    async def _get_node(self, IP: str, hostame: Union[str, None] = None, autogenerate: bool = False) -> Node:
        
        if autogenerate:
            await self._create_new_node(IP=IP, hostname=hostame)
        
        for node in self.nodes:
            if node.IP == IP:
                if node.hostname == hostame or hostame == None:
                    return node 

            
        self._logger.warning(f"A node with IP \"{IP}\" doesnt exist!")
        return None
    
    @async_log_errors
    async def _remoteplugin_from_dict(self, plugin_data: dict):
        return RemotePlugin(name=plugin_data["plugin_name"],
                                version=plugin_data["version"],
                                uuid=plugin_data["plugin_uuid"],
                                enabled=plugin_data["enabled"],
                                remote=plugin_data["remote"],
                                description=plugin_data["description"],
                                arguments=plugin_data.get("arguments", [])
                            )
    
    async def heartbeat_node(self, node: Node, timeout=5):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"http://{node.IP}:{self.port}/ping") #NOTE: Add hash to check for new plugins?
                if response.status_code == 200:
                    node.status = True
                    node.heartbeat()
                else:
                    node.status = False
        except:
            node.status = False

    @async_handle_errors(None)
    async def node_has_plugin(self, IP: str, plugin_name: str, plugin_uuid: Union[str, None] = None, timeout: float = 3.0) -> Optional[dict]:
        """
        Ask a node if it has the specified plugin.
        """
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                #plugin_uuid = plugin_uuid if not None else "None"
                #plugin_uuid = plugin_uuid or "None"

                if plugin_uuid:
                    response = await client.get(
                        f"http://{IP}:{self.port}/has_plugin",
                        params={"name": plugin_name, 
                                "plugin_uuid": plugin_uuid
                                }
                    )
                else:
                    response = await client.get(
                        f"http://{IP}:{self.port}/has_plugin",
                        params={"name": plugin_name
                                }
                    )
                if response.status_code == 200:
                    return response.json()
            except Exception:
                return None



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


        


#    async def find_plugin_on_nodes(self, plugin_name: str):
#        """Check all known nodes for the requested plugin."""
#        found_hosts = []
#        for node in self.nodes:
#            try:
#                async with httpx.AsyncClient(timeout=2.0) as client:
#                    response = await client.get(f"http://{node.ip}:{self.port}/plugins")
#                    if response.status_code == 200:
#                        if plugin_name in response.json():
#                            found_hosts.append(node)
#            except:
#                continue
#        return found_hosts
