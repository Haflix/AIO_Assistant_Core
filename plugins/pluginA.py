from logging import Logger
import plugin_collection
import time

class PluginA(plugin_collection.Plugin):
    """PluginA interacts with PluginB by creating a request."""
    
    def __init__(self, logger: Logger, plugin_collection_c: plugin_collection.PluginCollection):
        super().__init__(logger, plugin_collection_c)
        self.description = "None"
        self.plugin_name = "PluginA"

    def perform_operation(self, argument):
        """Simulate a request to PluginB."""
        target = "PluginB.calculate_square"
        args = argument

        # Create a request targeting PluginB
        request = self._plugin_collection.create_request_wait(
            author="PluginA",
            target=target,
            args=args,
            timeout=5
        )
        
        return request.get_result()