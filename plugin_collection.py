import functools
import inspect
import os
import pkgutil
from uuid import uuid4
from utils import LogUtil, Request, Plugin
from logging import Logger
from contextlib import asynccontextmanager
import asyncio
import socket


class PluginCollection(object):
    """ Manages all the plugins and requests
    """
    _logger = Logger

    def __init__(self, plugin_package: str, hostname: str=socket.gethostname()):
        try:
            self.hostname = hostname + uuid4().hex

            self._logger = LogUtil.create("DEBUG")

            self.requests = {}  # Dictionary to store ongoing requests
            self.request_lock = asyncio.Lock()
            self.task_list = []

            self.plugin_package = plugin_package
            
            self._init_task = asyncio.create_task(self.reload_plugins())
            
        except Exception as e:
            self._logger.critical(f"Critical Error while initializing: {e.with_traceback()}")

    async def wait_until_ready(self):
        """Wait until the plugins have been reloaded."""
        await self._init_task

    async def reload_plugins(self) -> None:
        """Clears all plugins and loads them again
        """
        self.plugins = []
        self.seen_paths = []
        
        self._logger.debug(f'Looking for plugins under package {self.plugin_package}')
        await self.walk_package(self.plugin_package)
        await self.start_loops()
        


    async def start_loops(self) -> None:
        plugin: Plugin
        for plugin in self.plugins: 
            if plugin.loop_req and plugin.loop_running == False:
                try:
                    if plugin.asynced:
                        task = asyncio.create_task(plugin.comm_layer(plugin.loop_start))
                        self.task_list.append(task)
                    else:
                        # For sync plugins, offload to an executor.
                        task = asyncio.create_task(self.sync_call(plugin.comm_layer, plugin.loop_start))
                        self.task_list.append(task)
                except Exception as e:
                    self._logger.error(f"Error starting loop of '{plugin.plugin_name}': {e}")
                


    async def kill_everything(self):
        raise NotImplementedError



    async def walk_package(self, package) -> None:
        """Recursively walk the supplied package to retrieve all plugins
        """
        
        # Import the specified package dynamically. 
        # The fromlist=[''] ensures that the imported_package is returned as the 
        # actual package object and not just the top-level module.
        imported_package = __import__(package, fromlist=[''])

        # Iterate over all modules in the specified package using pkgutil.iter_modules.
        # This returns a tuple for each module: (module loader, module name, is package flag).
        for _, pluginname, ispkg in pkgutil.iter_modules(imported_package.__path__, imported_package.__name__ + '.'):
            
            # Skip packages (directories) and only process regular modules (files).
            
            if not ispkg:
                # Dynamically import the module by name.
                plugin_module = __import__(pluginname, fromlist=[''])
                
                # Retrieve all classes defined in the module using inspect.getmembers.
                clsmembers = inspect.getmembers(plugin_module, inspect.isclass)
                
                for (_, c) in clsmembers:
                    # Only add classes that are a sub class of the main Plugin class, but NOT the Plugin class itself
                    if issubclass(c, Plugin) & (c is not Plugin):
                        # Log the discovered plugin class.
                        self._logger.debug(f'    Found plugin class: {c.__module__}.{c.__name__}')
                        
                        # Instantiate the plugin class and add it to the plugins list.
                        self.plugins.append(c(self._logger, self))


        # Retrieve all paths associated with the current package. 
        # A package's __path__ can be a single string or an iterable of paths.
        all_current_paths = []
        if isinstance(imported_package.__path__, str):
            all_current_paths.append(imported_package.__path__)
        else:
            all_current_paths.extend([x for x in imported_package.__path__])

        # Recursively traverse sub-packages.
        for pkg_path in all_current_paths:
            if pkg_path not in self.seen_paths:  # Avoid revisiting already-seen paths.
                self.seen_paths.append(pkg_path)

                # Identify all subdirectories in the current package path. 
                # These subdirectories are potential sub-packages.
                child_pkgs = [p for p in os.listdir(pkg_path) if os.path.isdir(os.path.join(pkg_path, p))]

                # Recursively call walk_package on each sub-package.
                for child_pkg in child_pkgs:
                    await self.walk_package(package + '.' + child_pkg)
    
    
    
