import asyncio
from plugin_collection import PluginCollection
from decorators import async_log_errors

@async_log_errors
async def main():
    """Main function to demonstrate the plugin system."""
    # Initialize the plugin collection
    plugin_collection = PluginCollection('config.yml')
    
    # Wait for plugins to be loaded
    await plugin_collection.wait_until_ready()
    
    # Start the maintenance loop
    asyncio.create_task(plugin_collection.running_loop())
    
    # Example of using the one-liner execute method
    result1 = await plugin_collection.execute("PluginA", "perform_operation", 3)
    print(f"Result from PluginA: {result1}")
    
    result2 = await plugin_collection.execute("PluginC", "perform_operation", 6)
    print(f"Result from PluginC: {result2}")
    
    # Attempt to call a non-existent plugin - will return None due to error handling
    result3 = await plugin_collection.execute("PluginD", "perform_operation")
    print(f"Result from non-existent PluginD: {result3}")

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