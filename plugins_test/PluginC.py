from utils import Plugin
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors
import asyncio

class PluginC(Plugin):
    """Example of a synchronous plugin that calls an async plugin."""
    
    def __init__(self, logger, plugin_collection):
        super().__init__(logger, plugin_collection)
        self.description = "Example sync plugin"
        self.plugin_name = "PluginC"
        self.asynced = False  # This is a sync plugin
    
    @log_errors
    def perform_operation(self, argument):
        """Call an async plugin from a sync plugin using the one-liner."""
        # One-liner with automatic error handling
        result = self.execute_sync("PluginB.calculate_square", argument)
        
        if result is None:
            return f"Error calling PluginB.calculate_square with {argument}"
        
        return result
