import contextlib
import functools
import importlib
import inspect
import os
import pkgutil
from uuid import uuid4
import asyncio
import socket
from typing import Any, Optional, Callable, Union, Dict, List
import yaml
from utils import LogUtil, Request, Plugin, ConfigUtil
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors

class PluginCollection:
    """Manages all plugins and facilitates communication between them."""
    
    def __init__(self, config_path: str):
        self._logger = LogUtil.create("DEBUG")
        
        
        self.yaml_config = None
        self.load_config_yaml(config_path)
        
        
        self.requests = {}
        self.request_lock = asyncio.Lock()
        self.task_list = []
        
        
        self.main_event_loop = asyncio.get_event_loop()
        self.plugins = {}
        self.seen_paths = []
        
        # Start initialization
        self._init_task = asyncio.create_task(self.load_plugins())
    
    async def wait_until_ready(self):
        """Wait until the plugins have been reloaded."""
        await self._init_task
    
    @log_errors
    def load_config_yaml(self, config_path: str):
        self.yaml_config: dict = ConfigUtil.load_config(config_path)
        self._logger.info(self.yaml_config)
        
        ConfigUtil.check_config_integrity(self.yaml_config, self._logger)
        
        ConfigUtil.apply_configvalues(self)
    
    @async_log_errors
    async def load_plugins(self) -> None:
        self.plugins = {}

        for plugin_entry in self.yaml_config.get("plugins", []):
            # Skip disabled plugins
            if not plugin_entry.get("enabled"):
                continue

            name = plugin_entry["name"]
            expected_version = plugin_entry.get("version")
            arguments = plugin_entry.get("arguments") or None

            # Resolve plugin directory
            path = plugin_entry.get("path") or os.path.join(self.plugin_package, name)
            path = os.path.abspath(path)

            if not os.path.exists(path):
                self._logger.error(f"Plugin directory missing: {name} ({path})")
                continue

            # Load plugin config
            try:
                with open(os.path.join(path, "plugin_config.yml"), "r") as f:
                    plugin_config = yaml.safe_load(f)
            except Exception as e:
                self._logger.error(f"Failed loading config for {name}: {e}")
                continue

            # Validate plugin config
            for field in ["description", "version", "asynced", "loop_req"]:
                if field not in plugin_config:
                    self._logger.error(f"{name} missing {field} in plugin_config.yml")
                    continue

            # Version check (only if specified in main config)
            if expected_version and plugin_config["version"] != expected_version:
                self._logger.error(f"{name} version mismatch: {expected_version}≠{plugin_config['version']}")
                continue

            # Dynamic import
            #try:
            if 1 == 1:
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
                    *arguments if isinstance(arguments, (list, tuple)) else [],  # Unpack list/tuple if applicable
                    **arguments if isinstance(arguments, dict) else {}  # Unpack dict if applicable
                )
                plugin.plugin_name = name  # Set name from main config
                plugin.version = plugin_config["version"]
                plugin.asynced = plugin_config["asynced"]
                self.plugins[name] = plugin
                
                self._logger.info(f"Successfully loaded plugin: {name} (Version: {plugin_config['version']}, Path: {path})")

            #except Exception as e:
            #    self._logger.error(f"Failed loading {name}: {e}")
            #    continue

        await self.start_loops()
    
    
    @async_log_errors
    async def start_loops(self) -> None:
        """Start all plugin loops.
        """
        
        for plugin in self.plugins.values():
            if plugin.loop_req and not plugin.loop_running:
                task = asyncio.create_task(self._start_plugin_loop(plugin))
                self.task_list.append(task)
    
    
    @async_handle_errors(None)
    async def _start_plugin_loop(self, plugin: Plugin):
        """Helper method to start a plugin's loop.
        """
        
        if plugin.asynced:
            await plugin.loop_start()
        else:
            await self.main_event_loop.run_in_executor(None, plugin.loop_start)
    
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
        
        self._logger.info(f"Request {request.id} created by {author} targeting {plugin}.{method}")
        task = asyncio.create_task(self._process_request(request))
        self.task_list.append(task)
        
        return request
    
#    @log_errors
#    def create_request_sync(self, 
#                          author: str, 
#                          target: str, 
#                          args: Any = None, 
#                          timeout: Optional[float] = None) -> Request:
#        """Create a new request synchronously."""
#        coro = self.create_request(author, target, args, timeout)
#        future = asyncio.run_coroutine_threadsafe(coro, self.main_event_loop)
#        return future.result()
    
    @async_log_errors
    async def find_plugin(self, name: str) -> Plugin:
        #TODO: Add remote search, search by host & search by id
        plg = None
        for plugin in self.plugins.values():
            if plugin.plugin_name == name:
                plg = plugin
                self._logger.info("Found " + plg.plugin_name)
        return plg
    
    @async_handle_errors(None)
    async def _process_request(self, request: Request) -> None:
        """Process a request by invoking the target plugin method."""
        plugin_name = request.target_plugin
        function_name = request.target_method
        
        plugin = await self.find_plugin(plugin_name)
        
        if not plugin:
            await self._set_request_result(request, f"Plugin {plugin_name} not found", True)
            return
        
        func = getattr(plugin, function_name, None)
        if not callable(func):
            await self._set_request_result(request, f"Function {function_name} not found in plugin {plugin_name}", True)
            return
        
        
        if plugin.asynced or asyncio.iscoroutinefunction(func):
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
            self._logger.warning(f"Error executing {plugin}.{method}: {result}")
            return None
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