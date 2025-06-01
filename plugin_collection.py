import contextlib
import functools
import importlib
import inspect
import json
import os
from pathlib import Path
import pkgutil
import time
from uuid import uuid4
import asyncio
import socket
from typing import Any, Optional, Callable, Union, Dict, List
import yaml

from exceptions import ConfigException, RequestException
from utils import LogUtil, Request, Plugin, AsyncMQTTClient
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors

class PluginCollection:
    """Manages all plugins and facilitates communication between them.
    
    Attributes
    ----------
    abc : str
        cool
        
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
        
        if self.yaml_config["mqtt"]["enabled"]:
            self._init_mqtt()

        # Host registry
        self.host_registry = {}
        self.host_timeout = 40  # Seconds since last heartbeat
        self.local_hostname = self.hostname
        
        # Start initialization
        self._init_task = asyncio.create_task(self.reload_plugins())
        
    def _init_mqtt(self):
        """Initialize MQTT connection and subscriptions"""
        
        self.mqtt = AsyncMQTTClient(
                config={
                    "broker_ip": self.yaml_config["mqtt"]["broker_ip"],
                    "port": self.yaml_config["mqtt"]["port"],
                    "username": self.yaml_config["mqtt"].get("username"),
                    "password": self.yaml_config["mqtt"].get("password"),
                    "tls": self.yaml_config["mqtt"].get("tls", False),
                    "hostname": self.hostname
                },
                logger=self._logger
            )
        asyncio.create_task(self.mqtt.connect())
        asyncio.create_task(self._heartbeat_task())
        
    
    async def _heartbeat_task(self):
        """Regularly publish heartbeat messages"""
        while True:
            await self.mqtt.publish(
                f"devices/{self.local_hostname}/heartbeat",
                json.dumps({
                    "plugins": [p.plugin_name for p in self.plugins],
                    "timestamp": time.time()
                })
            )
            await asyncio.sleep(30)   
        
    
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
                raise ConfigException(f"Missing config section: {section}")

        # Validate plugins
        for plugin in self.yaml_config.get("plugins", []):
            if "name" not in plugin or "enabled" not in plugin:
                raise ConfigException("Plugin entry missing name/enabled field")

            # Warn if path is empty but plugin_package isn't configured
            if not plugin.get("path") and "plugin_package" not in self.yaml_config.get("general", {}):
                self._logger.warning("No path or plugin_package - plugins may not load")

        # Validate MQTT if enabled
        if self.yaml_config.get("mqtt", {}).get("enabled", False):
            if not self.yaml_config["mqtt"].get("broker_ip"):
                raise ConfigException("MQTT enabled but missing broker_ip")
                    
            
    @log_errors       
    def apply_configvalues(self):
        # Handle MQTT hostname (empty string or None)
        mqtt_config = self.yaml_config.get('mqtt', {})
        hostname = mqtt_config.get('hostname')
        if not hostname:  # Covers None and empty string
            hostname = socket.gethostname()
            self.yaml_config['mqtt']['hostname'] = hostname
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
    
    def resolve_plugin_path(self, plugin_config, base_package):
        return os.path.abspath(plugin_config.get("path") or os.path.join(base_package, plugin_config["name"]))

    
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
            path = self.resolve_plugin_path(plugin_entry, self.plugin_package)
            #path = plugin_entry.get("path") or os.path.join(self.plugin_package, name)
            #path = os.path.abspath(path)

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
            try:
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

            except Exception as e:
                self._logger.error(f"Failed loading {name}: {e}")
                continue

        await self.start_loops()
    
    
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
    
    
    @async_handle_errors(default_return=None)
    async def find_matching_plugin(self):
        pass
    
    # One-liner methods for plugin communication
    @async_handle_errors(default_return=None)
    async def execute(
        self,
        plugin: str,
        method: str,
        args: Any = None,
        plugin_id: Optional[str] = "",
        host: str = "any",  # "any", "remote", "local", or hostname
        author: str = "system",
        author_id: str = "system",
        timeout: Optional[float] = None
    ) -> Any:
        # Determine target host
        if host == "local":
            return await self._execute_local(plugin, method, args, author, timeout)
        else:
            return await self._execute_remote(plugin, method, args, host, author, timeout)

    async def _execute_local(self, plugin, method, args, author, timeout):
        """Original local execution logic"""
        request = await self.create_request(author, f"{plugin}.{method}", args, timeout)
        async with self.request_context_async(request) as result:
            return result

    async def _execute_remote(self, plugin, method, args, host, author, timeout):
        """Enhanced remote execution with host filtering"""
        # First check local plugins if appropriate
        if host in ["any", "remote"] and self._has_local_plugin(plugin):
            if host == "any":
                self._logger.info(f"Using local instance of {plugin}")
                return await self._execute_local(plugin, method, args, author, timeout)
            if host == "remote":
                self._logger.debug("Skipping local instance for remote request")

        # Find suitable hosts
        candidates = self._get_eligible_hosts(plugin, host)
        if not candidates:
            self._logger.warning("No eligible hosts found")
            return None

        request_id = str(uuid4())
        response_topic = f"devices/{self.local_hostname}/response/{request_id}"
        
        payload = self.mqtt._serialize({
            "request_id": request_id,
            "plugin": plugin,
            "method": method,
            "args": args,
            "author": author,
            "response_topic": response_topic
        })

        # Send request with ACK verification
        future = asyncio.Future()
        self.mqtt.pending_requests[request_id] = {
            "future": future,
            "attempts": 0,
            "candidates": candidates.copy()
        }

        await self._send_request_with_ack(request_id, payload, candidates)
        return await future

    def _has_local_plugin(self, plugin_name):
        return any(p.plugin_name == plugin_name for p in self.plugins)

    def _get_eligible_hosts(self, plugin_name, host_mode):
        """Get hosts that have the plugin and are online"""
        now = time.time()
        eligible = []
        
        for host, info in self.host_registry.items():
            # Skip if offline
            if (now - info['last_seen']) > self.host_timeout:
                continue
                
            # Skip local host for remote requests
            if host_mode == "remote" and host == self.local_hostname:
                continue
                
            # Check plugin availability
            if plugin_name in info['plugins']:
                eligible.append(host)
                
        return eligible

    async def _send_request_with_ack(self, request_id, payload, candidates):
        """Send request and wait for ACK with retries"""
        request_info = self.mqtt.pending_requests[request_id]
        
        while request_info['attempts'] < self.mqtt.max_ack_attempts:
            request_info['attempts'] += 1
            target_host = self._select_target_host(candidates)
            
            if not target_host:
                request_info['future'].set_exception(ValueError("No available hosts"))
                return

            # Send to specific host
            topic = f"devices/{target_host}/execute"
            await self.mqtt.publish(topic, payload)
            
            # Wait for ACK
            try:
                ack = await asyncio.wait_for(
                    self._wait_for_ack(request_id, target_host),
                    timeout=self.mqtt.ack_timeout
                )
                if ack:
                    return  # Success, exit retry loop
                    
            except asyncio.TimeoutError:
                self._logger.warning(f"No ACK from {target_host}, attempt {request_info['attempts']}/{self.mqtt.max_ack_attempts}")
                candidates.remove(target_host)

        request_info['future'].set_exception(TimeoutError("No ACK received after retries"))

    async def _wait_for_ack(self, request_id, target_host):
        ack_topic = f"devices/{target_host}/ack/{request_id}"
        future = asyncio.Future()
        
        def ack_handler(topic, payload):
            if topic == ack_topic:
                data = self.mqtt._deserialize(payload)
                future.set_result(data)
                
        self.mqtt.mqttC.message_callback_add(ack_topic, ack_handler)
        try:
            return await future
        finally:
            self.mqtt.mqttC.message_callback_remove(ack_topic)

    
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
    ):
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