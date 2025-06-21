from utils import Plugin
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors
import asyncio

class PluginA(Plugin):
    """Example of an asynchronous plugin that calls another plugin."""
    
    def __init__(self, logger, plugin_core, arguments):
        super().__init__(logger, plugin_core, arguments)
    
    @log_errors
    def on_load(self, *args, **kwargs):
        self._logger.info("idk")

    @async_log_errors
    async def on_enable(self):
        self._logger.info("YOOOO")
        
    @async_log_errors
    async def on_disable(self):
        self._logger.info("YOOOO")
    
    @async_log_errors
    async def perform_operation(self, argument):
        """Simplified example showing how to call another plugin using the one-liner."""
        
        result = await self.execute("PluginB", "calculate_square", argument)
        
        if result is None:
            return f"Error calling PluginB.calculate_square with {argument}"
        
        return result
