from utils import Plugin
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors

class PluginC(Plugin):
    """Example of a synchronous plugin that calls an async plugin."""
    
    def __init__(self, logger, plugin_core, arguments):
        super().__init__(logger, plugin_core, arguments)


    @log_errors
    def on_load(self, *args, **kwargs):
        self._logger.debug(f"on_load")
        self._logger.debug(f"args sum({args}): {sum(args)}")
        self._logger.debug(f"kwargs ({kwargs}): {kwargs.get('variable')}")

    @log_errors
    def on_enable(self):
        self._logger.debug(f"on_enable")
        
    @log_errors
    def on_disable(self):
        self._logger.debug(f"on_disable")
    
    @log_errors
    def perform_operation(self, argument: int):
        """Call an async plugin from a sync plugin using the one-liner."""

        result = self.execute_sync("PluginB", "calculate_square", argument)
        
        if result is None:
            return f"Error calling PluginB.calculate_square with {argument}"
        
        return result
