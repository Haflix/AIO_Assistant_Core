from utils import Plugin
from decorators import (
    log_errors,
    handle_errors,
    async_log_errors,
    async_handle_errors,
    async_gen_log_errors,
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

    @async_gen_log_errors
    async def perform_operation_stream(self, argument):
        result = await self.execute("PluginB", "calculate_square", argument)
        for i in range(25):
            await asyncio.sleep(0.5)
            yield result  # we live in a society
        yield "ended stream test"

    @async_log_errors
    async def background_stream(self, argument):
        result = await self.execute("PluginB", "calculate_square", argument)
        for i in range(25):
            await asyncio.sleep(0.5)
            self._logger.info(f"Background stream: {i}")  # we live in a society
        self._logger.info("ended background stream")
