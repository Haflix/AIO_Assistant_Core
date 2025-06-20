import asyncio
import datetime
import logging
from logging.handlers import QueueHandler, QueueListener
import os
from pathlib import Path
import queue
import socket
import sys
from logging import Logger, StreamHandler, DEBUG
from typing import Union
from uuid import uuid4
import time
import yaml
from typing import Any, Optional, Tuple, Union
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors
from exceptions import RequestException, ConfigException



#class LogUtil(logging.Logger):
#    __FORMATTER = "%(asctime)s | %(name)s | %(levelname)s | %(module)s.%(funcName)s:%(lineno)d | %(message)s"
#    def __init__(
#            self,
#            name: str,
#            log_format: str = __FORMATTER,
#            level: Union[int, str] = DEBUG,
#            *args,
#            **kwargs
#    ) -> None:
#        super().__init__(name, level)
#        self.formatter = logging.Formatter(log_format)
#
#    @staticmethod
#    def create(log_level: str = 'DEBUG') -> logging.Logger:
#        """Create and configure the root logger."""
#        logging.setLoggerClass(LogUtil)
#        root_logger = logging.getLogger()
#        root_logger.setLevel(log_level)
#
#        # Remove existing handlers to avoid duplicates
#        for handler in root_logger.handlers[:]:
#            root_logger.removeHandler(handler)
#
#        # Create logs directory if it doesn't exist
#        logs_dir = "logs"
#        os.makedirs(logs_dir, exist_ok=True)
#
#        # Generate log filename with current timestamp
#        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
#        log_filename = f"AIO_AI_{timestamp}.log"
#        log_file_path = os.path.join(logs_dir, log_filename)
#
#        formatter = logging.Formatter(LogUtil.__FORMATTER)
#
#        # Add console handler
#        stream_handler = logging.StreamHandler(sys.stdout)
#        stream_handler.setFormatter(formatter)
#        root_logger.addHandler(stream_handler)
#
#        # Add file handler
#        file_handler = logging.FileHandler(log_file_path)
#        file_handler.setFormatter(formatter)
#        root_logger.addHandler(file_handler)
#
#        root_logger.info(f"Logging initialized. Log file: {log_file_path}")
#        return root_logger
class LogUtil(logging.Logger):
    __FORMATTER = "%(asctime)s | %(name)s | %(levelname)s | %(module)s.%(funcName)s:%(lineno)d | %(message)s"
    
    def __init__(
        self,
        name: str,
        log_format: str = __FORMATTER,
        level: Union[int, str] = logging.DEBUG,
        *args,
        **kwargs
    ) -> None:
        super().__init__(name, level)
        self.formatter = logging.Formatter(log_format)

    @staticmethod
    def create(log_level: str = 'DEBUG') -> logging.Logger:
        """Create and configure the root logger with non-blocking I/O"""
        logging.setLoggerClass(LogUtil)
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)

        # Remove existing handlers
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        # Create thread-safe queue and listener
        log_queue = queue.Queue(-1)  # Unlimited size
        queue_handler = QueueHandler(log_queue)
        root_logger.addHandler(queue_handler)

        # Create actual I/O handlers
        formatter = logging.Formatter(LogUtil.__FORMATTER)
        
        # Console handler
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        
        # File handler
        logs_dir = "logs"
        os.makedirs(logs_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_filename = f"AIO_AI_{timestamp}.log"
        log_file_path = os.path.join(logs_dir, log_filename)
        file_handler = logging.FileHandler(log_file_path)
        file_handler.setFormatter(formatter)

        # Create and start listener
        listener = QueueListener(
            log_queue,
            stream_handler,
            file_handler,
            respect_handler_level=True
        )
        listener.start()

        # Ensure proper shutdown
        def stop_listener():
            listener.stop()
            root_logger.removeHandler(queue_handler)
        import atexit
        atexit.register(stop_listener)

        root_logger.info(f"Non-blocking logging initialized. Log file: {log_file_path}")
        return root_logger


class ConfigUtil:
    @staticmethod
    @log_errors
    def load_config(config_path: str) -> dict:
        config = yaml.safe_load(Path(config_path).read_text())
        # Add validation logic here
        return config
    
    @staticmethod
    @log_errors
    def check_config_integrity(yaml_config, _logger):
        # Check required sections
        for section in ["plugins", "general"]:
            if section not in yaml_config:
                raise ConfigException(f"Missing config section: {section}")

        # Validate plugins
        for plugin in yaml_config.get("plugins", []):
            if "name" not in plugin or "enabled" not in plugin:
                raise ConfigException("Plugin entry missing name/enabled field")

            # Warn if path is empty but plugin_package isn't configured
            if not plugin.get("path") and "plugin_package" not in yaml_config.get("general", {}):
                _logger.warning("No path or plugin_package - plugins may not load")

        #TODO: Add validations for future networking
    
    @staticmethod
    @log_errors       
    def apply_configvalues(plugin_core):

        # Handle general ident_name (empty string or None)
        general_config = plugin_core.yaml_config.get('general', {})
        
        hostname = general_config.get('hostname')
        if not hostname:  # Covers None and empty string
            hostname = socket.gethostname()
            plugin_core.yaml_config['general']['hostname'] = hostname
        plugin_core.hostname = hostname + uuid4().hex
        plugin_core._logger.info(f"Network hostname: {plugin_core.hostname}")
        
        ident_name = general_config.get('ident_name')
        plugin_core.ident_name = ident_name if ident_name else plugin_core.hostname
        plugin_core._logger.info(f"Identifier name: {plugin_core.ident_name}")

        # Plugin base directory
        plugin_core.plugin_package = general_config.get('plugin_package', 'plugins')
        plugin_core._logger.info(f"Plugin base directory: {plugin_core.plugin_package}")


class Plugin:
    """Base class for all plugins."""
    
    def __init__(self, logger: Logger, plugin_core, arguments):
        self.description = "UNKNOWN"
        self.plugin_name = "UNKNOWN"
        self.version = "0.0.0"
        self.plugin_uuid = uuid4().hex
        self._logger = logger
        self._plugin_core = plugin_core
        self.enabled = False
        self.event_loop = plugin_core.main_event_loop
        
        self.on_load(
            *arguments if isinstance(arguments, (list, tuple)) else [],  # Unpack list/tuple if applicable
            **arguments if isinstance(arguments, dict) else {}  # Unpack dict if applicable
        )
    
    @async_log_errors
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
        One-liner to call another plugin's method asynchronously with error handling.
        
        Args:
            target: Target plugin and method (format: "PluginName.method_name")
            args: Arguments to pass to the method
            timeout: Optional timeout in seconds
            
        Returns:
            The result from the target method or None if an error occurs
        """
        return await self._plugin_core.execute(plugin, method, args, plugin_id, host, self.plugin_name, self.plugin_uuid, timeout)
    
    @log_errors
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
        One-liner to call another plugin's method synchronously with error handling.
        
        Args:
            target: Target plugin and method (format: "PluginName.method_name")
            args: Arguments to pass to the method
            timeout: Optional timeout in seconds
            
        Returns:
            The result from the target method or None if an error occurs
        """
        return self._plugin_core.execute_sync(plugin, method, args, plugin_id, host, self.plugin_name, self.plugin_uuid, timeout)
    
    @log_errors
    def on_load(self):
        """Override this method to implement functionality that needs to happen while the plugin gets loaded."""
        raise NotImplementedError
    
    @async_log_errors
    async def on_enable(self):
        """Override this method to implement plugin starting functionality. All loops and so on should be started here."""
        raise NotImplementedError
    
    @async_log_errors
    async def on_disable(self):
        """Override this method to implement plugin disabling functionality. All loops and so on should be stopped here."""
        raise NotImplementedError
    
    def perform_operation(self, argument):
        """Override this method to implement plugin operations."""
        raise NotImplementedError

class Request:
    """Represents a request from one plugin to another."""
    #def __init__(self, 
    #            author_host: str, 
    #            author: str, 
    #            target: str, 
    #            args: Any = None, 
    #            timeout: Optional[float] = None, 
    #            event_loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    def __init__(self, 
                author_host: str, 
                plugin: str,
                method: str, 
                args: Any = None, 
                plugin_id: Optional[str] = "",
                target_host: str = "any",
                author: str = "system",
                author_id: str = "system",
                timeout: Optional[float] = None, 
                event_loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self.author_host = author_host
        self.author = author
        self.author_id = author_id
        self.id = uuid4().hex
        self.target_plugin = plugin
        self.target_method = method
        self.target_plugin_id = plugin_id
        self.target_host = target_host
        self.args = args
        self.collected = False
        self.timeout = False
        self.ready = False
        self.error = False
        self.result = None
        self.created_at = time.time()
        self.timeout_duration = timeout
        self.event_loop = event_loop or asyncio.get_event_loop()
        self._future = self.event_loop.create_future()
    
    def set_result(self, result: Any, error: bool = False) -> None:
        """Set the result of the request."""
        if not self._future.done():
            self.error = error
            self.result = result
            self._future.set_result((result, error, False))
            self.ready = True
    
    def set_collected(self) -> None:
        """Mark the request as collected for cleanup."""
        self.collected = True
    
    def get_result_sync(self) -> Any:
        """Get the result synchronously."""
        future = asyncio.run_coroutine_threadsafe(self.wait_for_result_async(), self.event_loop)
        try:
            result, error, timed_out = future.result()
            if error:
                raise Exception(f"Request failed: {self.result}")
            return self.result
        except Exception as e:
            raise e
        
    async def wait_for_result_async(self) -> Tuple[Any, bool, bool]:
        """Wait for the result asynchronously."""
        try:
            if self.result is not None:
                return self.result, self.error, False
            
            # Check if we need to apply a timeout
            if self.timeout_duration:
                remaining_time = self.timeout_duration - (time.time() - self.created_at)
                if remaining_time <= 0:
                    # Already timed out
                    self.result = f"Request {self.id} timed out"
                    self.error = True
                    self.ready = True
                    self.timeout = True
                    return self.result, True, True
                
                # Wait with timeout
                try:
                    result, error, timed_out = await asyncio.wait_for(self._future, timeout=remaining_time)
                    return result, error, timed_out
                except asyncio.TimeoutError:
                    self.result = f"Request {self.id} timed out"
                    self.error = True
                    self.ready = True
                    self.timeout = True
                    return self.result, True, True
            else:
                # Wait indefinitely
                result, error, timed_out = await self._future
                return result, error, timed_out
        except Exception as e:
            return str(e), True, False