from utils import Plugin
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors

class PluginC(Plugin):
    """Example of a synchronous plugin that calls an async plugin."""
    
    def __init__(self, logger, plugin_collection, *args, **kwargs):
        super().__init__(logger, plugin_collection)
        self.example_args = args
        self.example_kwargs = kwargs
        
        self._logger.debug(f"args sum({args}): {sum(args)}")
        self._logger.debug(f"kwargs ({kwargs}): {kwargs.get('variable')}")

    
    @log_errors()
    def perform_operation(self, argument):
        """Call an async plugin from a sync plugin using the one-liner."""

        result = self.execute_sync("PluginB.calculate_square", argument)
        
        if result is None:
            return f"Error calling PluginB.calculate_square with {argument}"
        
        return result
