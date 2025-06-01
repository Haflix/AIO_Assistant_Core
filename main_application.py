"""Main applicatoin that demonstrates the functionality of
the dynamic plugins and the PluginCollection class
"""

from plugin_collection import PluginCollection
import time
import asyncio

async def main():
    """main function that runs the application
    """
    plugin_collection = PluginCollection('plugins_test')
    
    await plugin_collection.wait_until_ready()
    
    asyncio.create_task(plugin_collection.loop())
    
    #await asyncio.sleep(5)
    
    request = await plugin_collection.create_request(
            author="main",
            target="PluginA.perform_operation",
            args=int(3),
            timeout=3
        )
    async with plugin_collection.request_context(request) as result:
            # Use the result safely.
            print(result)
    
    #request = await plugin_collection.create_request(
    #        author="main",
    #        target="PluginC.perform_operation",
    #        args=int(4),
    #        timeout=None
    #    )
    #async with plugin_collection.request_context(request) as result:
    #        # Use the result safely.
    #        print(result)
    #
    #request = await plugin_collection.create_request(
    #        author="main",
    #        target="PluginD.perform_operation",
    #        args=int(4),
    #        timeout=None
    #    )
    #async with plugin_collection.request_context(request) as result:
    #        # Use the result safely.
    #        print(result)
    
    #asyncio.create_task(plugin_collection.loop())
    #print("Ende")


if __name__ == '__main__':
    #main()
    #asyncio.run(main())
    asyncio.ensure_future(main())
    asyncio.get_event_loop().run_forever()
