import contextlib
import importlib
import inspect
import os
import asyncio
from typing import Any, Optional, Callable, Union, Dict, List
import yaml

from exceptions import NetworkRequestException, RequestException
from networking_classes import Node, RemotePlugin
from utils import LogUtil, Request, Plugin, ConfigUtil, GeneratorRequest
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors
from networking import NetworkManager

class PluginCore:
    """Manages all plugins and facilitates communication between them."""
    
    def __init__(self, config_path: str):
        self._logger = LogUtil.create(
                                    ConfigUtil.quickget_config(
                                                    config_path, 
                                                    {"general":
                                                        {"console_log_level": "DEBUG"}
                                                    }
                                                )["general"]["console_log_level"]
                                        )
        
        
        self.yaml_config = None
        self.config_path = config_path
        self.load_config_yaml(self.config_path)
        
        
        self.requests = {}
        self.request_lock = asyncio.Lock()
        self.task_list = []
        
        
        self.main_event_loop = asyncio.get_event_loop()
        self.plugins = {}
        self.plugin_lock = asyncio.Lock()
        self.seen_paths = []
        
        self.request_cleaner = asyncio.create_task(self.running_loop())
        
        # Start initialization
        self._init_task = asyncio.create_task(self.load_plugins())
        
        if self.networking_enabled:
            self.network = NetworkManager(
                self, 
                self._logger.getChild("networking"),
                node_ips=self.yaml_config.get('networking').get("node_ips", []),
                discover_nodes=self.yaml_config.get('networking').get("discover_nodes", False),
                direct_discoverable=self.networking_direct_discoverable,
                auto_discoverable=self.networking_auto_discoverable,
                port=self.networking_port 
                )
            asyncio.create_task(self.network.start())
    
    
    async def wait_until_ready(self):
        """Wait until the plugins have been reloaded."""
        await self._init_task
        
        #NOTE: Wait for enabled?
    
    @log_errors
    def load_config_yaml(self, config_path: str):
        self._logger.info(f"Loading config from config_path: {config_path}")
        self.yaml_config: dict = ConfigUtil.load_config(config_path)
        self._logger.info(self.yaml_config)
        
        ConfigUtil.check_config_integrity(self.yaml_config, self._logger)
        
        ConfigUtil.apply_configvalues(self)
    
    
    @async_log_errors
    async def load_plugins(self):
        # Load the plugins
        await self.get_plugins()
        
        # Enable them
        await self.start_plugins()
        
        self._logger.info(f"Finished Loading plugins!")
    
    
    @async_log_errors
    async def get_plugins(self) -> None:
        
        for plugin_entry in self.yaml_config.get("plugins", []):
            
            # Load and initiate the pluginclass
            await self.load_plugin_with_conf(plugin_entry)

    
    
    @async_log_errors
    async def start_plugins(self) -> None:
        """Start all plugin loops.
        """

        for plugin in self.plugins.values():
            if not plugin.enabled:
                try:
                    await self._enable_plugin(plugin.plugin_name)
                except Exception as error:
                    self._logger.warning(f"Error occured while enabling plugin with name \"{plugin.plugin_name}\": {type(error).__name__}: {error}")
                #task = asyncio.create_task(self._enable_plugin(plugin.plugin_name))
                #self.task_list.append(task)
    
    
    @async_log_errors
    async def load_plugin_with_conf(self, plugin_entry: list) -> None:

        # Get values
        name = plugin_entry["name"]
        
        
        if not plugin_entry.get("enabled"):
            self._logger.debug(f"Plugin \"{name}\" wont be loaded due to it being disabled")
            await self.pop_plugin(name)
            return
        
        if name in list(self.plugins.keys()):
            self._logger.info(f"Plugin \"{name}\" has an old instance, that will be overwritten")
            await self.pop_plugin(name)
        
        # Resolve plugin directory
        path = plugin_entry.get("path") or os.path.join(self.plugin_package, name)
        path = os.path.abspath(path)
        if not os.path.exists(path):
            self._logger.error(f"Plugin directory missing: {name} ({path})")
            await self.pop_plugin(name)
            return
        
        
        
        # Load plugin config
        try:
            with open(os.path.join(path, "plugin_config.yml"), "r") as f:
                plugin_config = yaml.safe_load(f)
        except Exception as e:
            self._logger.error(f"Failed loading config for {name}: {e}")
            await self.pop_plugin(name)
            return
        
        # Validate plugin config
        for field in ["description", "version", "remote", "arguments"]:
            if field not in plugin_config:
                self._logger.warning(f"{name} missing {field} in plugin_config.yml")
                #await self.pop_plugin(name)
                #continue
            
        
        
        
        # Dynamic import
        module_path = os.path.join(path, "plugin.py")
        spec = importlib.util.spec_from_file_location(name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        
        
        # Find first Plugin subclass
        plugin_class = next(
            cls for _, cls in inspect.getmembers(module, inspect.isclass)
            if issubclass(cls, Plugin) and cls != Plugin
        )
        
        
        # Instantiate with config
        plugin = plugin_class(
            self._logger.getChild(name),
            self,
            arguments=plugin_config["arguments"] if isinstance(plugin_config["arguments"], (list, dict, tuple)) else None
        )
        plugin.plugin_name = name  # Set name from main config
        plugin.version = plugin_config["version"] or "0.0.0 - not given"
        plugin.remote = plugin_config["remote"] or False
        plugin.arguments = plugin_config["arguments"] or None
        
        async with self.plugin_lock:
            self.plugins[name] = plugin
        
        self._logger.info(f"Successfully loaded plugin: {name} (Version: {plugin_config['version']}, Path: {path})")
    

    @async_log_errors
    async def pop_plugin(self, plugin_name: str) -> None:
        self._logger.info(f"Popping plugin: {plugin_name}")
        try:
            if plugin_name in list(self.plugins.keys()):
                if self.plugins[plugin_name].enabled:
                    await self._disable_plugin(plugin_name)
                async with self.plugin_lock:
                    self.plugins.pop(plugin_name) 
            else:
                self._logger.warning(f"Plugin with name \"{plugin_name}\" doesnt exist")   
        except Exception as error:
            raise Exception(f"Error while popping plugin \"{plugin_name}\": {error}")
    
    
    @async_log_errors
    async def purge_plugins(self):
        self._logger.info("Purging plugins")
        try:
            for plugin_name in list(self.plugins.keys()):
                await self._disable_plugin(plugin_name)
            async with self.plugin_lock:
                self.plugins.clear()
            self._logger.info("Purged all plugins")
        except Exception as error:
            raise Exception(f"Error while purging plugins: {error}")


    @async_handle_errors(None)
    async def _enable_plugin(self, plugin_name: str):
        """Method to enable a plugin.
        """
        async with self.plugin_lock:
            plugin = self.plugins[plugin_name]
            if not plugin.enabled:
                if asyncio.iscoroutinefunction(plugin.on_enable):
                    await plugin.on_enable()
                else:
                    await self.main_event_loop.run_in_executor(None, plugin.on_enable)  
                plugin.enabled = True
    
    
    @async_log_errors
    async def _disable_plugin(self, plugin_name: str):
        """#TODO: Write this
        """
        async with self.plugin_lock:
            plugin = self.plugins[plugin_name]
            if plugin.enabled:
                if asyncio.iscoroutinefunction(plugin.on_disable):
                    await plugin.on_disable()
                else:
                    await self.main_event_loop.run_in_executor(None, plugin.on_disable) 
                plugin.enabled = False
                
                
                
    @async_handle_errors(None)
    async def _reload_plugin(self, plugin_name: str):
        """#TODO: Write this
        """
        
        self.pop_plugin(plugin_name)
        
        self.load_plugin_with_conf(self.yaml_config["plugins"][plugin_name])

    
    
    @contextlib.asynccontextmanager
    async def request_context_async(self, request: Request):
        """Async context manager to handle requests."""
        try:
            result, error, timed_out = await request.wait_for_result_async()
            if error:
                raise Exception(f"Request {request.id} failed: {request.result}")
            yield result
        finally:
            request.set_collected()
    
    @contextlib.contextmanager
    def request_context_sync(self, request: Request):
        """Sync context manager to handle requests."""
        try:
            result = request.get_result_sync()
            if request.error:
                raise Exception(f"Request failed: {request.result}")
            yield result
        finally:
            request.set_collected()
    
    @async_log_errors
    async def create_request(self, 
                            plugin: str,
                            method: str,
                            args: Any = None,
                            plugin_uuid: Optional[str] = "",
                            host: str = "any",  # "any", "remote", "local", or hostname
                            author: str = "system",
                            author_id: str = "system",
                            timeout: Union[float, tuple] = None,
                            author_host: str = None,
                            request_id: str = None
                            ) -> Request:
        """Create a new request asynchronously."""
        
        if author_host == None:
            author_host = self.hostname
        
        request = Request(author_host, plugin, method, args, plugin_uuid, host, author, author_id, timeout, request_id, self.main_event_loop)
        
        async with self.request_lock:
            self.requests[request.id] = request
        
        self._logger.debug(f"Request {request.id} created by {author} targeting {plugin}.{method}")
        
        task = asyncio.create_task(self._process_request(request))
        self.task_list.append(task)
        
        return request
    
    @log_errors
    def create_request_sync(self, 
                            plugin: str,
                            method: str,
                            args: Any = None,
                            plugin_uuid: Optional[str] = "",
                            host: str = "any",  # "any", "remote", "local", or hostname
                            author: str = "system",
                            author_id: str = "system",
                            timeout: Union[float, tuple] = None,
                            author_host: str = None,
                            request_id: str = None
                            ) -> Request:
        """Create a new request synchronously."""
        coro = self.create_request(plugin, method, args, plugin_uuid, host, author, author_id, timeout, author_host, request_id)
        future = asyncio.run_coroutine_threadsafe(coro, self.main_event_loop)
        return future.result()
    
    @async_log_errors
    async def create_gen_request(self, 
                            plugin: str,
                            method: str,
                            args: Any = None,
                            plugin_uuid: Optional[str] = "",
                            host: str = "any",  # "any", "remote", "local", or hostname
                            author: str = "system",
                            author_id: str = "system",
                            timeout: Union[float, tuple] = None,
                            author_host: str = None,
                            request_id: str = None
                            ) -> GeneratorRequest:
        """Create a new request asynchronously."""
        
        if author_host == None:
            author_host = self.hostname
        
        request = GeneratorRequest(author_host, plugin, method, args, plugin_uuid, host, author, author_id, timeout, request_id, self.main_event_loop)
        
        async with self.request_lock:
            self.requests[request.id] = request
        
        self._logger.debug(f"GeneratorRequest {request.id} created by {author} targeting {plugin}.{method}")
        
        task = asyncio.create_task(self._process_request_stream(request))
        self.task_list.append(task)
        
        return request
    
    @log_errors
    def create_gen_request_sync(self, 
                            plugin: str,
                            method: str,
                            args: Any = None,
                            plugin_uuid: Optional[str] = "",
                            host: str = "any",  # "any", "remote", "local", or hostname
                            author: str = "system",
                            author_id: str = "system",
                            timeout: Union[float, tuple] = None,
                            author_host: str = None,
                            request_id: str = None
                            ) -> Request:
        """Create a new request synchronously."""
        coro = self.create_gen_request(plugin, method, args, plugin_uuid, host, author, author_id, timeout, author_host, request_id)
        future = asyncio.run_coroutine_threadsafe(coro, self.main_event_loop)
        return future.result()
    
    @async_log_errors
    async def find_plugin(self, name: str, host: str = "any", plugin_uuid: Union[str, None] = None) -> Optional[Union[Plugin, tuple]]:
        #TODO: Add remote search, search by host & search by id 
        # check if other plugin meets:
        #   remote == True
        #   node.enabled = True
        #   node.is_alive() == True
        #   --> iterate nodes and call something like find_plugin?

        """
        Tries to find a plugin locally first, then on remote nodes if networking is enabled.
        """
        # check locally first
        if host in ["local", "any", self.hostname]:
            for plugin in self.plugins.values():
                plugin: Plugin

                if plugin.plugin_name == name and (not plugin_uuid or plugin.plugin_uuid == plugin_uuid):
                    #if plugin.plugin_name == name and (not plugin_uuid or plugin.plugin_uuid == plugin_uuid):
                    return plugin, None


        # if not found ask remote nodes
        if self.networking_enabled and host != "local" and host != self.hostname:
            for node in self.network.nodes:
                node: Node
                if not node.enabled or not await node.is_alive():
                    continue

                if host in ["remote", "any"] or node.hostname == host:


                    result = await self.network.node_has_plugin(node.IP, name, plugin_uuid)
                    if result and result.get("available") and result.get("remote"):
                        return RemotePlugin(
                            name=name,
                            version="unknown",  # could fetch real version later
                            uuid=result.get("plugin_uuid"),
                            enabled=True,
                            remote=True,
                            description="Remote plugin",
                            arguments=[],
                            hostname=result.get("hostname", node.hostname)
                        ), node

        return None, None
    
    
    @async_handle_errors(None)
    async def _process_request(self, request: Request) -> None:
        """Process a request by invoking the target plugin method."""
        plugin_name = request.target_plugin
        function_name = request.target_method
        
        plugin, node = await self.find_plugin(plugin_name, request.target_host, request.target_plugin_uuid)
        
        

        if not plugin:
            await self._set_request_result(request, f"Plugin {plugin_name} not found", True)
            return
        
        
        
        host = f"(local) {self.hostname}" if isinstance(plugin, Plugin) else f"{node.IP}#{node.hostname}"
        self._logger.debug(f"Found {plugin_name} (ID: {plugin.plugin_uuid}) for Request with ID {request.id} on host {host}")
        
        if isinstance(plugin, RemotePlugin):#FIXME: Fix the timeout thing. Warn if ping is higher than timeout
            #FIXME: Streams dont work remotely yet: gotta do it with the async start_server thingy i think
            result = await self.network.execute_remote(IP=node.IP,
                                        plugin=plugin_name,
                                        method=function_name,
                                        args=request.args,
                                        plugin_uuid=request.target_plugin_uuid,
                                        author=f"{self.hostname} - {request.author}#{request.author_id}",
                                        author_id=request.author_id,
                                        timeout=(request.timeout_duration, request.created_at),
                                        request_id=request.id
                                        )
        
        else:
            #if not plugin.remote and request.author_id:
            #    await self._set_request_result(request, NetworkRequestException(f"Plugin {plugin_name} is not accessable anymore"), True)
            #    return
            func = getattr(plugin, function_name, None)
            if not callable(func):# or not inspect.isfunction(func):
                await self._set_request_result(request, f"Function {function_name} not found in plugin {plugin_name}", True)
                return

            
            if asyncio.iscoroutinefunction(func):
                result = await func(request.args)
            elif inspect.isasyncgenfunction(func):
                #async for
                raise Exception(f"For Request {request.id}: The method you requested is an async-generator. Use execute_stream for generators")
                #FIXME
                #async for result in func(request.args):
                    #pass
            elif inspect.isgeneratorfunction(func):
                raise Exception(f"For Request {request.id}: The method you requested is a sync-generator. Use execute_stream for generators")
                #FIXME
                #pass
            else:
                result = await self.main_event_loop.run_in_executor(None, func, request.args)


        await self._set_request_result(request, result)
        
    #@async_handle_errors(None)
    async def _process_request_stream(self, request: GeneratorRequest) -> None:
        """Process a request by invoking the target plugin method."""
        plugin_name = request.target_plugin
        function_name = request.target_method
        
        plugin, node = await self.find_plugin(plugin_name, request.target_host, request.target_plugin_uuid)
        
        

        if not plugin:
            await self._set_gen_request_result(request, f"Plugin {plugin_name} not found", True)
            return
        
        
        
        host = f"(local) {self.hostname}" if isinstance(plugin, Plugin) else f"{node.IP}#{node.hostname}"
        self._logger.debug(f"Found {plugin_name} (ID: {plugin.plugin_uuid}) for Request with ID {request.id} on host {host}")
        
        if isinstance(plugin, RemotePlugin):
            async for result in self.network.execute_remote_stream(IP=node.IP,
                                        plugin=plugin_name,
                                        method=function_name,
                                        args=request.args,
                                        plugin_uuid=request.target_plugin_uuid,
                                        author=f"{self.hostname} - {request.author}#{request.author_id}",
                                        author_id=request.author_id,
                                        timeout=(request.timeout_duration, request.created_at),
                                        request_id=request.id
                                        ):
                await request.queue.put(result)#FIXME
        
        else:
            #if not plugin.remote and request.author_id:
            #    await self._set_request_result(request, NetworkRequestException(f"Plugin {plugin_name} is not accessable anymore"), True)
            #    return
            func = getattr(plugin, function_name, None)
            if not callable(func):# or not inspect.isfunction(func):
                #await request.queue.put((result, False, False))
                await self._set_gen_request_result(request, f"Function {function_name} not found in plugin {plugin_name}", True)
                return

            
            if asyncio.iscoroutinefunction(func):
                await self._set_gen_request_result(request, f"For Request {request.id}: The method you requested is a non-generator async function. Use execute for non-generators", True)
                return
            
            elif inspect.isasyncgenfunction(func):
                async for result in func(request.args):
                    await request.queue.put((result, False, False))#FIXME Add in utils
                    
            elif inspect.isgeneratorfunction(func):
                generator = func(request.args)
                sentinel = object()
                while True:
                    result = await asyncio.to_thread(next, generator, sentinel)
                    if result is sentinel:
                        break
                    await request.queue.put((result, False, False))#FIXME Add in utils

            else:
                await self._set_gen_request_result(request, f"For Request {request.id}: The method you requested is a non-generator sync function. Use execute for non-generators", True)
                return
                #result = await self.main_event_loop.run_in_executor(None, func, request.args)


        await self._set_gen_request_result(request)
    
    @async_handle_errors(None)
    async def _set_request_result(self, request: Request, result: Any, error: bool = False) -> None:
        """Set the result of a request."""
        if isinstance(result, asyncio.Future):
            result = await result
        await request.set_result(result, error)
    
    @async_handle_errors(None)
    async def _set_gen_request_result(self, request: GeneratorRequest, result: Any = None, error: bool = False) -> None:
        """Set the result of a request."""
        
        await request.set_result(result, error)
    
    async def running_loop(self):
        """Maintenance loop that cleans up tasks and requests."""
        while True:
            self.task_list = [t for t in self.task_list if not t.done()]
            await self.cleanup_requests()
            await asyncio.sleep(10)
    
    @async_log_errors
    async def cleanup_requests(self):
        """Remove collected requests."""
        async with self.request_lock:
            self.requests = {rid: req for rid, req in self.requests.items() if not req.collected}
    
    # One-liner methods for plugin communication
    #@async_handle_errors(default_return=None)
    #async def execute(self, target: str, args: Any = None, author: str = "system", timeout: Optional[float] = None) -> Any:
    @async_handle_errors(default_return=None)
    async def execute(self,
        plugin: str,
        method: str,
        args: Any = None,
        plugin_uuid: Optional[str] = "",
        host: str = "any",  # "any", "remote", "local", or host_uuid
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None
        ) -> Any:
        """
        One-liner to execute a plugin method and get its result with built-in error handling.
        This combines request creation, processing, and result retrieval in one method.
        
        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds
            
        Returns:
            The result from the plugin method or None if any error occurs
        """
        request = await self.create_request(plugin, method, args, plugin_uuid, host, author, author_id, timeout, author_host, request_id)
        result, error, _ = await request.wait_for_result_async()
        await request.set_collected()  # Mark for cleanup
        
        if error:
            self._logger.warning(f"Error executing {plugin}.{method} (Req-ID: {request.id}): {result}. You can check the logs for this Req-ID.")
            raise RequestException(result)
        return result
    
    @handle_errors(default_return=None)
    def execute_sync(self,
        plugin: str,
        method: str,
        args: Any = None,
        plugin_uuid: Optional[str] = "",
        host: str = "any",  # "any", "remote", "local", or host_uuid
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None
    ) -> Any:
        """
        Synchronous one-liner to execute a plugin method with built-in error handling.
        
        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds
            
        Returns:
            The result from the plugin method or None if any error occurs
        """
        
        future = asyncio.run_coroutine_threadsafe(
            self.execute(plugin, method, args, plugin_uuid, host, author, author_id, timeout, author_host, request_id),
            self.main_event_loop
        )
        return future.result()
    
    #@async_handle_errors(default_return=None)
    async def execute_stream(self,
        plugin: str,
        method: str,
        args: Any = None,
        plugin_uuid: Optional[str] = "",
        host: str = "any",  # "any", "remote", "local", or host_uuid
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None
        ) -> Any:
        """
        One-liner to execute a plugin method and get its result with built-in error handling.
        This combines request creation, processing, and result retrieval in one method.
        
        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds
            
        Returns:
            The result from the plugin method or None if any error occurs
        """
        request = await self.create_gen_request(plugin, method, args, plugin_uuid, host, author, author_id, timeout, author_host, request_id)
        async for result, error, _ in request.get_queue_stream():

            if error:#FIXME DOESNT DO SHIT
                self._logger.warning(f"Error executing {plugin}.{method} (GenReq-ID: {request.id}): {result}. You can check the logs for this Req-ID.")
                raise RequestException(result)
            yield result
        
        await request.set_collected()  # Mark for cleanup
    
    #@handle_errors(default_return=None)
    def execute_stream_sync(self,
        plugin: str,
        method: str,
        args: Any = None,
        plugin_uuid: Optional[str] = "",
        host: str = "any",  # "any", "remote", "local", or host_uuid
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None
    ) -> Any:
        """
        Synchronous one-liner to execute a plugin method with built-in error handling.
        
        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds
            
        Returns:
            The result from the plugin method or None if any error occurs
        """
        #FIXME Still wrong
        future = asyncio.run_coroutine_threadsafe(
            self.execute_stream(plugin, method, args, plugin_uuid, host, author, author_id, timeout, author_host, request_id),
            self.main_event_loop
        )
        return future.result()