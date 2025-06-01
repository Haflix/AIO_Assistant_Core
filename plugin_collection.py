import contextlib
import functools
import importlib
import inspect
import json
import os
from pathlib import Path
import pkgutil
from uuid import uuid4
import asyncio
import socket
from typing import Any, Optional, Callable, Union, Dict, List
import yaml

from utils import LogUtil, Request, Plugin
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors

class PluginCollection:
    """Manages all plugins and facilitates communication between them.
    
    Attributes
    ----------
    abc : str
        (TEXT NOT FINISHED)
        
    Methods
    -------
    async wait_until_ready()
        Waits until the plugins have been loaded. 
    """
    # TODO: complete here
    
    def __init__(self, config_path: str):
        """
        Parameters
        ----------
        config_path : str
            The path of the config.yaml
        """
        self._logger = LogUtil.create("DEBUG")
        
        
        self.mqtt = None
        self.yaml_config = None
        self.load_config_yaml(config_path)
        self.apply_configvalues()
        
        
        self.requests = {}
        self.request_lock = asyncio.Lock()
        self.task_list = []
        
        self.main_event_loop = asyncio.get_event_loop()
        self.plugins = []
        self.seen_paths = []
        
        # Start initialization
        self._init_task = asyncio.create_task(self.reload_plugins())
        
    @log_errors
    def load_config_yaml(self, config_path: str):
        self.yaml_config: dict = yaml.safe_load(Path(config_path).read_text())
        self._logger.info(self.yaml_config)#json.dumps(self.yaml_config, indent=2))# NOTE: This line is just for debug
        
        self.check_config_integrity()
        
    @log_errors
    def check_config_integrity(self):
        # Check required sections
        for section in ["plugins", "mqtt", "general"]:
            if section not in self.yaml_config:
                self._logger.critical(f"Missing config section: {section}")

        # Validate plugins
        for plugin in self.yaml_config.get("plugins", []):
            if "name" not in plugin or "enabled" not in plugin:
                self._logger.critical("Plugin entry missing name/enabled field")

            # Warn if path is empty but plugin_package isn't configured
            if not plugin.get("path") and "plugin_package" not in self.yaml_config.get("general", {}):
                self._logger.warning("No path or plugin_package - plugins may not load")

        # Validate MQTT if enabled
        if self.yaml_config.get("mqtt", {}).get("enabled", False):
            if not self.yaml_config["mqtt"].get("broker_ip"):
                self._logger.critical("MQTT enabled but missing broker_ip")
                    
            
    @log_errors       
    def apply_configvalues(self):
        # Handle MQTT hostname (empty string or None)
        mqtt_config = self.yaml_config.get('mqtt', {})
        hostname = mqtt_config.get('hostname')
        if not hostname:  # Covers None and empty string
            hostname = socket.gethostname()
        self.hostname = hostname
        self._logger.info(f"Network hostname: {self.hostname}")

        # Handle general ident_name (empty string or None)
        general_config = self.yaml_config.get('general', {})
        ident_name = general_config.get('ident_name')
        self.ident_name = ident_name if ident_name else self.hostname
        self._logger.info(f"Identifier name: {self.ident_name}")

        # Plugin base directory
        self.plugin_package = general_config.get('plugin_package', 'plugins')
        self._logger.info(f"Plugin base directory: {self.plugin_package}")
          
    
    
    async def wait_until_ready(self):
        """Wait until the plugins have been loaded.
        """
        
        # awaits the plugins loading
        await self._init_task
        
        asyncio.create_task(self.running_loop())
    
    @async_log_errors
    async def reload_plugins(self) -> None:
        self.plugins = []

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
                self.plugins.append(plugin)
                
                self._logger.info(f"Successfully loaded plugin: {name} (Version: {plugin_config['version']}, Path: {path})")

            #except Exception as e:
            #    self._logger.error(f"Failed loading {name}: {e}")
            #    continue

        await self.start_loops()

    
    
