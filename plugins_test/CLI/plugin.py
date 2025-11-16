from utils import Plugin
from decorators import log_errors, async_log_errors, async_handle_errors
import prompt_toolkit
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import print_formatted_text
import asyncio
import shlex
import json
import os
from typing import Optional, List, Dict, Any
from exceptions import RequestException


class CLI(Plugin):
    """CLI plugin that allows you to run commands and scripts with plugins"""

    @log_errors
    def on_load(self, *args, **kwargs):
        self._logger.debug(f"on_load")
        if not prompt_toolkit.__version__.startswith("3."):
            self._logger.error(f"Prompt Toolkit 3.0.0 or higher is required")
            raise RuntimeError("Prompt Toolkit 3.0.0 or higher is required")

        self._running = False
        self._input_task = None
        self._session = None
        self._background_tasks = []

    @async_log_errors
    async def on_enable(self):
        self._logger.debug(f"on_enable")
        self._running = True
        self._session = PromptSession()
        self._input_task = asyncio.create_task(self._command_loop())

    @async_log_errors
    async def on_disable(self):
        self._logger.debug(f"on_disable")
        self._running = False
        if self._input_task:
            self._input_task.cancel()
            try:
                await self._input_task
            except asyncio.CancelledError:
                pass
        self._input_task = None
        self._session = None

        # Cancel and clean up background tasks
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        # Wait for tasks to complete cancellation
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks = []

    async def _command_loop(self):
        """Main command input loop that doesn't block the event loop."""
        # Keep patch_stdout active to protect prompt from background output
        with patch_stdout():
            while self._running:
                try:
                    cmd = await self._session.prompt_async("AIO> ")
                    if cmd and cmd.strip():
                        await self._process_command(cmd.strip())
                except (EOFError, KeyboardInterrupt):
                    print_formatted_text("\nUse 'shutdown' command to exit gracefully.")
                    continue
                except Exception as e:
                    self._logger.exception(f"Error in command loop: {e}")
                    print_formatted_text(f"Error: {e}")

    def _parse_json_args(self, args: List[str]) -> Optional[Any]:
        """Parse JSON arguments from a list of string arguments.

        Handles cases where JSON might be split across multiple arguments
        or provided as a single quoted/unquoted string.
        """
        if not args:
            return None

        # Try joining all arguments first (handles unquoted JSON split by spaces)
        joined = " ".join(args)
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            pass

        # Try parsing as single argument (handles quoted JSON)
        if len(args) == 1:
            try:
                return json.loads(args[0])
            except json.JSONDecodeError:
                pass

        # If all else fails, raise an error
        raise json.JSONDecodeError(
            f"Could not parse JSON from arguments: {args}", joined, 0
        )

    async def _process_command(self, cmd_str: str):
        """Parse and dispatch commands."""
        try:
            parts = shlex.split(cmd_str)
            if not parts:
                return

            command = parts[0].lower()
            args = parts[1:] if len(parts) > 1 else []

            if command == "help":
                await self._cmd_help()
            elif command == "list":
                await self._cmd_list()
            elif command == "info":
                if args:
                    await self._cmd_info(args[0])
                else:
                    print("Usage: info <plugin_name>")
            elif command == "endpoints":
                if args:
                    await self._cmd_endpoints(args[0])
                else:
                    print("Usage: endpoints <plugin_name>")
            elif command == "status":
                await self._cmd_status()
            elif command == "load":
                if args:
                    await self._cmd_load(args[0])
                else:
                    print("Usage: load <plugin_name>")
            elif command == "enable":
                if args:
                    await self._cmd_enable(args[0])
                else:
                    print("Usage: enable <plugin_name>")
            elif command == "disable":
                if args:
                    await self._cmd_disable(args[0])
                else:
                    print("Usage: disable <plugin_name>")
            elif command == "reload":
                if args:
                    await self._cmd_reload(args[0])
                else:
                    print("Usage: reload <plugin_name>")
            elif command == "pop":
                if args:
                    await self._cmd_pop(args[0])
                else:
                    print("Usage: pop <plugin_name>")
            elif command == "purge":
                await self._cmd_purge()
            elif command == "purge_safe":
                await self._cmd_purge_safe()
            elif command == "call":
                if len(args) >= 2:
                    plugin = args[0]
                    method = args[1]
                    json_args = None
                    if len(args) > 2:
                        try:
                            json_args = self._parse_json_args(args[2:])
                        except json.JSONDecodeError as e:
                            print(f"Invalid JSON arguments: {e}")
                            print('Example: call PluginA method_name {"key": "value"}')
                            print(
                                '        call PluginA method_name \'{"key": "value"}\''
                            )
                            return
                    await self._cmd_call(plugin, method, json_args)
                else:
                    print("Usage: call <plugin> <method> [json_args]")
                    print('Example: call PluginA method_name {"key": "value"}')
                    print('        call PluginA method_name \'{"key": "value"}\'')
            elif command == "stream":
                if len(args) >= 2:
                    plugin = args[0]
                    method = args[1]
                    json_args = None
                    if len(args) > 2:
                        try:
                            json_args = self._parse_json_args(args[2:])
                        except json.JSONDecodeError as e:
                            print(f"Invalid JSON arguments: {e}")
                            print(
                                'Example: stream PluginA method_name {"key": "value"}'
                            )
                            print(
                                '        stream PluginA method_name \'{"key": "value"}\''
                            )
                            return
                    await self._cmd_stream(plugin, method, json_args)
                else:
                    print("Usage: stream <plugin> <method> [json_args]")
                    print('Example: stream PluginA method_name {"key": "value"}')
                    print('        stream PluginA method_name \'{"key": "value"}\'')
            elif command == "shutdown":
                await self._cmd_shutdown()
            elif command == "clear":
                os.system("cls" if os.name == "nt" else "clear")
            elif command == "config":
                if len(args) > 0 and args[0] == "show":
                    await self._cmd_config_show()
                elif len(args) > 0 and args[0] == "edit":
                    await self._cmd_config_edit()
                else:
                    print("Usage: config show | config edit")
            else:
                print(f"Unknown command: {command}")
                print("Type 'help' for available commands")
        except Exception as e:
            self._logger.exception(f"Error processing command: {e}")
            print(f"Error: {e}")

    async def _cmd_help(self):
        """Display help information."""
        help_text = """
Available Commands:

Plugin Management:
  load <plugin_name>           - Load a plugin from config
  enable <plugin_name>         - Enable a plugin
  disable <plugin_name>        - Disable a plugin
  reload <plugin_name>         - Reload a plugin
  pop <plugin_name>            - Remove a plugin
  purge                        - Remove all plugins
  purge_safe                   - Remove all plugins except CLI

Inspection:
  list                         - List all loaded plugins
  info <plugin_name>           - Show plugin information
  endpoints <plugin_name>      - Show plugin endpoints
  status                       - Show system status

Execution:
  call <plugin> <method> [args] - Call a plugin method (args as JSON)
  stream <plugin> <method> [args] - Stream from a plugin method (args as JSON)

System:
  shutdown                     - Gracefully shutdown the system
  clear                        - Clear the screen
  config show                  - Show current config
  config edit                  - Show instructions for editing config
  help                         - Show this help message

Examples:
  call PluginA perform_operation {"argument": 5}
  stream PluginA perform_operation_stream {"argument": 5}
  info PluginA
  endpoints PluginA
"""
        print(help_text)

    async def _cmd_list(self):
        """List all loaded plugins."""
        async with self._plugin_core.plugin_lock:
            plugins = {
                name: plugin for name, plugin in self._plugin_core.plugins.items()
            }

        if not plugins:
            print("No plugins loaded.")
            return

        print(f"\nLoaded plugins ({len(plugins)}):")
        for name in sorted(plugins.keys()):
            plugin = plugins.get(name)
            if plugin:
                status = "ENABLED" if plugin.enabled else "DISABLED"
                remote = "REMOTE" if getattr(plugin, "remote", False) else "LOCAL"
                print(
                    f"  {name:20} [{status:8}] [{remote:6}] v{getattr(plugin, 'version', '?')}"
                )

    async def _cmd_info(self, plugin_name: str):
        """Show detailed information about a plugin."""
        info = await self._plugin_core.get_plugin_info(plugin_name)
        if not info:
            print(f"Plugin '{plugin_name}' not found.")
            return

        print(f"\nPlugin: {info['name']}")
        print(f"  Version: {info['version']}")
        print(f"  UUID: {info['uuid']}")
        print(f"  Enabled: {info['enabled']}")
        print(f"  Remote: {info['remote']}")
        print(f"  Description: {info['description']}")
        if info["arguments"]:
            print(f"  Arguments: {info['arguments']}")

    async def _cmd_endpoints(self, plugin_name: str):
        """Show all endpoints for a plugin."""
        endpoints = await self._plugin_core.get_plugin_endpoints(plugin_name)
        if endpoints is None:
            print(f"Plugin '{plugin_name}' not found.")
            return

        if not endpoints:
            print(f"Plugin '{plugin_name}' has no endpoints defined.")
            return

        print(f"\nEndpoints for {plugin_name}:")
        for ep in endpoints:
            print(f"  {ep['access_name']} -> {ep['internal_name']}")
            print(
                f"    Remote: {ep['remote']}, Accessible by other plugins: {ep['accessible_by_other_plugins']}"
            )
            if ep.get("description"):
                print(f"    Description: {ep['description']}")
            if ep.get("tags"):
                print(f"    Tags: {ep['tags']}")

    async def _cmd_status(self):
        """Show system status."""
        async with self._plugin_core.plugin_lock:
            total_plugins = len(self._plugin_core.plugins)
            enabled_plugins = sum(
                1 for p in self._plugin_core.plugins.values() if p.enabled
            )
            networking_enabled = getattr(self._plugin_core, "networking_enabled", False)
            hostname = getattr(self._plugin_core, "hostname", "unknown")

        print(f"\nSystem Status:")
        print(f"  Hostname: {hostname}")
        print(f"  Plugins: {enabled_plugins}/{total_plugins} enabled")
        print(f"  Networking: {'Enabled' if networking_enabled else 'Disabled'}")

    async def _cmd_load(self, plugin_name: str):
        """Load a plugin from config."""
        try:
            entry = next(
                (
                    p
                    for p in self._plugin_core.yaml_config.get("plugins", [])
                    if p.get("name") == plugin_name
                ),
                None,
            )
            if not entry:
                print(f"Plugin '{plugin_name}' not found in config.")
                return

            await self._plugin_core.load_plugin_with_conf(entry)
            print(f"Plugin '{plugin_name}' loaded successfully.")
        except Exception as e:
            self._logger.exception(f"Error loading plugin {plugin_name}: {e}")
            print(f"Error loading plugin: {e}")

    async def _cmd_enable(self, plugin_name: str):
        """Enable a plugin."""
        try:
            if plugin_name not in self._plugin_core.plugins:
                print(f"Plugin '{plugin_name}' not found.")
                return

            await self._plugin_core._enable_plugin(plugin_name)
            print(f"Plugin '{plugin_name}' enabled.")
        except Exception as e:
            self._logger.exception(f"Error enabling plugin {plugin_name}: {e}")
            print(f"Error enabling plugin: {e}")

    async def _cmd_disable(self, plugin_name: str):
        """Disable a plugin."""
        try:
            if plugin_name not in self._plugin_core.plugins:
                print(f"Plugin '{plugin_name}' not found.")
                return

            await self._plugin_core._disable_plugin(plugin_name)
            print(f"Plugin '{plugin_name}' disabled.")
        except Exception as e:
            self._logger.exception(f"Error disabling plugin {plugin_name}: {e}")
            print(f"Error disabling plugin: {e}")

    async def _cmd_reload(self, plugin_name: str):
        """Reload a plugin."""
        try:
            await self._plugin_core._reload_plugin(plugin_name)
            print(f"Plugin '{plugin_name}' reloaded successfully.")
        except Exception as e:
            self._logger.exception(f"Error reloading plugin {plugin_name}: {e}")
            print(f"Error reloading plugin: {e}")

    async def _cmd_pop(self, plugin_name: str):
        """Remove a plugin."""
        try:
            if plugin_name == self.plugin_name:
                print("Cannot remove CLI plugin. Use 'shutdown' to exit.")
                return

            await self._plugin_core.pop_plugin(plugin_name)
            print(f"Plugin '{plugin_name}' removed.")
        except Exception as e:
            self._logger.exception(f"Error removing plugin {plugin_name}: {e}")
            print(f"Error removing plugin: {e}")

    async def _cmd_purge(self):
        """Remove all plugins."""
        try:
            response = await self._session.prompt_async(
                "This will remove ALL plugins including CLI. Continue? (yes/no): "
            )
            if response.lower() != "yes":
                print("Purge cancelled.")
                return

            await self._plugin_core.purge_plugins()
            print("All plugins purged.")
        except Exception as e:
            self._logger.exception(f"Error purging plugins: {e}")
            print(f"Error purging plugins: {e}")

    async def _cmd_purge_safe(self):
        """Remove all plugins except CLI."""
        try:
            async with self._plugin_core.plugin_lock:
                plugins_to_purge = [
                    name
                    for name in self._plugin_core.plugins.keys()
                    if name != self.plugin_name
                ]

            if not plugins_to_purge:
                print("No plugins to purge (only CLI is loaded).")
                return

            print(
                f"This will remove {len(plugins_to_purge)} plugin(s): {', '.join(plugins_to_purge)}"
            )
            response = await self._session.prompt_async("Continue? (yes/no): ")
            if response.lower() != "yes":
                print("Purge cancelled.")
                return

            await self._plugin_core.purge_plugins_except([self.plugin_name])
            print(f"Purged {len(plugins_to_purge)} plugin(s). CLI remains active.")
        except Exception as e:
            self._logger.exception(f"Error purging plugins: {e}")
            print(f"Error purging plugins: {e}")

    async def _cmd_call(self, plugin: str, method: str, args: Any):
        """Call a plugin method in the background."""

        async def _background_call():
            try:
                result = await self.execute(plugin, method, args, host="any")
                self._logger.info(f"Call {plugin}.{method} completed. Result: {result}")
            except RequestException as e:
                self._logger.error(f"Request error calling {plugin}.{method}: {e}")
            except Exception as e:
                self._logger.exception(f"Error calling {plugin}.{method}: {e}")

        task = asyncio.create_task(_background_call())
        self._background_tasks.append(task)
        # Clean up completed tasks
        self._background_tasks = [t for t in self._background_tasks if not t.done()]
        print_formatted_text(
            f"Started background call: {plugin}.{method} (check logs for result)"
        )

    async def _cmd_stream(self, plugin: str, method: str, args: Any):
        """Stream from a plugin method in the background."""

        async def _background_stream():
            try:
                self._logger.info(f"Starting stream from {plugin}.{method}")
                async for result in self.execute_stream(
                    plugin, method, args, host="any"
                ):
                    self._logger.info(f"Stream {plugin}.{method}: {result}")
                self._logger.info(f"Stream {plugin}.{method} completed")
            except RequestException as e:
                self._logger.error(
                    f"Request error streaming from {plugin}.{method}: {e}"
                )
            except Exception as e:
                self._logger.exception(f"Error streaming from {plugin}.{method}: {e}")

        task = asyncio.create_task(_background_stream())
        self._background_tasks.append(task)
        # Clean up completed tasks
        self._background_tasks = [t for t in self._background_tasks if not t.done()]
        print_formatted_text(
            f"Started background stream: {plugin}.{method} (check logs for results)"
        )

    async def _cmd_shutdown(self):
        """Gracefully shutdown the system."""
        try:
            response = await self._session.prompt_async(
                "Shutdown the system? (yes/no): "
            )
            if response.lower() != "yes":
                print("Shutdown cancelled.")
                return

            print("Shutting down...")
            await self._plugin_core.graceful_shutdown()
            # Stop the event loop if we're in the same thread
            if (
                self._plugin_core.main_event_loop
                and self._plugin_core.main_event_loop.is_running()
            ):
                # Schedule stop for next iteration to avoid stopping from within a callback
                asyncio.create_task(self._stop_loop())
        except Exception as e:
            self._logger.exception(f"Error during shutdown: {e}")
            print(f"Error during shutdown: {e}")

    async def _cmd_config_show(self):
        """Show current config."""
        try:
            config_path = self._plugin_core.config_path
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    content = f.read()
                print(f"\nConfig file ({config_path}):")
                print("=" * 60)
                print(content)
                print("=" * 60)
            else:
                print(f"Config file not found: {config_path}")
        except Exception as e:
            self._logger.exception(f"Error showing config: {e}")
            print(f"Error: {e}")

    async def _stop_loop(self):
        """Helper to stop the event loop after a short delay."""
        await asyncio.sleep(0.1)  # Small delay to let current operations complete
        if (
            self._plugin_core.main_event_loop
            and self._plugin_core.main_event_loop.is_running()
        ):
            self._plugin_core.main_event_loop.stop()

    async def _cmd_config_edit(self):
        """Show instructions for editing config."""
        config_path = self._plugin_core.config_path
        print(f"\nTo edit the configuration:")
        print(f"  1. Edit the file: {config_path}")
        print(f"  2. Restart the application for changes to take effect")
        print(f"\nNote: Some settings cannot be changed while the program is running.")
        print(f"      Changes will only apply after a restart.")
