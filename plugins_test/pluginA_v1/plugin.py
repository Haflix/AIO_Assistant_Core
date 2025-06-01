from utils import Plugin
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors
import asyncio

class PluginA(Plugin):
    """Example of an asynchronous plugin that calls another plugin."""
    
    def __init__(self, logger, plugin_collection, *args, **kwargs):
        super().__init__(logger, plugin_collection)
        
    
    @async_log_errors
    async def perform_operation(self, argument):
        """Simplified example showing how to call another plugin using the one-liner."""
        
        result = await self.execute("PluginB.calculate_square", argument)
        
        if result is None:
            return f"Error calling PluginB.calculate_square with {argument}"
        
        return result
