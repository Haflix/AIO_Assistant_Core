import asyncio
import contextlib
import datetime
import yaml
import logging
import os
from pathlib import Path
import socket
import sys
from logging import FileHandler, Logger, StreamHandler, DEBUG
from typing import Union
from uuid import uuid4
import time
from exceptions import RequestException, ConfigException
import paho.mqtt.client as mqtt
from typing import Any, Optional, Tuple, Union
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors




class LogUtil(logging.Logger):
    __FORMATTER = "%(asctime)s | %(name)s | %(levelname)s | %(module)s.%(funcName)s:%(lineno)d | %(message)s"
    def __init__(
            self,
            name: str,
            log_format: str = __FORMATTER,
            level: Union[int, str] = DEBUG,
            *args,
            **kwargs
    ) -> None:
        super().__init__(name, level)
        self.formatter = logging.Formatter(log_format)

    @staticmethod
    def create(log_level: str = 'DEBUG') -> logging.Logger:
        """Create and configure the root logger."""
        logging.setLoggerClass(LogUtil)
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)

        # Remove existing handlers to avoid duplicates
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        # Create logs directory if it doesn't exist
        logs_dir = "logs"
        os.makedirs(logs_dir, exist_ok=True)

        # Generate log filename with current timestamp
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_filename = f"AIO_AI_{timestamp}.log"
        log_file_path = os.path.join(logs_dir, log_filename)

        formatter = logging.Formatter(LogUtil.__FORMATTER)

        # Add console handler
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

        # Add file handler
        file_handler = logging.FileHandler(log_file_path)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

        root_logger.info(f"Logging initialized. Log file: {log_file_path}")
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
        for section in ["plugins", "mqtt", "general"]:
            if section not in yaml_config:
                raise ConfigException(f"Missing config section: {section}")

        # Validate plugins
        for plugin in yaml_config.get("plugins", []):
            if "name" not in plugin or "enabled" not in plugin:
                raise ConfigException("Plugin entry missing name/enabled field")

            # Warn if path is empty but plugin_package isn't configured
            if not plugin.get("path") and "plugin_package" not in yaml_config.get("general", {}):
                _logger.warning("No path or plugin_package - plugins may not load")

        # Validate MQTT if enabled
        if yaml_config.get("mqtt", {}).get("enabled", False):
            if not yaml_config["mqtt"].get("broker_ip"):
                raise ConfigException("MQTT enabled but missing broker_ip")
    
    @staticmethod
    @log_errors       
    def apply_configvalues(plugin_collection):
        # Handle MQTT hostname (empty string or None)
        mqtt_config = plugin_collection.yaml_config.get('mqtt', {})
        hostname = mqtt_config.get('hostname')
        if not hostname:  # Covers None and empty string
            hostname = socket.gethostname()
            plugin_collection.yaml_config['mqtt']['hostname'] = hostname
        plugin_collection.hostname = hostname
        plugin_collection._logger.info(f"Network hostname: {plugin_collection.hostname}")

        # Handle general ident_name (empty string or None)
        general_config = plugin_collection.yaml_config.get('general', {})
        ident_name = general_config.get('ident_name')
        plugin_collection.ident_name = ident_name if ident_name else plugin_collection.hostname
        plugin_collection._logger.info(f"Identifier name: {plugin_collection.ident_name}")

        # Plugin base directory
        plugin_collection.plugin_package = general_config.get('plugin_package', 'plugins')
        plugin_collection._logger.info(f"Plugin base directory: {plugin_collection.plugin_package}")


