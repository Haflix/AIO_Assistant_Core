from logging import Logger
import asyncio
from utils import *

class PluginB(Plugin):
    """PluginB provides a method to calculate the square of a number."""
    
    def __init__(self, logger: Logger, plugin_collection_c):
        super().__init__(logger, plugin_collection_c)
        self.description = "None"
        self.plugin_name = "PluginB"

    async def calculate_square(self, number):
        """Calculate the square of a number asynchronously."""
        await asyncio.sleep(10)  # Replace blocking sleep with async sleep.
        try:
            result = number ** 2
            self._logger.info(f"Calculated square of {number}: {result}")
            return result
        except Exception as e:
            self._logger.error(f"Error calculating square: {e}")
            raise
