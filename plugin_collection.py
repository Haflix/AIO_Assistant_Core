import inspect
import os
import pkgutil
from uuid import uuid4
from utils import LogUtil, Request
from logging import Logger
import threading
import asyncio
import socket


class PluginCollection(object):
    """ Manages all the plugins and requests
    """
    _logger = Logger

    def __init__(self, plugin_package, hostname=socket.gethostname(), host=False, **kwargs):
        self.hostname = hostname + uuid4().hex
        
        self._logger = LogUtil.create("DEBUG")
        
        self.requests = {}  # Dictionary to store ongoing requests
        self.request_lock = threading.Lock()
        self.thread_list = []
        
        self.plugin_package = plugin_package
        self.reload_plugins()
        
        if host:
            # NOTE: This instance is the main host
            pass
        else:
            # connect to host
            pass
        # TODO: Add mqtt stuff
        # NOTE: if host false then ip etc of host in kwargs



    def reload_plugins(self):
        """Clears all plugins and loads them again
        """
        self.plugins = []
        self.seen_paths = []
        
        self._logger.debug(f'Looking for plugins under package {self.plugin_package}')
        self.walk_package(self.plugin_package)
        self.start_loops()
        


    def start_loops(self):
        for plugin in self.plugins: 
            if plugin.loop_req and plugin.loop_running == False:
                try:
                    self.thread_list.append(threading.Thread(target=plugin.comm_layer, kwargs={"function":plugin.loop_start}, daemon=True).start())
                except Exception as e:
                    self._logger.error(f"Error starting loop of '{plugin.plugin_name}': {e}")
                


    def kill_everything(self):
        raise NotImplementedError


    def walk_package(self, package) -> None:
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
                    self.walk_package(package + '.' + child_pkg)
    
    
    
    def create_request(self, author: str, target: str, args, timeout=None) -> Request:
        """Create a new request and returns it."""
        
        # Create the request and add it to the dict
        request = Request(self.hostname, author, target, args, self.request_lock, timeout)
        with self.request_lock:
            self.requests[request.id] = request
        self._logger.info(f"Request {request.id} created by {author} targeting {target} with args ({args})")
        
        # Start thread for the processing of the request
        self.thread_list.append(threading.Thread(target=self._process_request, args=(request,), daemon=True).start())
        
        return request
    
    def create_request_wait(self, author, target, args, timeout=None)  -> Request:
        """Create a new request, wait for its result via threading, and return it."""
        
        # Create the request and add it to the dict
        request = Request(self.hostname, author, target, args, self.request_lock, timeout)
        with self.request_lock:
            self.requests[request.id] = request
        self._logger.info(f"Request {request.id} created by {author} targeting {target} with args ({args})")
        
        # Start the processing thread
        self.thread_list.append(threading.Thread(target=self._process_request, args=(request,), daemon=True).start())

        # Wait for the result or timeout
        result, error, timed_out = request.wait_for_result()
        if timed_out:
            self._logger.warning(f"Request {request.id} timed out.")
        elif error:
            self._logger.error(f"Request {request.id} failed with an error.")
        else:
            self._logger.info(f"Request {request.id} completed successfully.")
        
        return request  # Return the completed request object
    
    
    
    def _process_request(self, request: Request):
        """Process the request by passing it to the appropriate plugin."""
        # NOTE: send request to right host via mqtt (maybe even in create_request) but i think it should be better here in _process_request ()
        
        # Extract name of plugin and function
        plugin_name, function_name = request.target.split(".")
        
        # Get plugin from self.plugins if it exists
        plugin: Plugin = next((p for p in self.plugins if p.plugin_name == plugin_name), None)
        
        if not plugin:
            request.set_result(f"Plugin {plugin_name} not found", error=True)
            return
        
        # Check if function exists in the plugin
        try:
            func = getattr(plugin, function_name, None)
            if not callable(func):
                request.set_result(f"Function {function_name} not found in plugin {plugin_name}", error=True)
                return
            # Call the function with arguments
            result = plugin.comm_layer(func, request.args)
            #result = func(request.args)
            request.set_result(result)
        except Exception as e:
            self._logger.error(f"Error processing request from {request.author} to {request.target}({request.args}) ({request.id}): {e}")
            request.set_result(str(e), error=True)
            
            
    def _process_request_external(self, request: Request):
        """Process the request by passing it to the appropriate sub-host with the plugin."""
        # NOTE: THIS METHOD IS A PLACEHOLDER
        pass
    
    
            
    
    
    
    async def loop(self):
        """Loop for the program to stay alive while scripts are running"""
        while True:
            
            # Iterate over the threads to check if theyre done
            for t in self.thread_list:
                if not t.is_alive():
                    t.handled = True
                    
                    
            # Fill list
            self.thread_list = [t for t in self.thread_list if not t.handled or any]
            
            await asyncio.get_event_loop(
                ).create_task(
                    self.cleanup_requests()
                    )
            
            await asyncio.sleep(10)
                  
    
    def cleanup_requests(self):
        """Remove completed requests."""
        with self.request_lock:
            self.requests = {rid: req for rid, req in self.requests.items() if not req.collected}


class Plugin(object):
    """Base class that each plugin must inherit from. This class sets standard variables and functions that each plugin must have.
    """

    def __init__(self, logger: Logger, plugin_collection_c: PluginCollection):
        self.description = "UNKNOWN"
        self.plugin_name = "UNKNOWN"
        self.version = "0.0.0"
        self._logger = logger
        self._plugin_collection = plugin_collection_c
        self.asynced = False
        self.loop_running = False
        self.loop_req = False
        self.event_loop = None
    
    def comm_layer(self, function, *args):
        """The method that makes it possible for synchronous and asynchronous methods to call methods from plugins
        """
        try:
            if self.asynced:
                if asyncio.iscoroutinefunction(function):  # Check if function is async
                    return self.event_loop.run_until_complete(function(*args))  # Now safe
                else:
                    self._logger.error(f"Function {function.__name__} is not async but called in async mode!")
                    return function(*args)  # Just call it normally
            else:
                return function(*args)
        except Exception as e:
            self._logger.error(f"Error in comm_layer: {e}")
            return None
    
    def loop_start(self):
        """The method that we expect all plugins, which need a loop, to implement. This is the
            method that starts the needed loops
        """
        raise NotImplementedError
    
    def perform_operation(self, argument):
        """The method that we expect all plugins to implement. This is the
            basic method
        """
        raise NotImplementedError