"""Main applicatoin that demonstrates the functionality of
the dynamic plugins and the PluginCollection class
"""

from plugin_collection import PluginCollection
import time
import asyncio

async def main():
    """main function that runs the application
    """
    plugin_collection = PluginCollection('plugins', host=True)
    #request = plugin_collection.create_request_wait( # NOTE: works but blocks the loop entirely
    #        author="main",
    #        target="PluginA.perform_operation",
    #        args=int(3),
    #        timeout=None
    #    )
    #print(request.result)
    
    #asyncio.create_task(plugin_collection.loop())
    #print("Ende")
    #my_plugins.apply_all_plugins_on_value(5)


if __name__ == '__main__':
    #main()
    asyncio.ensure_future(main())
    asyncio.get_event_loop().run_forever()
