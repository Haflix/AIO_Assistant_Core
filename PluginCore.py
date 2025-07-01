import contextlib
import importlib
import inspect
import os
import asyncio
from typing import Any, Optional, Callable, Union, Dict, List
import yaml

from exceptions import RequestException
from networking_classes import RemotePlugin
from utils import LogUtil, Request, Plugin, ConfigUtil
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
                network_ip=self.networking_network_ip,#FIXME: from config as well
                port=self.networking_port #FIXME: from config as well
                )
            asyncio.create_task(self.network.start())
    
    
    async def wait_until_ready(self):
        """Wait until the plugins have been reloaded."""
        await self._init_task
        if True: #FIXME: Will be replaced by something like self.config["networking"]["enabled"]: boolean
            pass
            #await self._init_network_task
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
        
        #async with self.plugin_lock:
        #plugin = self.plugins[plugin_name]
            
        #TODO: Check if plugin is already enabled
        
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
                            plugin_id: Optional[str] = "",
                            host: str = "any",  # "any", "remote", "local", or hostname
                            author: str = "system",
                            author_id: str = "system",
                            timeout: Optional[float] = None
                            ) -> Request:
        """Create a new request asynchronously."""
        #request = Request(self.hostname, author, target, args, timeout, self.main_event_loop)
        request = Request(self.hostname, plugin, method, args, plugin_id, host, author, author_id, timeout, self.main_event_loop)
        
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
                            plugin_id: Optional[str] = "",
                            host: str = "any",  # "any", "remote", "local", or hostname
                            author: str = "system",
                            author_id: str = "system",
                            timeout: Optional[float] = None
                            ) -> Request:
        """Create a new request synchronously."""
        coro = self.create_request(plugin, method, args, plugin_id, host, author, author_id, timeout)
        future = asyncio.run_coroutine_threadsafe(coro, self.main_event_loop)
        return future.result()
    
    @async_log_errors
    async def find_plugin(self, name: str) -> Plugin:
        #TODO: Add remote search, search by host & search by id 
        # check if other plugin meets:
        #   remote == True
        #   node gotta be active
        #   wait! find plugin inside of network manager cause of lock? (node lock & plugin lock)
    
        plg = None
        
        for plugin in self.plugins.values():
            if plugin.plugin_name == name:
                plg = plugin

        if self.networking_enabled:
            pass #NOTE: Add search for RemotePlugin s here
        
        return plg
    
    #@async_log_errors
    #async def _sub_find_plugin(self, plugins)
    
    @async_handle_errors(None)
    async def _process_request(self, request: Request) -> None:
        """Process a request by invoking the target plugin method."""
        plugin_name = request.target_plugin
        function_name = request.target_method
        
        plugin = await self.find_plugin(plugin_name)
        
        #FIXME: execute_remote
        # Check if None, Plugin or RemotePlugin
        
        if not plugin:
            await self._set_request_result(request, f"Plugin {plugin_name} not found", True)
            return
        
        self._logger.debug(f"Found {plugin.plugin_name} (ID: {plugin.plugin_uuid}) for Request with ID {request.id}")
        
        if isinstance(plugin, RemotePlugin):
            
        
        func = getattr(plugin, function_name, None)
        if not callable(func):
            await self._set_request_result(request, f"Function {function_name} not found in plugin {plugin_name}", True)
            return
        
        
        if asyncio.iscoroutinefunction(func):
            result = await func(request.args)
        else:
            result = await self.main_event_loop.run_in_executor(None, func, request.args)
        
        await self._set_request_result(request, result)
    
    @async_handle_errors(None)
    async def _set_request_result(self, request: Request, result: Any, error: bool = False) -> None:
        """Set the result of a request."""
        if isinstance(result, asyncio.Future):
            result = await result
        request.set_result(result, error)
    
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
        plugin_id: Optional[str] = "",
        host: str = "any",  # "any", "remote", "local", or hostname
        author: str = "system",
        author_id: str = "system",
        timeout: Optional[float] = None
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
        request = await self.create_request(plugin, method, args, plugin_id, host, author, author_id, timeout)
        result, error, _ = await request.wait_for_result_async()
        request.set_collected()  # Mark for cleanup
        
        if error:
            self._logger.warning(f"Error executing {plugin}.{method} (Req-ID: {request.id}): {result}. You can check the logs for this Req-ID.")
            raise RequestException(result)
        return result
    
    @handle_errors(default_return=None)
    def execute_sync(self,
        plugin: str,
        method: str,
        args: Any = None,
        plugin_id: Optional[str] = "",
        host: str = "any",  # "any", "remote", "local", or hostname
        author: str = "system",
        author_id: str = "system",
        timeout: Optional[float] = None
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
            self.execute(plugin, method, args, plugin_id, host, author, author_id, timeout),
            self.main_event_loop
        )
        return future.result()