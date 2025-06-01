import contextlib
import functools
import inspect
import os
import pkgutil
from uuid import uuid4
import asyncio
import socket
from typing import Any, Optional, Callable, Union, Dict, List

from utils import LogUtil, Request, Plugin
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors

class PluginCollection:
    """Manages all plugins and facilitates communication between them."""
    
    def __init__(self, plugin_package: str, hostname: str = socket.gethostname()):
        self.hostname = hostname + uuid4().hex
        self._logger = LogUtil.create("DEBUG")
        self.requests = {}
        self.request_lock = asyncio.Lock()
        self.task_list = []
        self.plugin_package = plugin_package
        self.main_event_loop = asyncio.get_event_loop()
        self.plugins = []
        self.seen_paths = []
        
        # Start initialization
        self._init_task = asyncio.create_task(self.reload_plugins())
    
    async def wait_until_ready(self):
        """Wait until the plugins have been reloaded."""
        await self._init_task
    
    @async_log_errors
    async def reload_plugins(self) -> None:
        """Reload all plugins from the specified package."""
        self.plugins = []
        self.seen_paths = []
        
        self._logger.debug(f'Looking for plugins under package {self.plugin_package}')
        await self.walk_package(self.plugin_package)
        await self.start_loops()
    
    @async_log_errors
    async def walk_package(self, package) -> None:
        """Recursively walk the supplied package to retrieve all plugins."""
        imported_package = __import__(package, fromlist=[''])
        
        for _, pluginname, ispkg in pkgutil.iter_modules(imported_package.__path__, imported_package.__name__ + '.'):
            if not ispkg:
                plugin_module = __import__(pluginname, fromlist=[''])
                clsmembers = inspect.getmembers(plugin_module, inspect.isclass)
                
                for (_, c) in clsmembers:
                    if issubclass(c, Plugin) & (c is not Plugin):
                        self._logger.debug(f'    Found plugin class: {c.__module__}.{c.__name__}')
                        self.plugins.append(c(self._logger, self))
        
        all_current_paths = []
        if isinstance(imported_package.__path__, str):
            all_current_paths.append(imported_package.__path__)
        else:
            all_current_paths.extend([x for x in imported_package.__path__])
        
        for pkg_path in all_current_paths:
            if pkg_path not in self.seen_paths:
                self.seen_paths.append(pkg_path)
                child_pkgs = [p for p in os.listdir(pkg_path) if os.path.isdir(os.path.join(pkg_path, p))]
                
                for child_pkg in child_pkgs:
                    await self.walk_package(package + '.' + child_pkg)
    
    @async_log_errors
    async def start_loops(self) -> None:
        """Start all plugin loops."""
        for plugin in self.plugins:
            if plugin.loop_req and not plugin.loop_running:
                task = asyncio.create_task(self._start_plugin_loop(plugin))
                self.task_list.append(task)
    
    @async_handle_errors(None)
    async def _start_plugin_loop(self, plugin: Plugin):
        """Helper method to start a plugin's loop."""
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
                           author: str, 
                           target: str, 
                           args: Any = None, 
                           timeout: Optional[float] = None) -> Request:
        """Create a new request asynchronously."""
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
        """Create a new request synchronously."""
        coro = self.create_request(author, target, args, timeout)
        future = asyncio.run_coroutine_threadsafe(coro, self.main_event_loop)
        return future.result()
    
    @async_handle_errors(None)
    async def _process_request(self, request: Request) -> None:
        """Process a request by invoking the target plugin method."""
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