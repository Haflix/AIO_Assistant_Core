from logging import Logger
import plugin_collection
import asyncio
from bleak import BleakScanner

class Bluetooth(plugin_collection.Plugin):
    """Bluetooth Plugin Alpha"""
    
    def __init__(self, logger: Logger, plugin_collection_c: plugin_collection.PluginCollection):
        super().__init__(logger, plugin_collection_c)
        self.description = "None"
        self.plugin_name = "Bluetooth"
        self.version = "0.0.1"
        self.asynced = True
        self.loop_req = True
        self.event_loop = asyncio.new_event_loop()
        
        
    def loop_start(self):
        #super().loop_start()
        self.loop_running = True
        try:
            self._logger.debug(f"Starting loop of '{self.plugin_name}'")
            asyncio.run(self.main())
        except Exception as e:
            self._logger.error(f"Error starting loop of '{self.plugin_name}': {e}")
        self.loop_running = True
    
    async def main(self):
        devices = await BleakScanner.discover()

        for device in devices:
            print(device)

    def perform_operation(self, argument):
        
        
        return