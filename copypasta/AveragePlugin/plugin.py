from utils import Plugin
from decorators import (
    log_errors,
    handle_errors,
    async_log_errors,
    async_handle_errors,
    async_gen_log_errors,
    async_gen_handle_errors,
    gen_log_errors,
    gen_handle_errors,
)
import asyncio


class AveragePlugin(Plugin):
    """
    Example plugin demonstrating the AIO Assistant Core plugin structure.

    This plugin shows:
    - Basic plugin lifecycle (on_load, on_enable, on_disable)
    - Using decorators for error handling and logging
    - Calling other plugins via self.execute()
    - Both simple and streaming methods
    """

    @log_errors
    def on_load(self, *args, **kwargs):
        """Called when the plugin is first loaded. Use for initialization."""
        self._logger.debug(f"AveragePlugin loaded")
        # Initialize any instance variables here
        self.state = {}

    @async_log_errors
    async def on_enable(self):
        """Called when the plugin is enabled. Use for async setup."""
        self._logger.debug(f"AveragePlugin enabled")
        # Start any background tasks or async initialization here

    @async_log_errors
    async def on_disable(self):
        """Called when the plugin is disabled. Use for cleanup."""
        self._logger.debug(f"AveragePlugin disabled")
        # Clean up resources, stop background tasks, etc.

    @async_log_errors
    async def example_method(self, value):
        """
        Example method that performs a simple operation.

        Args:
            value: Input value to process

        Returns:
            Processed result
        """
        self._logger.info(f"Processing value: {value}")
        result = value * 2
        return result

    @async_handle_errors(default_return=None)
    async def call_other_plugin(self, plugin_name, method_name, args):
        """
        Example showing how to call another plugin.

        Args:
            plugin_name: Name of the plugin to call
            method_name: Method to execute
            args: Arguments to pass to the method

        Returns:
            Result from the other plugin or None on error
        """
        self._logger.info(f"Calling {plugin_name}.{method_name} with args: {args}")

        # Execute method on another plugin
        # host options: "local", "remote", "any", or specific hostname
        result = await self.execute(plugin_name, method_name, args, host="any")

        return result

    @async_log_errors
    async def handle_event(self, data):
        """
        Example topic-subscribed endpoint.

        This method is called when someone publishes to "example/event"
        via self.notify("example/event", data) or
        self.request_topic("example/event", data).

        No need to know which plugin handles this — topic-based routing
        decouples the caller from the handler.
        """
        self._logger.info(f"Received event: {data}")
        return {"received": data, "handled_by": self.plugin_name}

    @async_gen_log_errors
    async def example_stream(self, count):
        """
        Example streaming method that yields multiple results.

        Args:
            count: Number of items to yield

        Yields:
            Sequential results
        """
        self._logger.info(f"Starting stream with count: {count}")

        for i in range(count):
            await asyncio.sleep(0.1)  # Simulate async work
            yield f"Item {i + 1} of {count}"

        self._logger.info(f"Stream completed")
