from utils import Plugin
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors
import asyncio

class PluginB(Plugin):
    """Example of an asynchronous plugin that performs calculations."""
    
    def __init__(self, logger, plugin_collection):
        super().__init__(logger, plugin_collection)
        self.description = "Example calculation plugin"
        self.plugin_name = "PluginB"
        self.asynced = True
    
    @async_handle_errors(default_return=0)
    async def calculate_square(self, number):
        """Calculate the square of a number with built-in error handling."""
        await asyncio.sleep(1)  # Simulate some async work
        result = number ** 2
        self._logger.info(f"Calculated square of {number}: {result}")
        return result
