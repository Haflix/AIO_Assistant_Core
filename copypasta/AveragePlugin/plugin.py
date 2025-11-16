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


class PluginA(Plugin):
    """Example of an asynchronous plugin that calls another plugin."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self._logger.debug(f"on_load")

    @async_log_errors
    async def on_enable(self):
        self._logger.debug(f"on_enable")

    @async_log_errors
    async def on_disable(self):
        self._logger.debug(f"on_disable")

    @async_log_errors
    async def perform_operation(self, argument):
        """Simplified example showing how to call another plugin using the one-liner."""

        result = await self.execute("PluginB", "calculate_square", argument)

        if result is None:
            return f"Error calling PluginB.calculate_square with {argument}"

        return result