#class AsyncMQTTClient:
#    """
#    # Example usage
#def sample_callback(topic, message):
#    print(f"Received message on {topic}: {message}")
#
#mqtt_client = DummyMQTTClient()
#mqtt_client.connect()
#mqtt_client.subscribe("test/topic", sample_callback)
#mqtt_client.publish("test/topic", "Hello, MQTT!")
#mqtt_client.simulate_incoming_message("test/topic", "Simulated message")
#mqtt_client.disconnect()
#"""
#    def __init__(self, config, logger: LogUtil):
#        #NOTE: Add on_message etc
#        #NOTE: connect, disconnect, subscribe, unsubscribe, 
#        #NOTE: on_connect, on_message, on_connect_fail, on_disconnect
#        self.mqttC = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
#        self._logger = logger
#        self._logger.setLevel(logging.DEBUG)
#        self.config = config
#        self.connected = False
#        self.loop = asyncio.get_event_loop()
#        self.pending_requests = {}
#        
#        self.ack_timeout = 2  # Seconds to wait for ACK
#        self.max_ack_attempts = 3
#        
#        # Configure credentials
#        if config.get("username"):
#            self.mqttC.username_pw_set(config["username"], config["password"])
#        
#        # Configure TLS if needed
#        if config.get("tls", True):
#            self.mqttC.tls_set()
#        
#        
#        self.configureCallbacks()
#        
#        
#    def configureCallbacks(self):
#        self.mqttC.on_connect = self._on_connect 
#        self.mqttC.on_connect_fail = self._on_connect_fail #FIXME
#        
#        self.mqttC.on_disconnect = self._on_disconnect #FIXME
#        
#        self.mqttC.on_log = self._logger.debug #NOTE: Idk if it works
#        
#        self.mqttC.on_pre_connect #FIXME
#    
#        self.mqttC.on_message = self._on_message 
#        
#    
#    async def connect(self):
#        self.mqttC.connect_async(
#            self.config["broker_ip"],
#            self.config["port"]
#        )
#        self.mqttC.loop_start()    
#    
#    def _on_connect(self, mqttC, userdata, flags, reason_code, properties):
#        if reason_code == 0:
#            self.logger.info("Connected to MQTT broker")
#            self.connected = True
#            # Subscribe to necessary topics
#            mqttC.subscribe(f"devices/{self.config['hostname']}/execute")
#            mqttC.subscribe("devices/+/heartbeat")
#            mqttC.subscribe(f"devices/{self.config['hostname']}/response/+")
#        else:
#            self.logger.error(f"Connection failed: {mqtt.connack_string(reason_code)}")
#
#    def _on_message(self, mqttC, userdata, msg):
#        self.loop.call_soon_threadsafe(
#            self._handle_async_message,
#            msg.topic,
#            msg.payload.decode()
#        )
#        
#    async def _handle_async_message(self, topic, payload):
#        if topic.startswith("devices/") and "/execute" in topic:
#            await self._handle_execute_request(topic, payload)
#        elif "heartbeat" in topic:
#            self._handle_heartbeat(topic, payload)
#        elif "/response/" in topic:
#            self._handle_response(topic, payload)
#            
#    def _handle_heartbeat(self, topic, payload):
#        """Update host registry with heartbeat data"""
#        host = topic.split("/")[1]
#        data = json.loads(payload)
#        self.host_registry[host] = {
#            "last_seen": time.time(),
#            "plugins": data["plugins"]
#        } 
#
#    async def _handle_execute_request(self, topic, payload):
#        """Process incoming requests with ACK mechanism"""
#        try:
#            data = self._deserialize(payload)
#            request_id = data['request_id']
#            sender_host = topic.split('/')[1]
#
#            # Send ACK immediately
#            ack_topic = f"devices/{sender_host}/ack/{request_id}"
#            self.client.publish(ack_topic, self._serialize({
#                'status': 'received',
#                'receiver': self.config['hostname']
#            }))
#
#            # Process request
#            result = await self._process_request_locally(data)
#            
#            # Send response
#            response_topic = data['response_topic']
#            self.client.publish(response_topic, self._serialize(result))
#            
#        except Exception as e:
#            self.logger.error(f"Request handling failed: {str(e)}")
#
#    async def _process_request_locally(self, data):
#        """Execute the requested plugin method locally"""
#        plugin = self._plugin_collection.get_plugin(data['plugin'])
#        if not plugin:
#            raise ValueError(f"Plugin {data['plugin']} not found")
#        return await getattr(plugin, data['method'])(data['args'])
#    
#    def _serialize(self, data):
#        """Handle binary data and complex types"""
#        if isinstance(data, bytes):
#            return json.dumps({
#                '_type': 'bytes',
#                'data': base64.b64encode(data).decode('utf-8')
#            })
#        return json.dumps(data)
#
#    def _deserialize(self, payload):
#        """Convert received payload back to original format"""
#        try:
#            data = json.loads(payload)
#            if isinstance(data, dict) and data.get('_type') == 'bytes':
#                return base64.b64decode(data['data'])
#            return data
#        except json.JSONDecodeError:
#            return payload


class Plugin:
    """Base class for all plugins."""
    def __init__(self, logger: LogUtil, plugin_collection):
        self.description = "UNKNOWN"
        self.plugin_name = "UNKNOWN"
        self.version = "0.0.0"
        self.plugin_uuid = uuid4().hex
        self._logger = logger
        self._logger.setLevel(logging.DEBUG)
        self._plugin_collection = plugin_collection
        self.asynced = False
        self.loop_running = False
        self.loop_req = False
        self.event_loop = plugin_collection.main_event_loop
    
    @async_handle_errors(default_return=None)
    async def execute(self, plugin: str, method: str, args: Any = None, target_plugin_id: str = None, host: str = "any", timeout: Optional[float] = None) -> Any:
        """
        One-liner to call another plugin's method asynchronously with error handling.
        
        Args:
            target: Target plugin and method (format: "PluginName.method_name")
            args: Arguments to pass to the method
            timeout: Optional timeout in seconds
            
        Returns:
            The result from the target method or None if an error occurs
        """
        #TODO: Update the description
        
        return await self._plugin_collection.execute(plugin, method, args, target_plugin_id, host, self.plugin_name, self.plugin_uuid, timeout)

    @handle_errors(default_return=None)
    def execute_sync(self, plugin: str, method: str, args: Any = None, target_plugin_id: str = None, host: str = "any", timeout: Optional[float] = None) -> Any:
        """
        One-liner to call another plugin's method synchronously with error handling.
        
        Args:
            target: Target plugin and method (format: "PluginName.method_name")
            args: Arguments to pass to the method
            timeout: Optional timeout in seconds
            
        Returns:
            The result from the target method or None if an error occurs
        """
        return self._plugin_collection.execute_sync(plugin, method, args, target_plugin_id, host, self.plugin_name, self.plugin_uuid, timeout)
    
    def loop_start(self):
        """Override this method to implement plugin loop functionality."""
        raise NotImplementedError
    
    def perform_operation(self, argument):
        """Override this method to implement plugin operations."""
        raise NotImplementedError

class Request:
    """Represents a request from one plugin to another."""
    
    def __init__(self, 
                author_host: str, 
                author: str, 
                target: str, 
                args: Any = None, 
                timeout: Optional[float] = None, 
                event_loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self.author_host = author_host
        self.author = author
        self.id = uuid4().hex
        self.target = target
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
    
    @contextlib.asynccontextmanager
    async def request_context_async(self):   
        try:
            result, error, timed_out = await self.wait_for_result_async()
            if error:
                raise RequestException(f"Request {self.id} failed: {self.result}")
            yield result
        finally:
            self.set_collected()

    @contextlib.contextmanager
    def request_context_sync(self):
        try:
            result = self.get_result_sync()
            if self.error:
                raise RequestException(f"Request failed: {self.result}")
            yield result
        finally:
            self.set_collected()