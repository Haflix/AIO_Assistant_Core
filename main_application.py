import asyncio
import signal
from PluginCore import PluginCore


async def main():
    pc = PluginCore("config.yml")
    await pc.wait_until_ready()

    # Plugins (e.g., DiscordBot /shutdown) set this event to trigger exit.
    pc._shutdown_event = asyncio.Event()

    # Handle Ctrl+C gracefully — set the shutdown event instead of killing the loop.
    # This lets graceful_shutdown() run (disabling plugins, closing connections).
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, pc._shutdown_event.set)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler for SIGINT in all contexts.
            # Fall back to signal.signal for Ctrl+C.
            signal.signal(sig, lambda s, f: pc._shutdown_event.set())

    await pc._shutdown_event.wait()
    print("Shutting down...")
    await pc.graceful_shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Interrupted by user")
    finally:
        print("Successfully shutdown the service.")
