from utils import *
from logging import Logger

class PluginC(Plugin):
    """"""
    def __init__(self, logger: Logger, plugin_collection_c):
        super().__init__(logger, plugin_collection_c)
        self.description = "None"
        self.plugin_name = "PluginC"
        
    def perform_operation(self, argument):
        """Simulate a request to PluginB and use the context manager to handle errors and cleanup."""
        target = "PluginB.calculate_square"
        # Create the request asynchronously.
        request = self._plugin_collection.create_request("PluginC", target, argument, timeout=5)
        
        # Use the async context manager to await the result with cleanup.
        with self._plugin_collection.request_context(request) as result:
            # Use the result safely.
            return result