#    async def create_request(self, author: str, target: str, args, timeout=None) -> Request:
#        """Create a new request and returns it."""
#        
#        # Create the request and add it to the dict
#        request = Request(self.hostname, author, target, args, self.request_lock, timeout)
#        with self.request_lock:
#            self.requests[request.id] = request
#        self._logger.info(f"Request {request.id} created by {author} targeting {target} with args ({args})")
#        
#        # Start thread for the processing of the request
#        self.thread_list.append(threading.Thread(target=self._process_request, args=(request,), daemon=True).start())
#        
#        return request
    
    @asynccontextmanager
    async def request_context(self, request):
        """
        An async context manager to handle waiting for a request's result and ensure cleanup.
        """
        try:
            # Wait for the result asynchronously.
            result, error, timed_out = await request.wait_for_result_async()
            if error:
                raise Exception(f"Request {request.id} failed with result: {request.result}")
            yield result
        except Exception as e:
            # Optionally log the error or handle it here.
            self._logger.error(e)
            raise e
        finally:
            # Always mark the request as collected.
            request.set_collected()
    
    
    async def create_request(self, author: str, target: str, args, timeout=None) -> Request:
        """Create a new request and start its processing asynchronously."""
        
        request = Request(self.hostname, author, target, args, timeout)
        async with self.request_lock:
            self.requests[request.id] = request
        self._logger.info(f"Request {request.id} created by {author} targeting {target} with args ({args})")
        
        task = asyncio.create_task(self._process_request(request))
        self.task_list.append(task)
        
        return request
    
    
#    async def create_request_wait(self, author, target, args, timeout=None)  -> object:
#        """Create a new request, wait for its result via threading, and return it."""
#        
#        # Create the request and add it to the dict
#        request = await self.create_request(author, target, args, timeout)
#
#        # Wait for the result or timeout
#        result, error, timed_out = await request.wait_for_result_async()
#        
#        
#        if timed_out:
#            self._logger.warning(f"Request {request.id} timed out.") 
#        elif error:
#            self._logger.error(f"Request {request.id} failed with an error: {result}")
#        else:
#            self._logger.info(f"Request {request.id} completed successfully.")
#            return request.get_result()  # Return the completed request object
        
    
    
    
    async def _process_request(self, request: Request) -> None:
        """Process the request by passing it to the appropriate plugin."""
        # NOTE: send request to right host via mqtt (maybe even in create_request) but i think it should be better here in _process_request ()
        
        # Extract name of plugin and function
        plugin_name, function_name = request.target.split(".")
        
        # Get plugin from self.plugins if it exists
        plugin: Plugin = next((p for p in self.plugins if p.plugin_name == plugin_name), None)
        
        if not plugin:
            await self.sync_call(request.set_result, f"Plugin {plugin_name} not found", True)
            return
        
        # Check if function exists in the plugin
        try:
            func = getattr(plugin, function_name, None)
            if not callable(func):
                await self.sync_call(request.set_result, f"Function {function_name} not found in plugin {plugin_name}", True)
                return
            # Call the function with arguments
            result = await self.sync_call(plugin.comm_layer, func, request.args)
            
            #result = func(request.args)
            await self.sync_call(request.set_result, result)
            
        except Exception as e:
            self._logger.error(f"Error processing request from {request.author} to {request.target}({request.args}) ({request.id}): {e}")
            await self.sync_call(request.set_result, str(e), True)


    async def sync_call(self, func, *args):
        """Calls a sync function asynchronously and returns its result."""
        #NOTE: Was wenn keine args?
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(func, *args))
        
    
    
    async def loop(self):
        """Keep the application running, cleaning up tasks and requests periodically."""
        while True:
            # Remove completed tasks.
            self.task_list = [t for t in self.task_list if not t.done()]
            await self.cleanup_requests()
            await asyncio.sleep(10)
                  
    
    async def cleanup_requests(self):
        """Remove completed requests."""
        async with self.request_lock:
            self.requests = {rid: req for rid, req in self.requests.items() if not req.collected}


