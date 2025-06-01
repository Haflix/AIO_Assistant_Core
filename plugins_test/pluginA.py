from logging import Logger
from utils import *

class PluginA(Plugin):
    """PluginA interacts with PluginB by creating a request."""
    
    def __init__(self, logger: Logger, plugin_collection_c):
        super().__init__(logger, plugin_collection_c)
        self.description = "None"
        self.plugin_name = "PluginA"

    async def perform_operation(self, argument):
        """Simulate a request to PluginB."""
        target = "PluginB.calculate_square"
        args = argument
        # Create a request targeting PluginB and wait for its result asynchronously.
        request = await self._plugin_collection.create_request(
            author="PluginA",
            target=target,
            args=args,
            timeout=5
        )
        # Use the async context manager to await the result with cleanup.
        with self._plugin_collection.request_context(request) as result:
            # Use the result safely.
            return result
