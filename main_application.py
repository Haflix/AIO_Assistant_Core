import asyncio
from PluginCore import PluginCore
from decorators import async_log_errors

@async_log_errors
async def main():
    """Main function to demonstrate the plugin system."""
    # Initialize the plugin collection
    plugin_core = PluginCore('config.yml')
    
    # Wait for plugins to be loaded
    await plugin_core.wait_until_ready()

    
    nodes = await plugin_core.network.update_all_nodes()
    print("Discovered nodes:", nodes)

    #await plugin_core.purge_plugins()

    #print(result4)
    #while True:
        #await asyncio.sleep(10000)
    
    # Example of using the one-liner execute method
    result1 = await plugin_core.execute("PluginA", "perform_operation", 3, host="local")
    plugin_core._logger.info(f"Result from PluginA: {result1}")
    
    await asyncio.sleep(10000)
    
    result2 = await plugin_core.execute("PluginC", "perform_operation", 6)
    plugin_core._logger.info(f"Result from PluginC: {result2}")
    
    # Attempt to call a non-existent plugin - will return None due to error handling
    result3 = await plugin_core.execute("PluginD", "perform_operation")
    plugin_core._logger.info(f"Result from non-existent PluginD: {result3}")
    
    
    plugin_core._logger.info(plugin_core.plugins)
    
    await plugin_core.purge_plugins()
    
    plugin_core._logger.info(plugin_core.plugins)
    
    await plugin_core.get_plugins()
    
    plugin_core._logger.info(plugin_core.plugins)
    
    await plugin_core.start_plugins()
    
    plugin_core._logger.info(plugin_core.plugins)
    
    result1 = await plugin_core.execute("PluginA", "perform_operation", 4)
    plugin_core._logger.info(f"Result from PluginA: {result1}")
    
    await plugin_core.pop_plugin("PluginB")
    
    result1 = await plugin_core.execute("PluginC", "perform_operation")
    plugin_core._logger.info(f"Result from PluginC: {result1}")

async def shutdown(loop, signal=None):
    """Cleanup tasks tied to the service's shutdown."""
    if signal:
        print(f"Received exit signal {signal.name}...")
    
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [task.cancel() for task in tasks]
    
    print(f"Cancelling {len(tasks)} outstanding tasks")
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.create_task(main())
        loop.run_forever()
    finally:
        print("Successfully shutdown the service.")
        loop.close()