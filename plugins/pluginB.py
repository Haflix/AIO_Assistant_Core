from logging import Logger
import plugin_collection
import time

class PluginB(plugin_collection.Plugin):
    """PluginA interacts with PluginB by creating a request."""
    
    def __init__(self, logger: Logger, plugin_collection_c: plugin_collection.PluginCollection):
        super().__init__(logger, plugin_collection_c)
        self.description = "None"
        self.plugin_name = "PluginB"

    def calculate_square(self, number):
        """Calculate the square of a number."""
        time.sleep(10)
        try:
            result = number ** 2 
            self._logger.info(f"Calculated square of {number}: {result}")
            return result
        except Exception as e:
            self._logger.error(f"Error calculating square: {e}")
            raise