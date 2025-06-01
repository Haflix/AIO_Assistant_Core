from utils import Plugin
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors
import asyncio

class PluginA(Plugin):
    """Example of an asynchronous plugin that calls another plugin."""
    
    def __init__(self, logger, plugin_collection):
        super().__init__(logger, plugin_collection)
        self.description = "Example async plugin"
        self.plugin_name = "PluginA"
        self.asynced = True  # This is an async plugin
    
    @async_log_errors
    async def perform_operation(self, argument):
        """Simplified example showing how to call another plugin using the one-liner."""
        # This single line replaces the entire try-except block, request creation,
        # context manager, and result handling
        result = await self.execute("PluginB.calculate_square", argument)
        
        if result is None:
            return f"Error calling PluginB.calculate_square with {argument}"
        
        return result
