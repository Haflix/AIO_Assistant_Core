"""
AIO Dashboard Plugin — Textual-based TUI for the PluginCore.

Plugins can register custom TUI panels by implementing either:
  - get_tui_menu() -> dict    (declarative, no Textual dependency)
  - get_tui_widget() -> Widget (full Textual widget, more power)
"""

import asyncio
import importlib.util
import logging
import os
import sys
import threading

from utils import Plugin
from decorators import log_errors, async_log_errors

# ── Sibling module imports ────────────────────────────────────────────
_plugin_dir = os.path.dirname(os.path.abspath(__file__))

def _import_sibling(module_name: str):
    path = os.path.join(_plugin_dir, f"{module_name}.py")
    spec = importlib.util.spec_from_file_location(
        f"cli_dashboard.{module_name}", path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod

_log_handler_mod = _import_sibling("log_handler")
_request_tracker_mod = _import_sibling("request_tracker")
_app_mod = _import_sibling("app")

TUILogHandler = _log_handler_mod.TUILogHandler
DashboardApp = _app_mod.DashboardApp


class CLI(Plugin):
    """Dashboard TUI plugin for the PluginCore."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self._logger.debug("Dashboard plugin on_load")
        self._app = None
        self._tui_thread = None
        self._log_handler = TUILogHandler(max_buffer=1000)
        self._muted_handler = None
        self._main_loop = None

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("Dashboard plugin on_enable")

        # Remember the main event loop for shutdown signaling
        self._main_loop = asyncio.get_running_loop()

        # Install TUI log handler on root logger (buffers until widget attaches)
        root_logger = logging.getLogger()
        self._log_handler.setLevel(logging.DEBUG)
        root_logger.addHandler(self._log_handler)

        # Run TUI in its own thread with its own event loop.
        # This prevents PluginCore's blocking tasks (model loading, DB
        # schema creation) from starving Textual's message pump.
        self._tui_thread = threading.Thread(
            target=self._run_tui_thread,
            name="tui-thread",
            daemon=True,
        )
        self._tui_thread.start()

    def _mute_console(self):
        """Remove the console StreamHandler from QueueListener while TUI active,
        and switch FDRedirector streams to capture mode.

        All Python-level writes (sys.stdout, sys.stderr, sys.__stderr__, any
        cached reference held by third-party libs like loguru/HuggingFace)
        are routed to logging — except Textual's output thread which is
        exempt so it can still render to the real terminal.

        TUILogHandler shows all logs in the dashboard instead. File logging
        and DB logging are unaffected (separate handlers).
        """
        root = logging.getLogger()
        listener = getattr(root, "_queue_listener", None)
        if not listener:
            return
        console = root._custom_handlers[0] if getattr(root, "_custom_handlers", None) else None
        if console and console in listener.handlers:
            self._muted_handler = console
            listener.handlers = tuple(h for h in listener.handlers if h is not console)

        # Switch _MutableStream objects to capture mode.
        # Every reference to sys.stdout/stderr (including cached ones from
        # loguru, HuggingFace, tqdm, etc.) points to the same _MutableStream.
        # mute() makes all writes go to logging, except for Textual's
        # "textual-output" thread which still renders to the real terminal.
        redirector = getattr(root, "_fd_redirector", None)
        if redirector:
            redirector.mute(exempt_thread_names=("textual-output",))

    def _unmute_console(self):
        """Re-add the console StreamHandler to QueueListener and unmute streams."""
        # Unmute FDRedirector streams — all writes go back to real terminal
        root = logging.getLogger()
        redirector = getattr(root, "_fd_redirector", None)
        if redirector:
            redirector.unmute()

        if not self._muted_handler:
            return
        listener = getattr(root, "_queue_listener", None)
        if listener and self._muted_handler not in listener.handlers:
            listener.handlers = (*listener.handlers, self._muted_handler)
        self._muted_handler = None

    def _run_tui_thread(self):
        """Entry point for the TUI thread — creates its own event loop."""
        # Mute console logging — TUILogHandler shows logs in dashboard instead.
        # Textual manages sys.stdout itself during app.run().
        self._mute_console()

        self._app = DashboardApp(
            plugin_core=self._plugin_core,
            plugin_instance=self,
            log_handler=self._log_handler,
        )

        try:
            self._app.run()
        except Exception as e:
            self._logger.error(f"Dashboard app error: {e}")
        finally:
            self._unmute_console()
            self._logger.info("Dashboard TUI exited")
            # Signal shutdown on the main event loop
            if hasattr(self._plugin_core, "_shutdown_event"):
                if self._main_loop and self._main_loop.is_running():
                    self._main_loop.call_soon_threadsafe(
                        self._plugin_core._shutdown_event.set
                    )
                else:
                    self._plugin_core._shutdown_event.set()

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("Dashboard plugin on_disable")

        # Detach log handler
        root_logger = logging.getLogger()
        root_logger.removeHandler(self._log_handler)
        self._log_handler.detach()

        # Exit the app if still running
        if self._app and self._app.is_running:
            self._app.exit()

        # Wait for thread to finish
        if self._tui_thread and self._tui_thread.is_alive():
            self._tui_thread.join(timeout=5.0)

        # Restore console handler if not already done
        self._unmute_console()

        self._app = None
        self._tui_thread = None
