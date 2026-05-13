"""
AIO Dashboard Plugin — Textual-based TUI for the PluginCore.

Plugins can register custom TUI panels by implementing either:
  - get_tui_module_info() -> dict  (Dashboard imports TUI package, recommended)
  - get_tui_menu() -> dict         (declarative, no Textual dependency)
"""

import asyncio
import collections
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
        f"tui_dashboard.{module_name}", path
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


class TUI(Plugin):
    """Dashboard TUI plugin for the PluginCore."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self._logger.debug("Dashboard plugin on_load")
        self._app = None
        self._tui_thread = None
        self._log_handler = TUILogHandler(max_buffer=1000)
        self._muted_handler = None
        self._main_loop = None
        self._observed_topics: list = []

        # Phase 2 — plugin-side observer state.
        #
        # The Networking-tab cluster summary, disconnect-reason counters,
        # event log strip, and per-peer drill-down event log all read from
        # state mutated by `_on_peer_event`. The observer runs on the
        # PluginCore event loop thread; the TUI reads from its own thread.
        # `_observer_lock` (threading.Lock) keeps the two dicts coherent
        # under concurrent read + observer-fire. The deque + dict live
        # here (NOT on DashboardApp) so events arriving BEFORE the TUI
        # thread mounts the App are captured — the TUI hydrates from the
        # deque on tab mount.
        self._recent_peer_events: collections.deque = collections.deque(maxlen=500)
        # Monotonic counter incremented on every peer event ever observed
        # (NOT bounded by deque maxlen). The TUI's baseline-dedup gate
        # uses this to detect "is there a new event since I snapshotted"
        # because `len(deque)` plateaus at maxlen and stops being a
        # reliable signal once the ring buffer wraps.
        self._event_seq: int = 0
        self._disconnect_reason_counts: dict = {
            "normal": 0, "connection_error": 0, "rce_attempt": 0, "error": 0,
        }
        self._observer_lock = threading.Lock()

    @async_log_errors
    async def on_enable(self):
        self._logger.debug("Dashboard plugin on_enable")

        # Remember the main event loop for shutdown signaling
        self._main_loop = asyncio.get_running_loop()

        # Install TUI log handler on root logger (buffers until widget attaches)
        root_logger = logging.getLogger()
        self._log_handler.setLevel(logging.DEBUG)
        root_logger.addHandler(self._log_handler)

        # Phase 2 — clear plugin-side state before re-subscribing so a
        # disable -> re-enable cycle starts with a fresh deque + zeroed
        # counters. Events arriving during the disable window are lost
        # regardless (observer was unregistered); mixing pre-disable and
        # post-enable events in the same deque has no useful semantic.
        with self._observer_lock:
            self._recent_peer_events.clear()
            self._event_seq = 0
            for k in self._disconnect_reason_counts:
                self._disconnect_reason_counts[k] = 0

        # Phase 1: subscribe to internal-bus topics for live-update of
        # the Networking tab. Registration happens on the loop thread
        # (this method's caller); callbacks bridge to the TUI thread
        # via `app.call_from_thread`. The `app and app.is_running`
        # guard in each callback covers the startup race window where
        # the TUI thread hasn't constructed the App yet.
        self._observed_topics = [
            ("_core/peer/connected", self._on_peer_event),
            ("_core/peer/disconnected", self._on_peer_event),
        ]
        for topic, cb in self._observed_topics:
            self.internal_observe(topic, cb)

        # Run TUI in its own thread with its own event loop.
        # This prevents PluginCore's blocking tasks (model loading, DB
        # schema creation) from starving Textual's message pump.
        self._tui_thread = threading.Thread(
            target=self._run_tui_thread,
            name="tui-thread",
            daemon=True,
        )
        self._tui_thread.start()

    # ── Internal-bus observers (loop thread) ───────────────────────────
    # Callbacks must return quickly (< 1ms per PluginCore.internal_observe
    # contract). We bridge to the TUI thread via `app.call_from_thread`
    # so DOM mutations happen on the right loop.

    @log_errors
    def _on_peer_event(self, topic: str, payload: dict) -> None:
        """Forward peer-state changes to the Dashboard's TUI thread.

        Shutdown race: the `is_running` check and the `call_from_thread`
        call are not atomic — `app.exit()` may fire between them. We
        wrap the dispatch in try/except to swallow the
        `NoActiveAppError` (or version-equivalent) Textual raises when
        the app is mid-teardown. The TUI is going away anyway; losing
        the last few peer events is acceptable.

        Phase 2 — record state under `_observer_lock` BEFORE the TUI
        bridge so the TUI's snapshot reads (which take the same lock)
        see consistent state. NO `await` may be added under this lock —
        we run on the asyncio loop thread, and re-entering would let
        another observer fire mid-mutation.
        """
        with self._observer_lock:
            # Payload is appended by reference. ALL CONSUMERS MUST TREAT
            # IT AS READ-ONLY — the framework's emit site does not share
            # the dict between observers today, so mutating here would
            # also break those future observers if they ever land.
            self._recent_peer_events.append((topic, payload))
            self._event_seq += 1
            if topic == "_core/peer/disconnected":
                reason = payload.get("reason", "normal")
                if reason in self._disconnect_reason_counts:
                    self._disconnect_reason_counts[reason] += 1

        app = self._app
        if app is None or not app.is_running:
            return
        try:
            app.call_from_thread(app.on_peer_event_bus, topic, payload)
        except Exception:
            pass

    # ── Plugin-side state snapshot helpers (TUI thread → plugin) ───────
    # All three methods are safe to call from any thread. They take
    # `_observer_lock` briefly; no `await` happens under the lock so
    # they cannot deadlock the asyncio loop. The returned values are
    # FRESH copies; the caller may mutate them freely. Payload dicts
    # inside `get_recent_peer_events()` are shared references — TUI
    # readers MUST treat them as read-only.

    def get_recent_peer_events(self) -> list:
        """Snapshot the recent-peer-event deque.

        Returns a fresh list whose elements are the SAME (topic, payload)
        tuples held by the deque. Payload dicts are NOT copied — treat
        them as read-only.
        """
        with self._observer_lock:
            return list(self._recent_peer_events)

    def get_event_seq(self) -> int:
        """Snapshot the monotonic peer-event counter.

        Used by the TUI's baseline-dedup gate to detect "did a new event
        arrive since the snapshot" — `len(_recent_peer_events)` is an
        unreliable signal once the deque reaches its maxlen and wraps.
        """
        with self._observer_lock:
            return self._event_seq

    def get_disconnect_reason_counts(self) -> dict:
        """Snapshot disconnect-reason counts. Returns a fresh dict; caller
        may mutate freely."""
        with self._observer_lock:
            return dict(self._disconnect_reason_counts)

    def clear_disconnect_reason_counts(self) -> None:
        """Reset disconnect-reason counts to zero. Thread-safe."""
        with self._observer_lock:
            for k in self._disconnect_reason_counts:
                self._disconnect_reason_counts[k] = 0

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
            # Signal shutdown on the main event loop.
            # `asyncio.Event.set` is not thread-safe; if the main loop
            # is running we hop onto it. If it has already stopped,
            # the sync fallback may raise on a closed loop — swallow
            # since the framework is shutting down anyway and losing
            # the signal in that narrow window is preferable to a
            # crash on the TUI thread.
            if hasattr(self._plugin_core, "_shutdown_event"):
                if self._main_loop and self._main_loop.is_running():
                    try:
                        self._main_loop.call_soon_threadsafe(
                            self._plugin_core._shutdown_event.set
                        )
                    except RuntimeError:
                        pass
                else:
                    try:
                        self._plugin_core._shutdown_event.set()
                    except RuntimeError:
                        pass

    @async_log_errors
    async def on_disable(self):
        self._logger.debug("Dashboard plugin on_disable")

        # Phase 1: unregister internal-bus observers. PluginCore's
        # `_unobserve_plugin` (called on plugin pop) cleans these up
        # automatically, but explicit unregister keeps the dicts tidy
        # under disable/re-enable churn.
        for topic, cb in getattr(self, "_observed_topics", []):
            try:
                self.internal_unobserve(topic, cb)
            except Exception:
                pass
        self._observed_topics = []

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
