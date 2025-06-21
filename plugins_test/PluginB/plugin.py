from utils import Plugin
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors
import asyncio

class PluginB(Plugin):
    """Example of an asynchronous plugin that performs calculations."""
    
    def __init__(self, logger, plugin_core, arguments):
        super().__init__(logger, plugin_core, arguments)
        
    @log_errors
    def on_load(self, *args, **kwargs):
        self._logger.debug(f"on_load")

    @async_log_errors
    async def on_enable(self):
        self._logger.debug(f"on_enable")
        
    @async_log_errors
    async def on_disable(self):
        self._logger.debug(f"on_disable")
    
    @async_handle_errors(default_return=0)
    async def calculate_square(self, number):
        """Calculate the square of a number with built-in error handling."""
        
        #await asyncio.sleep(1) Simulate some async work (Simulated headache)
        result = number ** 2
        self._logger.info(f"Calculated square of {number}: {result}")
        return result