#    @async_log_errors
#    async def walk_package(self, package) -> None:
#        """Recursively walk the supplied package to retrieve all plugins.
#        """
#        
#        imported_package = __import__(package, fromlist=[''])
#        
#        for _, pluginname, ispkg in pkgutil.iter_modules(imported_package.__path__, imported_package.__name__ + '.'):
#            if not ispkg:
#                plugin_module = __import__(pluginname, fromlist=[''])
#                clsmembers = inspect.getmembers(plugin_module, inspect.isclass)
#                
#                for (_, c) in clsmembers:
#                    if issubclass(c, Plugin) & (c is not Plugin):
#                        self._logger.debug(f'    Found plugin class: {c.__module__}.{c.__name__}')
#                        self.plugins.append(c(self._logger, self))
#        
#        all_current_paths = []
#        if isinstance(imported_package.__path__, str):
#            all_current_paths.append(imported_package.__path__)
#        else:
#            all_current_paths.extend([x for x in imported_package.__path__])
#        
#        for pkg_path in all_current_paths:
#            if pkg_path not in self.seen_paths:
#                self.seen_paths.append(pkg_path)
#                child_pkgs = [p for p in os.listdir(pkg_path) if os.path.isdir(os.path.join(pkg_path, p))]
#                
#                for child_pkg in child_pkgs:
#                    await self.walk_package(package + '.' + child_pkg)
    
    
    @async_log_errors
    async def start_loops(self) -> None:
        """Start all plugin loops.
        """
        
        for plugin in self.plugins:
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
    
    @async_log_errors
    async def call(self, 
                  target: str, 
                  args: Any = None, 
                  author: str = "system", 
                  timeout: Optional[float] = None) -> Any:
        """
        Simplified method to call a plugin method and get its result.
        
        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds
            
        Returns:
            The result from the plugin method
        """
        
        request = await self.create_request(author, target, args, timeout)
        async with self.request_context_async(request) as result:
            return result
    
    @log_errors
    def call_sync(self, 
                 target: str, 
                 args: Any = None, 
                 author: str = "system", 
                 timeout: Optional[float] = None) -> Any:
        """
        Synchronous version of call method.
        
        Args:
            target: Target plugin and method (format: "PluginName.method_name")
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds
            
        Returns:
            The result from the plugin method
        """
        
        request = self.create_request_sync(author, target, args, timeout)
        with self.request_context_sync(request) as result:
            return result
    
    @contextlib.asynccontextmanager
    async def request_context_async(self, request: Request):   
        """Async context manager to handle requests.
        """
        
        try:
            result, error, timed_out = await request.wait_for_result_async()
            if error:
                raise Exception(f"Request {request.id} failed: {request.result}")
            yield result
        finally:
            request.set_collected()
    
    @contextlib.contextmanager
    def request_context_sync(self, request: Request):
        """Sync context manager to handle requests.
        """
        
        try:
            result = request.get_result_sync()
            if request.error:
                raise Exception(f"Request failed: {request.result}")
            yield result
        finally:
            request.set_collected()
    
    @async_log_errors
    async def create_request(self, 
                           author: str, 
                           target: str, 
                           args: Any = None, 
                           timeout: Optional[float] = None) -> Request:
        """Create a new request asynchronously.
        """
        
        request = Request(self.hostname, author, target, args, timeout, self.main_event_loop)
        async with self.request_lock:
            self.requests[request.id] = request
        
        self._logger.info(f"Request {request.id} created by {author} targeting {target}")
        task = asyncio.create_task(self._process_request(request))
        self.task_list.append(task)
        
        return request
    
    @log_errors
    def create_request_sync(self, 
                          author: str, 
                          target: str, 
                          args: Any = None, 
                          timeout: Optional[float] = None) -> Request:
        """Create a new request synchronously.
        """
        
        coro = self.create_request(author, target, args, timeout)
        future = asyncio.run_coroutine_threadsafe(coro, self.main_event_loop)
        return future.result()
    
    @async_handle_errors(None)
    async def _process_request(self, request: Request) -> None:
        """Process a request by invoking the target plugin method.
        """
        
        plugin_name, function_name = request.target.split(".")
        
        plugin = next((p for p in self.plugins if p.plugin_name == plugin_name), None)
        
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
        """Set the result of a request.
        """
        
        if isinstance(result, asyncio.Future):
            result = await result
        request.set_result(result, error)
    
    async def running_loop(self):
        """Maintenance loop that cleans up tasks and requests.
        """
        
        while True:
            self.task_list = [t for t in self.task_list if not t.done()]
            await self.cleanup_requests()
            await asyncio.sleep(10)
    
    @async_log_errors
    async def cleanup_requests(self):
        """Remove collected requests.
        """
        
        async with self.request_lock:
            self.requests = {rid: req for rid, req in self.requests.items() if not req.collected}
    
    # One-liner methods for plugin communication
    @async_handle_errors(default_return=None)
    async def execute(self, target: str, args: Any = None, author: str = "system", timeout: Optional[float] = None) -> Any:
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
        
        request = await self.create_request(author, target, args, timeout)
        result, error, _ = await request.wait_for_result_async()
        request.set_collected()  # Mark for cleanup
        
        if error:
            self._logger.warning(f"Error executing {target}: {result}")
            return None
        return result
    
    @handle_errors(default_return=None)
    def execute_sync(self, target: str, args: Any = None, author: str = "system", timeout: Optional[float] = None) -> Any:
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
            self.execute(target, args, author, timeout),
            self.main_event_loop
        )
        return future.result()