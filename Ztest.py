import asyncio

async def async_add(a, b):
    await asyncio.sleep(1)
    return a + b

def sync_add(a, b):
    return a + b

class Example:
    def __init__(self, asynced):
        self.asynced = asynced

    def comm_layer(self, function, *args):
        if self.asynced:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(function(*args))  # Correctly passing arguments
        else:
            return function(*args)  # Correctly passing arguments

# Test
example_sync = Example(asynced=False)
example_async = Example(asynced=True)

print(example_sync.comm_layer(sync_add, 3, 5))   # Output: 8
print(example_async.comm_layer(async_add, 3, 5))  # Output: 8 (after 1 sec delay)
