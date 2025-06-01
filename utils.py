import asyncio
import logging
import sys
from logging import Logger, StreamHandler, DEBUG
from typing import Union
from uuid import uuid4
import time
from typing import Any, Optional, Tuple, Union
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors



class LogUtil(Logger):
    """Logging class
    """
    __FORMATTER = "%(asctime)s — %(name)s — %(levelname)s — %(module)s.%(funcName)s:%(lineno)d — %(message)s"

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
        self.addHandler(self.__get_stream_handler())

    def __get_stream_handler(self) -> StreamHandler:
        handler = StreamHandler(sys.stdout)
        handler.setFormatter(self.formatter)
        return handler

    @staticmethod
    def create(log_level: str = 'DEBUG') -> Logger:
        logging.setLoggerClass(LogUtil)
        logger = logging.getLogger('AIO_AI')
        logger.setLevel(log_level)
        return logger

class DummyMQTTClient:
    """
    # Example usage
def sample_callback(topic, message):
    print(f"Received message on {topic}: {message}")

mqtt_client = DummyMQTTClient()
mqtt_client.connect()
mqtt_client.subscribe("test/topic", sample_callback)
mqtt_client.publish("test/topic", "Hello, MQTT!")
mqtt_client.simulate_incoming_message("test/topic", "Simulated message")
mqtt_client.disconnect()
"""
    def __init__(self, client_id="dummy_client"):
        self.client_id = client_id
        self.connected = False
        self.subscriptions = {}

    def connect(self, host="localhost", port=1883):
        """Simulates connecting to an MQTT broker."""
        self.connected = True
        print(f"[{self.client_id}] Connected to {host}:{port}")

    def disconnect(self):
        """Simulates disconnecting from the MQTT broker."""
        self.connected = False
        print(f"[{self.client_id}] Disconnected from broker")

    def subscribe(self, topic, callback):
        """Simulates subscribing to a topic."""
        if not self.connected:
            print(f"[{self.client_id}] Cannot subscribe, not connected")
            return
        self.subscriptions[topic] = callback
        print(f"[{self.client_id}] Subscribed to topic: {topic}")

    def unsubscribe(self, topic):
        """Simulates unsubscribing from a topic."""
        if topic in self.subscriptions:
            del self.subscriptions[topic]
            print(f"[{self.client_id}] Unsubscribed from topic: {topic}")
        else:
            print(f"[{self.client_id}] Not subscribed to topic: {topic}")

    def publish(self, topic, message):
        """Simulates publishing a message to a topic."""
        if not self.connected:
            print(f"[{self.client_id}] Cannot publish, not connected")
            return
        print(f"[{self.client_id}] Published to {topic}: {message}")
        # Simulate message reception if subscribed
        if topic in self.subscriptions:
            self.subscriptions[topic](topic, message)

    def simulate_incoming_message(self, topic, message):
        """Simulates receiving a message on a subscribed topic."""
        if topic in self.subscriptions:
            print(f"[{self.client_id}] Simulating incoming message on {topic}: {message}")
            self.subscriptions[topic](topic, message)
        else:
            print(f"[{self.client_id}] No subscription found for topic: {topic}")

class Plugin:
    """Base class for all plugins."""
    
    def __init__(self, logger: Logger, plugin_collection):
        self.description = "UNKNOWN"
        self.plugin_name = "UNKNOWN"
        self.version = "0.0.0"
        self._logger = logger
        self._plugin_collection = plugin_collection
        self.asynced = False  # Set to True for async plugins
        self.loop_running = False
        self.loop_req = False  # Set to True if the plugin needs a loop
        self.event_loop = plugin_collection.main_event_loop
    
    @async_handle_errors(default_return=None)
    async def execute(self, target: str, args: Any = None, timeout: Optional[float] = None) -> Any:
        """
        One-liner to call another plugin's method asynchronously with error handling.
        
        Args:
            target: Target plugin and method (format: "PluginName.method_name")
            args: Arguments to pass to the method
            timeout: Optional timeout in seconds
            
        Returns:
            The result from the target method or None if an error occurs
        """
        return await self._plugin_collection.execute(target, args, self.plugin_name, timeout)
    
    @handle_errors(default_return=None)
    def execute_sync(self, target: str, args: Any = None, timeout: Optional[float] = None) -> Any:
        """
        One-liner to call another plugin's method synchronously with error handling.
        
        Args:
            target: Target plugin and method (format: "PluginName.method_name")
            args: Arguments to pass to the method
            timeout: Optional timeout in seconds
            
        Returns:
            The result from the target method or None if an error occurs
        """
        return self._plugin_collection.execute_sync(target, args, self.plugin_name, timeout)
    
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