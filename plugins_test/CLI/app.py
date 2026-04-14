"""
AIO Dashboard — Textual TUI for the PluginCore.

Tabs:
  1. Home:    system stats, plugin health, active requests, network nodes
  2. Plugins: searchable list, detail panel with stats, per-plugin actions
  3. Config:  YAML editor with file picker, backup-on-save
  4. Logs:    live log viewer with level filter, auto-scroll toggle
  5. Settings: TUI refresh rates, PluginCore info, networking display
  Dynamic:    per-plugin tabs with collapsible endpoints, form/JSON input
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Dict, Optional

import yaml
from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.css.query import NoMatches
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    DataTable,
    Footer,
    Header,
    Input,
    RichLog,
    Rule,
    Select,
    Sparkline,
    Static,
    Switch,
    TabbedContent,
    TabPane,
    TextArea,
)

# ── Import siblings ──────────────────────────────────────────────────
import sys as _sys

if "cli_dashboard.log_handler" in _sys.modules:
    TUILogHandler = _sys.modules["cli_dashboard.log_handler"].TUILogHandler
else:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "cli_dashboard.log_handler",
        os.path.join(os.path.dirname(__file__), "log_handler.py"),
    )
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    TUILogHandler = _mod.TUILogHandler

if "cli_dashboard.request_tracker" in _sys.modules:
    RequestTracker = _sys.modules["cli_dashboard.request_tracker"].RequestTracker
else:
    import importlib.util as _ilu2
    _spec2 = _ilu2.spec_from_file_location(
        "cli_dashboard.request_tracker",
        os.path.join(os.path.dirname(__file__), "request_tracker.py"),
    )
    _mod2 = _ilu2.module_from_spec(_spec2)
    _spec2.loader.exec_module(_mod2)
    RequestTracker = _mod2.RequestTracker


# ─── Defaults ────────────────────────────────────────────────────────
MAX_GRAPH_POINTS = 60
DEFAULT_STATS_INTERVAL = 2.0
DEFAULT_PLUGIN_INTERVAL = 3.0
DEFAULT_REQUEST_INTERVAL = 1.0


# ─── CSS ─────────────────────────────────────────────────────────────
APP_CSS = """
Screen {
    background: #1e1e1e;
    color: #d4d4d4;
}

Header {
    background: #2d2d2d;
    color: #e0e0e0;
}

Footer {
    background: #2d2d2d;
    color: #808080;
}

/* ── Home ────────────────────────────────── */
#home-scroll { height: 1fr; }
.stat-row { height: auto; layout: horizontal; padding: 0 0 1 0; }
.stat-card {
    border: round #404040;
    padding: 0 1;
    margin: 0 1 0 0;
    min-width: 20;
    height: 3;
    background: #2d2d2d;
}

.stat-key { color: #808080; width: auto; }
.stat-val { color: #d4d4d4; text-style: bold; width: 1fr; }
.stat-val-good { color: #73c991; text-style: bold; width: 1fr; }
.stat-val-warn { color: #cca75a; text-style: bold; width: 1fr; }
.stat-val-bad { color: #d16969; text-style: bold; width: 1fr; }

#request-table { height: auto; max-height: 14; border: round #404040; background: #2d2d2d; }
#request-empty { height: auto; padding: 0 1; }
#network-empty { height: auto; padding: 0 1; }
#network-section { height: auto; }

.section-header {
    color: #c7a06e;
    text-style: bold;
    padding: 1 0 0 0;
}

#graphs-section { height: auto; }
.graph-box {
    border: round #404040;
    padding: 0 1;
    margin: 0 1 0 0;
    height: 8;
    background: #2d2d2d;
}
.graph-header { height: auto; layout: horizontal; }
.graph-title { color: #9bb5a0; text-style: bold; width: auto; }
.graph-value { color: #d4d4d4; text-style: bold; width: 1fr; text-align: right; }
#graph-toggles { height: auto; layout: horizontal; padding: 0 0 1 0; }

/* ── Plugins ─────────────────────────────── */
#plugins-scroll { height: 1fr; }
#plugin-search { margin: 0 0 1 0; }
#plugin-table { height: 1fr; min-height: 8; }
#plugin-actions { height: auto; layout: horizontal; padding: 1 0 0 0; }
#plugin-actions Button { margin: 0 1 0 0; }
#plugin-detail {
    height: auto; max-height: 14;
    border: round #404040; padding: 1 2; margin: 1 0 0 0;
    background: #2d2d2d;
}

/* ── Config ──────────────────────────────── */
#config-scroll { height: 1fr; }
#config-selector { height: auto; layout: horizontal; padding: 0 0 1 0; }
#config-editor { height: 1fr; }
#config-actions { height: auto; layout: horizontal; padding: 1 0 0 0; }
#config-actions Button { margin: 0 1 0 0; }
#config-status { padding: 0 1; }

/* ── Logs ────────────────────────────────── */
#logs-scroll { height: 1fr; }
#log-filters { height: auto; layout: horizontal; padding: 0 0 1 0; }
#log-search { width: 1fr; }
#log-table { height: 1fr; border: round #404040; background: #252525; }
#log-record-count { height: auto; color: #808080; padding: 0 1; }
#log-detail {
    height: auto; max-height: 10;
    border: round #404040; padding: 1 2; margin: 1 0 0 0;
    background: #2d2d2d; color: #d4d4d4;
}

/* ── PluginCore stats ───────────────────── */
#plugincore-section { height: auto; }
.pc-stat-row { height: auto; layout: horizontal; padding: 0 0 1 0; }
#top-plugins-table { height: auto; max-height: 10; border: round #404040; background: #2d2d2d; }

/* ── Settings ────────────────────────────── */
#settings-scroll { height: 1fr; }
.settings-group {
    border: round #404040;
    padding: 1 2;
    margin: 0 0 1 0;
    height: auto;
    background: #2d2d2d;
}
.settings-group-title { color: #c7a06e; text-style: bold; padding: 0 0 1 0; }
.setting-row { height: auto; layout: horizontal; padding: 0 0 1 0; }
.setting-label { color: #808080; width: 25; }
.setting-value { color: #d4d4d4; width: 1fr; }

/* ── Plugin view ─────────────────────────── */
.plugin-view-container { height: 1fr; padding: 1 2; }
.plugin-view-container > Horizontal { height: auto; padding: 0 0 1 0; }
.plugin-view-container > Horizontal > Button { margin: 0 1 0 0; }

.ep-meta { color: #808080; }
.ep-arg-table { height: auto; max-height: 8; }


/* ── General ─────────────────────────────── */
Button { background: #3c3c3c; color: #d4d4d4; }
Button:hover { background: #505050; }
Button.-success { background: #2d3b2d; color: #73c991; }
Button.-warning { background: #3b3325; color: #cca75a; }
Button.-error { background: #3b2525; color: #d16969; }
Button.-primary { background: #2d3340; color: #7dade0; }

Input { background: #2d2d2d; border: round #404040; color: #d4d4d4; }
Select { background: #2d2d2d; }
TextArea { background: #2d2d2d; }
DataTable { background: #2d2d2d; }
DataTable > .datatable--header { background: #1e1e1e; color: #c7a06e; text-style: bold; }
DataTable > .datatable--cursor { background: #3c3c3c; }

Checkbox { background: transparent; }
Collapsible { background: transparent; padding: 0; }
CollapsibleTitle { background: #2d2d2d; color: #9bb5a0; padding: 0 1; }

RichLog { background: #1e1e1e; }
"""


class QuitConfirmScreen(ModalScreen[bool]):
    """Modal confirmation dialog for quitting the dashboard."""

    DEFAULT_CSS = """
    QuitConfirmScreen {
        align: center middle;
    }
    #quit-dialog {
        width: 40;
        height: auto;
        border: round #404040;
        background: #2d2d2d;
        padding: 1 2;
    }
    #quit-dialog Static {
        width: 1fr;
        content-align: center middle;
        margin: 0 0 1 0;
    }
    #quit-dialog Horizontal {
        width: 1fr;
        height: auto;
        align: center middle;
    }
    #quit-dialog Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-dialog"):
            yield Static("Quit the dashboard?")
            with Horizontal():
                yield Button("Yes", id="quit-yes", variant="error")
                yield Button("No", id="quit-no", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "quit-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class DashboardApp(App):
    """AIO Dashboard TUI."""

    TITLE = "AIO Dashboard"
    SUB_TITLE = "PluginCore Management"
    CSS = APP_CSS

    BINDINGS = [
        Binding("q", "request_quit", "Quit", show=True),
        Binding("ctrl+q", "force_quit", "Force Quit", show=False),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("1", "tab_home", "Home"),
        Binding("2", "tab_plugins", "Plugins"),
        Binding("3", "tab_config", "Config"),
        Binding("4", "tab_logs", "Logs"),
        Binding("5", "tab_settings", "Settings"),
    ]

    def __init__(
        self,
        plugin_core,
        plugin_instance,
        log_handler: TUILogHandler,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.plugin_core = plugin_core
        self.plugin_instance = plugin_instance
        self.log_handler = log_handler
        # Main event loop reference — used to dispatch PluginCore calls
        # from the TUI thread back to the correct event loop.
        self._main_loop: asyncio.AbstractEventLoop = plugin_instance.event_loop
        self._start_time = time.time()
        self._tracker = RequestTracker()

        # Process handle for per-process stats
        self._process = psutil.Process() if HAS_PSUTIL else None

        # Graph data
        self._graph_toggles = {"cpu": True, "memory": True}
        self._cpu_data: list[float] = []
        self._mem_data: list[float] = []

        # Config editor state
        self._current_config_file: Optional[str] = None
        self._config_files: Dict[str, str] = {}
        self._config_clean_hash: Optional[str] = None  # hash of content at load/save

        # Refresh intervals (configurable via Settings)
        self._stats_interval = DEFAULT_STATS_INTERVAL
        self._plugin_interval = DEFAULT_PLUGIN_INTERVAL
        self._request_interval = DEFAULT_REQUEST_INTERVAL

        # Timer references for restart on settings change
        self._stats_timer = None
        self._plugin_timer = None
        self._request_timer = None
        self._log_timer = None

        # ID registry for dynamic widgets
        self._id_counter = 0
        self._id_registry: Dict[str, Dict[str, str]] = {}
        self._plugin_tab_map: Dict[str, str] = {}

        # Plugin search filter
        self._plugin_filter: str = ""

    # ─── Cross-loop dispatch ────────────────────────────────────────

    async def _run_on_main(self, coro, timeout: float = 30.0):
        """Schedule a coroutine on the main event loop and await its result.

        PluginCore's async methods (execute, _enable_plugin, etc.) use
        asyncio primitives bound to the main loop. Awaiting them directly
        from the TUI thread's loop would use the wrong event loop, breaking
        locks, tasks, and futures. This helper dispatches correctly.

        Returns None (instead of crashing) if the main loop is
        closed/stopped — this happens during shutdown and must not
        take down the TUI.
        """
        if self._main_loop.is_closed():
            coro.close()  # prevent "coroutine never awaited" warning
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._main_loop)
        # Wrap the concurrent.futures.Future so we can await it on Textual's loop.
        # Timeout prevents a hung PluginCore call from freezing the entire TUI.
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)

    # ─── ID Registry ─────────────────────────────────────────────────

    def _make_id(self, prefix: str, plugin_name: str, endpoint: str, widget_type: str) -> str:
        self._id_counter += 1
        wid = f"{prefix}-{self._id_counter}"
        self._id_registry[wid] = {
            "plugin": plugin_name,
            "endpoint": endpoint,
            "type": widget_type,
        }
        return wid

    def _lookup_id(self, wid: str) -> Optional[Dict[str, str]]:
        return self._id_registry.get(wid)

    def _cleanup_registry_for_plugin(self, plugin_name: str) -> None:
        to_remove = [k for k, v in self._id_registry.items() if v.get("plugin") == plugin_name]
        for wid in to_remove:
            del self._id_registry[wid]

    @staticmethod
    def _sanitize_id(name: str) -> str:
        sanitized = re.sub(r'[^a-zA-Z0-9_-]', '-', name)
        if sanitized and sanitized[0].isdigit():
            sanitized = f"p-{sanitized}"
        sanitized = sanitized or "unknown"
        name_hash = hashlib.md5(name.encode()).hexdigest()[:6]
        return f"{sanitized}-{name_hash}"

    # ─── Compose ─────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="main-tabs"):

            # ── 1. Home ──────────────────────────────────────────
            with TabPane("Home", id="tab-home"):
                with VerticalScroll(id="home-scroll"):
                    # System stats row
                    yield Static("System", classes="section-header")
                    with Horizontal(classes="stat-row"):
                        with Horizontal(classes="stat-card"):
                            yield Static("Host ", classes="stat-key")
                            yield Static("...", id="stat-hostname", classes="stat-val")
                        with Horizontal(classes="stat-card"):
                            yield Static("Up ", classes="stat-key")
                            yield Static("...", id="stat-uptime", classes="stat-val")
                        with Horizontal(classes="stat-card"):
                            yield Static("CPU ", classes="stat-key")
                            yield Static("...", id="stat-cpu", classes="stat-val")
                        with Horizontal(classes="stat-card"):
                            yield Static("Mem ", classes="stat-key")
                            yield Static("...", id="stat-memory", classes="stat-val")
                        with Horizontal(classes="stat-card"):
                            yield Static("Net ", classes="stat-key")
                            yield Static("...", id="stat-networking", classes="stat-val")

                    # Plugin health row
                    yield Static("Plugins", classes="section-header")
                    with Horizontal(classes="stat-row"):
                        with Horizontal(classes="stat-card"):
                            yield Static("Total ", classes="stat-key")
                            yield Static("...", id="stat-plugins-total", classes="stat-val")
                        with Horizontal(classes="stat-card"):
                            yield Static("Enabled ", classes="stat-key")
                            yield Static("...", id="stat-plugins-enabled", classes="stat-val-good")
                        with Horizontal(classes="stat-card"):
                            yield Static("Disabled ", classes="stat-key")
                            yield Static("...", id="stat-plugins-disabled", classes="stat-val-warn")

                    # Request stats row
                    yield Static("Requests", classes="section-header")
                    with Horizontal(classes="stat-row"):
                        with Horizontal(classes="stat-card"):
                            yield Static("Active ", classes="stat-key")
                            yield Static("0", id="stat-req-active", classes="stat-val")
                        with Horizontal(classes="stat-card"):
                            yield Static("Total ", classes="stat-key")
                            yield Static("0", id="stat-req-total", classes="stat-val")
                        with Horizontal(classes="stat-card"):
                            yield Static("Errors ", classes="stat-key")
                            yield Static("0", id="stat-req-errors", classes="stat-val")
                        with Horizontal(classes="stat-card"):
                            yield Static("Avg ms ", classes="stat-key")
                            yield Static("0", id="stat-req-latency", classes="stat-val")
                        with Horizontal(classes="stat-card"):
                            yield Static("Req/min ", classes="stat-key")
                            yield Static("0", id="stat-req-rpm", classes="stat-val")

                    # Active requests table
                    yield DataTable(id="request-table", cursor_type="none")
                    yield Static("[dim]No active requests[/dim]", id="request-empty", markup=True)

                    # Network nodes (conditional)
                    with Vertical(id="network-section"):
                        yield Static("Network Nodes", classes="section-header")
                        yield DataTable(id="network-table", cursor_type="none")
                        yield Static("[dim]No network nodes[/dim]", id="network-empty", markup=True)

                    # Graphs
                    yield Static("Graphs", classes="section-header")
                    with Horizontal(id="graph-toggles"):
                        yield Checkbox("CPU", value=True, id="toggle-cpu")
                        yield Checkbox("Memory", value=True, id="toggle-memory")
                    with Horizontal(id="graphs-section"):
                        with Vertical(id="graph-cpu-box", classes="graph-box"):
                            with Horizontal(classes="graph-header"):
                                yield Static("CPU %", classes="graph-title")
                                yield Static("", id="graph-cpu-val", classes="graph-value")
                            yield Sparkline([], id="graph-cpu")
                        with Vertical(id="graph-mem-box", classes="graph-box"):
                            with Horizontal(classes="graph-header"):
                                yield Static("Memory %", classes="graph-title")
                                yield Static("", id="graph-mem-val", classes="graph-value")
                            yield Sparkline([], id="graph-mem")

                    # PluginCore internals
                    with Vertical(id="plugincore-section"):
                        yield Static("PluginCore", classes="section-header")
                        with Horizontal(classes="stat-row"):
                            with Horizontal(classes="stat-card"):
                                yield Static("Tasks ", classes="stat-key")
                                yield Static("0", id="stat-pc-tasks", classes="stat-val")
                            with Horizontal(classes="stat-card"):
                                yield Static("Threads ", classes="stat-key")
                                yield Static("0/0", id="stat-pc-threads", classes="stat-val")
                            with Horizontal(classes="stat-card"):
                                yield Static("RPM ", classes="stat-key")
                                yield Static("0", id="stat-pc-rpm", classes="stat-val")
                        yield Static("Top Plugins by Requests", classes="section-header")
                        yield DataTable(id="top-plugins-table", cursor_type="none")

            # ── 2. Plugins ───────────────────────────────────────
            with TabPane("Plugins", id="tab-plugins"):
                with VerticalScroll(id="plugins-scroll"):
                    yield Input(placeholder="Search plugins...", id="plugin-search")
                    yield DataTable(id="plugin-table", cursor_type="row")
                    with Horizontal(id="plugin-actions"):
                        yield Button("Enable", id="btn-enable", variant="success")
                        yield Button("Disable", id="btn-disable", variant="warning")
                        yield Button("Reload", id="btn-reload", variant="primary")
                        yield Button("Remove", id="btn-remove", variant="error")
                        yield Button("Open Tab", id="btn-open-tab")
                        yield Button("Refresh", id="btn-refresh-plugins")
                    yield Static("Select a plugin to see details", id="plugin-detail", markup=True)

            # ── 3. Config ────────────────────────────────────────
            with TabPane("Config", id="tab-config"):
                with Vertical(id="config-scroll"):
                    with Horizontal(id="config-selector"):
                        yield Select([], id="config-select", prompt="Select config file...")
                        yield Button("Load", id="btn-config-load", variant="primary")
                    yield TextArea("", id="config-editor", language="yaml")
                    with Horizontal(id="config-actions"):
                        yield Button("Save", id="btn-config-save", variant="success")
                        yield Button("Revert", id="btn-config-revert", variant="warning")
                        yield Button("Reload Main Config", id="btn-config-reload", variant="primary")
                        yield Static("", id="config-status", markup=True)

            # ── 4. Logs ──────────────────────────────────────────
            with TabPane("Logs", id="tab-logs"):
                with Vertical(id="logs-scroll"):
                    with Horizontal(id="log-filters"):
                        yield Select(
                            [("All Levels", "ALL"), ("DEBUG", "DEBUG"),
                             ("INFO", "INFO"), ("WARNING", "WARNING"),
                             ("ERROR", "ERROR")],
                            id="log-level-filter", value="ALL",
                            prompt="Filter level...",
                        )
                        yield Input(placeholder="Search logs...", id="log-search")
                        yield Checkbox("Auto-scroll", value=True, id="log-autoscroll")
                        yield Checkbox("Pause", value=False, id="log-pause")
                    yield DataTable(id="log-table", cursor_type="row")
                    yield Static("0/0 records", id="log-record-count", markup=True)
                    yield Static("", id="log-detail", markup=True)

            # ── 5. Settings ──────────────────────────────────────
            with TabPane("Settings", id="tab-settings"):
                with VerticalScroll(id="settings-scroll"):
                    # TUI settings
                    with Vertical(classes="settings-group"):
                        yield Static("TUI Settings", classes="settings-group-title")
                        with Horizontal(classes="setting-row"):
                            yield Static("Stats refresh (s):", classes="setting-label")
                            yield Input(str(DEFAULT_STATS_INTERVAL), id="setting-stats-interval", type="number")
                        with Horizontal(classes="setting-row"):
                            yield Static("Plugin refresh (s):", classes="setting-label")
                            yield Input(str(DEFAULT_PLUGIN_INTERVAL), id="setting-plugin-interval", type="number")
                        with Horizontal(classes="setting-row"):
                            yield Static("Request poll (s):", classes="setting-label")
                            yield Input(str(DEFAULT_REQUEST_INTERVAL), id="setting-request-interval", type="number")
                        yield Button("Apply", id="btn-apply-settings", variant="primary")
                        yield Static("", id="settings-status", markup=True)

                    # PluginCore info
                    with Vertical(classes="settings-group"):
                        yield Static("PluginCore", classes="settings-group-title")
                        with Horizontal(classes="setting-row"):
                            yield Static("Hostname:", classes="setting-label")
                            yield Static("...", id="info-hostname", classes="setting-value")
                        with Horizontal(classes="setting-row"):
                            yield Static("Plugin package:", classes="setting-label")
                            yield Static("...", id="info-plugin-package", classes="setting-value")
                        with Horizontal(classes="setting-row"):
                            yield Static("Console log level:", classes="setting-label")
                            yield Select(
                                [("DEBUG", "DEBUG"), ("INFO", "INFO"),
                                 ("WARNING", "WARNING"), ("ERROR", "ERROR")],
                                id="setting-log-level", value="DEBUG",
                                prompt="Log level...",
                            )

                    # Networking info
                    with Vertical(classes="settings-group"):
                        yield Static("Networking", classes="settings-group-title")
                        with Horizontal(classes="setting-row"):
                            yield Static("Enabled:", classes="setting-label")
                            yield Static("...", id="info-net-enabled", classes="setting-value")
                        with Horizontal(classes="setting-row"):
                            yield Static("Port:", classes="setting-label")
                            yield Static("...", id="info-net-port", classes="setting-value")
                        with Horizontal(classes="setting-row"):
                            yield Static("Discoverable:", classes="setting-label")
                            yield Static("...", id="info-net-discoverable", classes="setting-value")
                        with Horizontal(classes="setting-row"):
                            yield Static("Node IPs:", classes="setting-label")
                            yield Static("...", id="info-net-nodes", classes="setting-value")

        yield Footer()

    # ─── Lifecycle ───────────────────────────────────────────────────

    def on_mount(self) -> None:
        # Attach log handler to DataTable
        try:
            log_table = self.query_one("#log-table", DataTable)
            log_detail = self.query_one("#log-detail", Static)
            log_table.add_columns("Time", "Level", "Source", "Message")
            self.log_handler.attach(log_table, detail_widget=log_detail, app=self)
        except NoMatches:
            pass

        # Setup tables
        try:
            t = self.query_one("#plugin-table", DataTable)
            t.add_columns("Name", "Status", "Version", "Remote", "Endpoints", "Description")
        except NoMatches:
            pass
        try:
            rt = self.query_one("#request-table", DataTable)
            rt.add_columns("ID", "Plugin", "Method", "Author", "Elapsed", "Status")
        except NoMatches:
            pass
        try:
            nt = self.query_one("#network-table", DataTable)
            nt.add_columns("Hostname", "IP", "Status")
        except NoMatches:
            pass
        try:
            tpt = self.query_one("#top-plugins-table", DataTable)
            tpt.add_columns("Plugin", "Requests", "Errors", "Avg ms")
        except NoMatches:
            pass

        # Hide network section if networking disabled
        if not getattr(self.plugin_core, "networking_enabled", False):
            try:
                self.query_one("#network-section").display = False
            except NoMatches:
                pass

        # Populate settings info
        self._populate_settings_info()

        # Build config file list
        self._build_config_file_list()

        # Initial data loads (workers — non-blocking)
        self._refresh_stats_worker()
        self._refresh_plugin_table_worker()
        self._refresh_requests_worker()

        # Start periodic timers
        self._start_timers()

    def _start_timers(self) -> None:
        if self._stats_timer:
            self._stats_timer.stop()
        if self._plugin_timer:
            self._plugin_timer.stop()
        if self._request_timer:
            self._request_timer.stop()
        if self._log_timer:
            self._log_timer.stop()
        self._stats_timer = self.set_interval(self._stats_interval, self._refresh_stats_worker)
        self._plugin_timer = self.set_interval(self._plugin_interval, self._periodic_plugin_refresh)
        self._request_timer = self.set_interval(self._request_interval, self._refresh_requests_worker)
        self._log_timer = self.set_interval(0.5, self._refresh_log_table)

    def _periodic_plugin_refresh(self) -> None:
        try:
            self._refresh_plugin_table_worker()
            self._cleanup_stale_plugin_tabs()
        except Exception:
            pass

    def _print(self, text: str, stderr: bool = False) -> None:
        """Override Textual's print capture to route into our log handler.

        By default Textual displays captured stdout/stderr in a console
        area at the top of the screen. We redirect it into the Logs tab
        instead so third-party library output doesn't corrupt the TUI.
        """
        if text.strip():
            stream = "stderr" if stderr else "stdout"
            record = logging.LogRecord(
                name=f"captured.{stream}",
                level=logging.WARNING if stderr else logging.INFO,
                pathname="<captured>",
                lineno=0,
                msg=text.rstrip(),
                args=(),
                exc_info=None,
            )
            self.log_handler.emit(record)

    def _refresh_log_table(self) -> None:
        """Batch-refresh the log DataTable from the handler's store."""
        try:
            counts = self.log_handler.refresh_table()
        except Exception:
            return
        if counts is not None:
            filtered, total = counts
            try:
                label = f"{filtered}/{total} records"
                if filtered < total:
                    label += " (filtered)"
                self.query_one("#log-record-count", Static).update(label)
            except NoMatches:
                pass

    def _show_log_detail(self, row_key: str) -> None:
        """Show full log record text in the detail panel when a row is selected."""
        try:
            detail = self.query_one("#log-detail", Static)
        except NoMatches:
            return
        try:
            # .store returns a thread-safe snapshot (list copy)
            for rec in self.log_handler.store:
                if str(rec.seq) == row_key:
                    # Use Text objects to avoid MarkupError on log content
                    # containing bracket patterns (e.g. system prompts, JSON)
                    header = Text(f"{rec.level} {rec.timestamp} {rec.source}", style="bold")
                    body = Text(f"\n{rec.full_text}")
                    detail.update(header + body)
                    return
            detail.update("")
        except Exception:
            pass

    async def _shutdown(self) -> None:
        self.log_handler.detach()
        await super()._shutdown()

    # ─── Stats refresh ───────────────────────────────────────────────

    @work(thread=False, exclusive=True, group="stats")
    async def _refresh_stats_worker(self) -> None:
        # Early guard — if key widget missing, DOM not ready / being torn down
        try:
            hostname_w = self.query_one("#stat-hostname", Static)
        except NoMatches:
            return

        try:
            hostname_w.update(getattr(self.plugin_core, "hostname", "?") or "?")

            # Uptime
            secs = int(time.time() - self._start_time)
            h, r = divmod(secs, 3600)
            m, s = divmod(r, 60)
            self.query_one("#stat-uptime", Static).update(f"{h}h{m:02d}m")

            # Plugins — snapshot to avoid RuntimeError from cross-thread dict mutation
            plugins = list(self.plugin_core.plugins.values())
            total = len(plugins)
            enabled = sum(1 for p in plugins if p.enabled)
            disabled = total - enabled
            self.query_one("#stat-plugins-total", Static).update(str(total))
            self.query_one("#stat-plugins-enabled", Static).update(str(enabled))
            self.query_one("#stat-plugins-disabled", Static).update(str(disabled))

            # Networking
            net = "ON" if getattr(self.plugin_core, "networking_enabled", False) else "OFF"
            self.query_one("#stat-networking", Static).update(net)

            # CPU & Memory (process / system)
            if HAS_PSUTIL:
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory()
                proc_cpu = self._process.cpu_percent() if self._process else 0
                try:
                    proc_mem = self._process.memory_info().rss / (1024**3) if self._process else 0
                except Exception:
                    proc_mem = 0
                self.query_one("#stat-cpu", Static).update(
                    f"{proc_cpu:.0f}% / {cpu:.0f}%"
                )
                self.query_one("#stat-memory", Static).update(
                    f"{proc_mem:.1f}G / {mem.used / (1024**3):.1f}G"
                )
                # Update graph value labels
                try:
                    self.query_one("#graph-cpu-val", Static).update(f"{cpu:.1f}%")
                except NoMatches:
                    pass
                try:
                    self.query_one("#graph-mem-val", Static).update(f"{mem.percent:.1f}%")
                except NoMatches:
                    pass
                # Graphs
                self._cpu_data.append(cpu)
                self._mem_data.append(mem.percent)
                if len(self._cpu_data) > MAX_GRAPH_POINTS:
                    self._cpu_data = self._cpu_data[-MAX_GRAPH_POINTS:]
                if len(self._mem_data) > MAX_GRAPH_POINTS:
                    self._mem_data = self._mem_data[-MAX_GRAPH_POINTS:]
                if self._graph_toggles.get("cpu"):
                    try:
                        self.query_one("#graph-cpu", Sparkline).data = list(self._cpu_data)
                    except NoMatches:
                        pass
                if self._graph_toggles.get("memory"):
                    try:
                        self.query_one("#graph-mem", Sparkline).data = list(self._mem_data)
                    except NoMatches:
                        pass
            else:
                self.query_one("#stat-cpu", Static).update("n/a")
                self.query_one("#stat-memory", Static).update("n/a")

            # PluginCore stats
            # Active tasks — snapshot to avoid cross-thread mutation
            task_list = getattr(self.plugin_core, "task_list", [])
            try:
                task_count = len(list(task_list)) if task_list else 0
            except RuntimeError:
                task_count = 0
            self.query_one("#stat-pc-tasks", Static).update(str(task_count))

            # Thread pool — _threads is an internal set, snapshot defensively
            executor = getattr(self.plugin_core, "_plugin_executor", None)
            if executor:
                try:
                    threads = getattr(executor, "_threads", set())
                    thread_count = len(threads)
                except (RuntimeError, TypeError):
                    thread_count = 0
                max_w = getattr(executor, "_max_workers", 0)
                self.query_one("#stat-pc-threads", Static).update(
                    f"{thread_count}/{max_w}"
                )

            # RPM from tracker
            self.query_one("#stat-pc-rpm", Static).update(
                f"{self._tracker.requests_per_minute:.1f}"
            )

            # Top 5 plugins by request count — snapshot dict to avoid mutation
            tpt = self.query_one("#top-plugins-table", DataTable)
            tpt.clear()
            sorted_plugins = sorted(
                list(self._tracker.per_plugin.items()),
                key=lambda x: x[1].total, reverse=True,
            )[:5]
            for pname, pstats in sorted_plugins:
                tpt.add_row(
                    pname,
                    str(pstats.total),
                    str(pstats.errors),
                    f"{pstats.avg_latency * 1000:.0f}",
                )
        except (NoMatches, Exception):
            pass

    # ─── Request tracking ────────────────────────────────────────────

    @work(thread=False, exclusive=True, group="requests")
    async def _refresh_requests_worker(self) -> None:
        # Snapshot the dict — PluginCore mutates it from the main thread.
        # dict() is GIL-safe in CPython, but wrap for defensive safety.
        try:
            requests_dict = dict(getattr(self.plugin_core, "requests", {}))
        except RuntimeError:
            return
        try:
            self._tracker.poll(requests_dict)
        except Exception:
            return

        # Update stat cards
        try:
            self.query_one("#stat-req-active", Static).update(str(len(self._tracker.active)))
            self.query_one("#stat-req-total", Static).update(str(self._tracker.total_requests))
            errs = self._tracker.total_errors
            err_w = self.query_one("#stat-req-errors", Static)
            err_w.update(str(errs))
            # Swap class for color
            err_w.remove_class("stat-val", "stat-val-good", "stat-val-bad")
            err_w.add_class("stat-val-bad" if errs > 0 else "stat-val")
            avg_ms = self._tracker.avg_latency * 1000
            self.query_one("#stat-req-latency", Static).update(f"{avg_ms:.0f}")
            self.query_one("#stat-req-rpm", Static).update(f"{self._tracker.requests_per_minute:.1f}")
        except (NoMatches, Exception):
            pass

        # Update active requests table
        try:
            table = self.query_one("#request-table", DataTable)
            table.clear()
            has_active = bool(self._tracker.active)
            for req in self._tracker.active[:20]:  # cap display
                elapsed_str = f"{req.elapsed:.1f}s"
                if req.has_error:
                    status = Text("ERROR", style="red")
                elif req.has_timeout:
                    status = Text("TIMEOUT", style="yellow")
                elif req.is_finished:
                    status = Text("FINISHED", style="cyan")
                elif req.elapsed > 5:
                    status = Text("SLOW", style="yellow")
                else:
                    status = Text("ACTIVE", style="green")
                table.add_row(req.request_id, req.plugin, req.method, req.author, elapsed_str, status)
            try:
                self.query_one("#request-empty").display = not has_active
            except NoMatches:
                pass
        except (NoMatches, Exception):
            pass

    # ─── Plugin table ────────────────────────────────────────────────

    @work(thread=False, exclusive=True, group="plugins")
    async def _refresh_plugin_table_worker(self) -> None:
        try:
            table = self.query_one("#plugin-table", DataTable)
        except NoMatches:
            return

        selected_key = None
        if table.row_count > 0 and table.cursor_row is not None:
            try:
                selected_key = table.get_row_at(table.cursor_row)[0]
            except Exception:
                pass

        table.clear()

        # Read without lock — dict snapshot is safe for display
        try:
            plugins_snapshot = [
                (
                    name,
                    p.enabled,
                    getattr(p, "version", "?"),
                    getattr(p, "remote", False),
                    len(getattr(p, "endpoints", [])),
                    getattr(p, "description", ""),
                )
                for name, p in list(self.plugin_core.plugins.items())
            ]
        except Exception:
            return

        filt = self._plugin_filter.lower()
        for name, enabled, version, remote, ep_count, desc in plugins_snapshot:
            if filt and filt not in name.lower() and filt not in desc.lower():
                continue
            status = Text("ON", style="green") if enabled else Text("OFF", style="red")
            remote_str = Text("R", style="cyan") if remote else Text("L", style="dim")
            desc_short = (desc[:40] + "...") if len(desc) > 43 else desc
            table.add_row(name, status, version, remote_str, str(ep_count), desc_short, key=name)

        if selected_key:
            for idx in range(table.row_count):
                try:
                    if table.get_row_at(idx)[0] == selected_key:
                        table.move_cursor(row=idx)
                        break
                except Exception:
                    pass

    # ─── Config ──────────────────────────────────────────────────────

    def _build_config_file_list(self) -> None:
        try:
            self._config_files = {}
            main_config = os.path.abspath(self.plugin_core.config_path)
            self._config_files["config.yml (main)"] = main_config

            plugin_package = getattr(self.plugin_core, "plugin_package", "plugins")
            for entry in self.plugin_core.yaml_config.get("plugins", []):
                name = entry.get("name", "")
                if not name:
                    continue
                path = entry.get("path") or os.path.join(plugin_package, name)
                cfg = os.path.join(os.path.abspath(path), "plugin_config.yml")
                if os.path.exists(cfg):
                    self._config_files[f"{name}/plugin_config.yml"] = cfg

            self.query_one("#config-select", Select).set_options(
                [(l, l) for l in sorted(self._config_files.keys())]
            )
        except (NoMatches, Exception):
            pass

    @work(thread=False, exclusive=True, group="config")
    async def _load_config_file(self, label: str) -> None:
        path = self._config_files.get(label)
        if not path or not os.path.exists(path):
            self._set_status(f"File not found: {path}", error=True)
            return
        # Warn if current editor has unsaved changes
        had_unsaved = self._config_is_dirty()
        try:
            content = Path(path).read_text(encoding="utf-8")
            self.query_one("#config-editor", TextArea).load_text(content)
            self._current_config_file = path
            self._config_clean_hash = hashlib.md5(content.encode()).hexdigest()
            if had_unsaved:
                self._set_status(f"Loaded: {os.path.basename(path)} (unsaved changes discarded)", error=True)
            else:
                self._set_status(f"Loaded: {os.path.basename(path)}")
        except Exception as e:
            self._set_status(f"Error: {e}", error=True)

    def load_plugin_config(self, plugin_name: str) -> None:
        """Switch to Config tab and load a plugin's config file."""
        label = f"{plugin_name}/plugin_config.yml"
        if label in self._config_files:
            try:
                self.query_one("#main-tabs", TabbedContent).active = "tab-config"
                self.query_one("#config-select", Select).value = label
            except (NoMatches, Exception):
                pass
            self._load_config_file(label)

    @work(thread=False, exclusive=True, group="config")
    async def _save_config_file(self) -> None:
        if not self._current_config_file:
            self._set_status("No file loaded", error=True)
            return
        try:
            content = self.query_one("#config-editor", TextArea).text
            yaml.safe_load(content)
            config_path = Path(self._current_config_file)
            if config_path.exists():
                config_path.with_suffix(".yml.bak").write_text(
                    config_path.read_text(encoding="utf-8"), encoding="utf-8"
                )
            config_path.write_text(content, encoding="utf-8")
            self._config_clean_hash = hashlib.md5(content.encode()).hexdigest()
            main = os.path.abspath(self.plugin_core.config_path)
            if self._current_config_file == main:
                self._set_status("Saved main config. Restart to apply.")
            else:
                self._set_status(f"Saved: {os.path.basename(self._current_config_file)}")
        except yaml.YAMLError as e:
            self._set_status(f"Invalid YAML: {e}", error=True)
        except Exception as e:
            self._set_status(f"Error: {e}", error=True)

    def _config_is_dirty(self) -> bool:
        """Check if config editor content differs from last load/save."""
        if self._config_clean_hash is None:
            return False
        try:
            current = self.query_one("#config-editor", TextArea).text
            return hashlib.md5(current.encode()).hexdigest() != self._config_clean_hash
        except NoMatches:
            return False

    def _set_status(self, msg: str, error: bool = False) -> None:
        try:
            s = self.query_one("#config-status", Static)
            safe = escape(msg)
            s.update(f"[red]{safe}[/]" if error else f"[green]{safe}[/]")
        except NoMatches:
            pass

    # ─── Settings ────────────────────────────────────────────────────

    def _populate_settings_info(self) -> None:
        try:
            self.query_one("#info-hostname", Static).update(
                getattr(self.plugin_core, "hostname", "?") or "?"
            )
            self.query_one("#info-plugin-package", Static).update(
                getattr(self.plugin_core, "plugin_package", "?")
            )
            net_enabled = getattr(self.plugin_core, "networking_enabled", False)
            self.query_one("#info-net-enabled", Static).update("Yes" if net_enabled else "No")
            self.query_one("#info-net-port", Static).update(
                str(getattr(self.plugin_core, "networking_port", "?"))
            )
            auto = getattr(self.plugin_core, "networking_auto_discoverable", False)
            direct = getattr(self.plugin_core, "networking_direct_discoverable", False)
            self.query_one("#info-net-discoverable", Static).update(
                f"Auto: {'Y' if auto else 'N'} | Direct: {'Y' if direct else 'N'}"
            )
            net_cfg = self.plugin_core.yaml_config.get("networking", {})
            ips = net_cfg.get("node_ips", [])
            self.query_one("#info-net-nodes", Static).update(
                ", ".join(ips) if ips else "none"
            )
            # Set log level select to current
            log_level = self.plugin_core.yaml_config.get("general", {}).get(
                "console_log_level", "DEBUG"
            )
            try:
                self.query_one("#setting-log-level", Select).value = log_level.upper()
            except Exception:
                pass
        except NoMatches:
            pass

    # ─── Plugin view generation ──────────────────────────────────────

    def _build_plugin_tab_content(self, plugin_name: str, plugin=None) -> list:
        if plugin is None:
            plugin = self.plugin_core.plugins.get(plugin_name)
        if not plugin:
            return [Static(f"Plugin '{escape(plugin_name)}' not found.")]

        # Custom widget
        if hasattr(plugin, "get_tui_widget") and callable(plugin.get_tui_widget):
            try:
                from textual.widget import Widget as _Widget
                w = plugin.get_tui_widget()
                if w is not None and isinstance(w, _Widget):
                    return [w]
            except Exception as e:
                return [Static(f"Error: {escape(str(e))}")]

        # Menu dict
        if hasattr(plugin, "get_tui_menu") and callable(plugin.get_tui_menu):
            try:
                menu = plugin.get_tui_menu()
                if menu and isinstance(menu, dict):
                    return self._render_menu_dict(plugin_name, menu)
            except Exception as e:
                return [Static(f"Error: {escape(str(e))}")]

        # Auto-generate
        return self._auto_generate_plugin_view(plugin_name, plugin)

    def _auto_generate_plugin_view(self, plugin_name: str, plugin) -> list:
        widgets = []

        # Header with close + config buttons side by side
        close_id = self._make_id("close", plugin_name, "", "close-tab")
        config_id = self._make_id("cfg", plugin_name, "", "goto-config")
        btn_row = Horizontal(
            Button("Close Tab", id=close_id, variant="error"),
            Button("Open Config", id=config_id, variant="primary"),
        )
        widgets.append(btn_row)

        desc = getattr(plugin, "description", "")
        version = getattr(plugin, "version", "?")
        widgets.append(Static(
            f"[bold]{escape(plugin_name)}[/bold] v{escape(str(version))}"
            + (f"  [dim]{escape(desc)}[/dim]" if desc else ""),
            markup=True,
        ))
        widgets.append(Rule())

        endpoints = getattr(plugin, "endpoints", [])
        if not endpoints:
            widgets.append(Static("[dim]No endpoints defined.[/dim]", markup=True))
            return widgets

        for ep in endpoints:
            if not isinstance(ep, dict):
                continue
            access_name = ep.get("access_name", "unknown")
            internal_name = ep.get("internal_name", "")
            description = ep.get("description", "")
            remote = ep.get("remote", False)
            accessible = ep.get("accessible_by_other_plugins", False)
            arguments = ep.get("arguments") or []
            tags = ep.get("tags") or []

            # Build collapsible title
            flags = []
            if remote:
                flags.append("[cyan]R[/cyan]")
            if accessible:
                flags.append("[green]A[/green]")
            flag_str = " ".join(flags)
            title = f"{escape(access_name)}  {flag_str}" if flags else escape(access_name)

            # Content inside collapsible
            inner_widgets = []

            # Description as readable Static (not crammed into title)
            if description:
                inner_widgets.append(Static(
                    f"[dim]{escape(description)}[/dim]", markup=True
                ))

            # Metadata line
            meta_parts = [f"Internal: {escape(internal_name)}"]
            if tags:
                meta_parts.append(f"Tags: {', '.join(escape(t) for t in tags)}")
            inner_widgets.append(Static(
                "  ".join(meta_parts), classes="ep-meta"
            ))
            inner_widgets.append(Rule())

            # Argument table — only for non-accessible endpoints (reference only).
            # Accessible endpoints show form fields instead to avoid duplication.
            if arguments and not accessible:
                arg_table = DataTable(classes="ep-arg-table", cursor_type="none")
                arg_table.add_columns("Name", "Type", "Required", "Description")
                for arg in arguments:
                    if isinstance(arg, dict):
                        arg_name = arg.get("name", "?")
                        arg_required = arg.get("required")
                        if arg_required is None:
                            desc_lower = arg.get("description", "").lower()
                            arg_required = "optional" not in desc_lower and "omit" not in desc_lower
                        req_marker = Text("*", style="bold red") if arg_required else Text("-", style="dim")
                        arg_table.add_row(
                            arg_name,
                            arg.get("type", "?"),
                            req_marker,
                            arg.get("description", ""),
                        )
                inner_widgets.append(arg_table)

            # Call UI (only for accessible endpoints)
            if accessible:
                # Mode toggle: Form vs JSON
                mode_id = self._make_id("mode", plugin_name, access_name, "mode-toggle")
                inner_widgets.append(Static(""))  # spacer

                form_field_ids_for_mode = []
                if arguments:
                    inner_widgets.append(
                        Checkbox("JSON mode (raw input)", value=False, id=mode_id)
                    )
                    # Form fields — one input per argument
                    for arg in arguments:
                        if isinstance(arg, dict):
                            arg_name = arg.get("name", "param")
                            arg_type = arg.get("type", "")
                            arg_desc = arg.get("description", "")
                            arg_required = arg.get("required")
                            if arg_required is None:
                                dl = arg_desc.lower()
                                arg_required = "optional" not in dl and "omit" not in dl
                            field_id = self._make_id(
                                "field", plugin_name, f"{access_name}.{arg_name}", "form-field"
                            )
                            req_str = " (required)" if arg_required else " (optional)"
                            placeholder = f"{arg_name}{req_str}"
                            if arg_type:
                                placeholder += f"  [{arg_type}]"
                            form_field_ids_for_mode.append(field_id)
                            inner_widgets.append(Input(placeholder=placeholder, id=field_id))

                # JSON textarea (hidden by default if form fields exist)
                json_id = self._make_id("json", plugin_name, access_name, "json-input")
                json_input = Input(
                    placeholder='{"key": "value"} or leave empty',
                    id=json_id,
                )
                if arguments:
                    json_input.display = False
                    # Store form/json field IDs on the mode-toggle for fast lookup
                    self._id_registry[mode_id]["form_fields"] = form_field_ids_for_mode
                    self._id_registry[mode_id]["json_id"] = json_id
                inner_widgets.append(json_input)

                # Call button + result
                call_id = self._make_id("call", plugin_name, access_name, "call")
                result_id = self._make_id("result", plugin_name, access_name, "result")

                # Store cross-references
                form_field_ids = [
                    wid for wid, entry in self._id_registry.items()
                    if entry.get("type") == "form-field"
                    and entry.get("plugin") == plugin_name
                    and entry.get("endpoint", "").startswith(f"{access_name}.")
                ]
                self._id_registry[call_id]["json_id"] = json_id
                self._id_registry[call_id]["result_id"] = result_id
                self._id_registry[call_id]["form_fields"] = form_field_ids
                self._id_registry[call_id]["mode_id"] = mode_id if arguments else ""
                self._id_registry[call_id]["arg_names"] = [
                    a.get("name", "param") for a in arguments if isinstance(a, dict)
                ]

                inner_widgets.append(Button("Call", id=call_id, variant="primary"))
                inner_widgets.append(RichLog(id=result_id, max_lines=50, markup=True, wrap=True))

            # Wrap in Collapsible
            collapsible = Collapsible(*inner_widgets, title=title, collapsed=True)
            widgets.append(collapsible)

        return widgets

    def _render_menu_dict(self, plugin_name: str, menu: dict) -> list:
        widgets = []
        # Close button
        close_id = self._make_id("close", plugin_name, "", "close-tab")
        widgets.append(Button("Close Tab", id=close_id, variant="error"))

        label = menu.get("label", plugin_name)
        widgets.append(Static(f"[bold]{escape(label)}[/bold]", markup=True))

        for section in menu.get("sections", []):
            title = section.get("title", "")
            section_type = section.get("type", "info")
            items = section.get("items", [])

            if title:
                widgets.append(Rule())
                widgets.append(Static(f"[bold]{escape(title)}[/bold]", markup=True))

            if section_type == "actions":
                for item in items:
                    btn_id = self._make_id("menu-btn", plugin_name, item.get("action", ""), "menu-action")
                    widgets.append(Button(item.get("label", "Action"), id=btn_id, variant="primary"))
            elif section_type == "toggle_list":
                for item in items:
                    sw_id = self._make_id("menu-sw", plugin_name, item.get("action", ""), "menu-toggle")
                    widgets.append(Static(f"  {escape(item.get('label', ''))}"))
                    widgets.append(Switch(value=item.get("state", False), id=sw_id))
            elif section_type == "input":
                ep = section.get("action", "")
                inp_id = self._make_id("menu-inp", plugin_name, ep, "menu-input-field")
                btn_id = self._make_id("menu-btn", plugin_name, ep, "menu-input-submit")
                res_id = self._make_id("menu-res", plugin_name, ep, "menu-input-result")
                self._id_registry[btn_id]["input_id"] = inp_id
                self._id_registry[btn_id]["result_id"] = res_id
                widgets.append(Input(placeholder="Enter value...", id=inp_id))
                widgets.append(Button("Send", id=btn_id, variant="primary"))
                widgets.append(Static("", id=res_id))
            elif section_type == "info":
                for item in items:
                    if isinstance(item, str):
                        widgets.append(Static(escape(item)))
                    elif isinstance(item, dict):
                        widgets.append(Static(f"  {escape(str(item.get('label', '')))}: {escape(str(item.get('value', '')))}"))
        return widgets

    # ─── Dynamic plugin tabs ─────────────────────────────────────────

    async def open_plugin_tab(self, plugin_name: str) -> None:
        tab_id = f"tab-plugin-{self._sanitize_id(plugin_name)}"
        tabs = self.query_one("#main-tabs", TabbedContent)

        try:
            self.query_one(f"#{tab_id}", TabPane)
            tabs.active = tab_id
            return
        except NoMatches:
            pass

        self._cleanup_registry_for_plugin(plugin_name)

        plugin_snapshot = self.plugin_core.plugins.get(plugin_name)

        content = self._build_plugin_tab_content(plugin_name, plugin_snapshot)
        pane = TabPane(plugin_name, id=tab_id)
        await tabs.add_pane(pane)

        try:
            self.query_one(f"#{tab_id}", TabPane)
        except NoMatches:
            return

        scroll = VerticalScroll(classes="plugin-view-container")
        await pane.mount(scroll)
        for w in content:
            await scroll.mount(w)

        self._plugin_tab_map[tab_id] = plugin_name
        tabs.active = tab_id

    async def _close_plugin_tab(self, plugin_name: str) -> None:
        tab_id = f"tab-plugin-{self._sanitize_id(plugin_name)}"
        tabs = self.query_one("#main-tabs", TabbedContent)
        try:
            await tabs.remove_pane(tab_id)
        except Exception:
            pass
        self._cleanup_registry_for_plugin(plugin_name)
        self._plugin_tab_map.pop(tab_id, None)

    def _cleanup_stale_plugin_tabs(self) -> None:
        stale = []
        for tab_id, pname in list(self._plugin_tab_map.items()):
            try:
                self.query_one(f"#{tab_id}", TabPane)
            except NoMatches:
                self._cleanup_registry_for_plugin(pname)
                stale.append(tab_id)
        for tid in stale:
            self._plugin_tab_map.pop(tid, None)

    # ─── Event handlers ──────────────────────────────────────────────

    # Plugin management buttons
    @on(Button.Pressed, "#btn-enable")
    @on(Button.Pressed, "#btn-disable")
    @on(Button.Pressed, "#btn-reload")
    @on(Button.Pressed, "#btn-remove")
    @on(Button.Pressed, "#btn-open-tab")
    def _on_plugin_mgmt(self, event: Button.Pressed) -> None:
        actions = {
            "btn-enable": "enable", "btn-disable": "disable",
            "btn-reload": "reload", "btn-remove": "remove",
            "btn-open-tab": "open_tab",
        }
        action = actions.get(event.button.id or "")
        if action:
            self._plugin_action(action)

    @on(Button.Pressed, "#btn-refresh-plugins")
    def _on_refresh_plugins(self) -> None:
        try:
            self._refresh_plugin_table_worker()
            self._build_config_file_list()
        except Exception:
            pass

    @on(Button.Pressed, "#btn-config-load")
    def _on_config_load(self) -> None:
        try:
            sel = self.query_one("#config-select", Select)
            if sel.value and sel.value != Select.BLANK:
                self._load_config_file(str(sel.value))
        except NoMatches:
            pass

    @on(Button.Pressed, "#btn-config-save")
    def _on_config_save(self) -> None:
        self._save_config_file()

    @on(Button.Pressed, "#btn-config-revert")
    def _on_config_revert(self) -> None:
        try:
            if self._current_config_file:
                for label, path in self._config_files.items():
                    if path == self._current_config_file:
                        self._load_config_file(label)
                        break
        except Exception:
            pass

    @on(Button.Pressed, "#btn-config-reload")
    def _on_config_reload(self) -> None:
        self._reload_main_config()

    @work(thread=False)
    async def _reload_main_config(self) -> None:
        """Reload config.yml into PluginCore (re-applies general settings)."""
        try:
            self.plugin_core.load_config_yaml(self.plugin_core.config_path)
            self._set_status("Main config reloaded. General settings applied.")
            # Refresh settings display and config file list
            self._populate_settings_info()
            self._build_config_file_list()
        except Exception as e:
            self._set_status(f"Reload failed: {e}", error=True)

    @on(Input.Changed, "#log-search")
    def _on_log_search(self, event: Input.Changed) -> None:
        try:
            self.log_handler.search_filter = event.value
        except Exception:
            pass

    @on(Checkbox.Changed, "#log-autoscroll")
    def _on_log_autoscroll(self, event: Checkbox.Changed) -> None:
        try:
            self.log_handler._auto_scroll = event.value
        except Exception:
            pass

    @on(Checkbox.Changed, "#log-pause")
    def _on_log_pause(self, event: Checkbox.Changed) -> None:
        try:
            self.log_handler.paused = event.value
        except Exception:
            pass

    @on(Button.Pressed, "#btn-apply-settings")
    def _on_apply_settings(self) -> None:
        try:
            si = float(self.query_one("#setting-stats-interval", Input).value)
            pi = float(self.query_one("#setting-plugin-interval", Input).value)
            ri = float(self.query_one("#setting-request-interval", Input).value)
            self._stats_interval = max(0.5, si)
            self._plugin_interval = max(1.0, pi)
            self._request_interval = max(0.5, ri)
            self._start_timers()
            self._set_settings_status("Settings applied")
        except Exception as e:
            self._set_settings_status(f"Invalid value: {e}", error=True)

    def _set_settings_status(self, msg: str, error: bool = False) -> None:
        try:
            s = self.query_one("#settings-status", Static)
            safe = escape(msg)
            s.update(f"[red]{safe}[/]" if error else f"[green]{safe}[/]")
        except NoMatches:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Catch-all for registry-based buttons."""
        btn_id = event.button.id or ""
        entry = self._id_registry.get(btn_id)
        if not entry:
            return
        try:
            t = entry["type"]
            if t == "call":
                self._handle_endpoint_call(btn_id, entry)
            elif t == "menu-action":
                self._handle_menu_action(btn_id, entry)
            elif t == "menu-input-submit":
                self._handle_menu_input(btn_id, entry)
            elif t == "close-tab":
                self._close_plugin_tab_sync(entry["plugin"])
            elif t == "goto-config":
                self.load_plugin_config(entry["plugin"])
        except Exception:
            pass

    def _close_plugin_tab_sync(self, plugin_name: str) -> None:
        """Non-async wrapper to close a plugin tab from a button handler."""
        self._do_close_plugin_tab(plugin_name)

    @work(thread=False)
    async def _do_close_plugin_tab(self, plugin_name: str) -> None:
        await self._close_plugin_tab(plugin_name)

    @on(Select.Changed, "#log-level-filter")
    def _on_log_level_changed(self, event: Select.Changed) -> None:
        try:
            level_str = str(event.value)
            if level_str == "ALL":
                self.log_handler.display_level = logging.DEBUG
            else:
                self.log_handler.display_level = getattr(logging, level_str, logging.DEBUG)
        except Exception:
            pass

    @on(Select.Changed, "#setting-log-level")
    def _on_console_log_level(self, event: Select.Changed) -> None:
        try:
            if event.value and event.value != Select.BLANK:
                level = getattr(logging, str(event.value), logging.DEBUG)
                logging.getLogger().setLevel(level)
        except Exception:
            pass

    @on(Checkbox.Changed, "#toggle-cpu")
    def _on_toggle_cpu(self, event: Checkbox.Changed) -> None:
        self._graph_toggles["cpu"] = event.value
        try:
            self.query_one("#graph-cpu-box").display = event.value
        except NoMatches:
            pass

    @on(Checkbox.Changed, "#toggle-memory")
    def _on_toggle_memory(self, event: Checkbox.Changed) -> None:
        self._graph_toggles["memory"] = event.value
        try:
            self.query_one("#graph-mem-box").display = event.value
        except NoMatches:
            pass

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Handle form/JSON mode toggles in plugin tabs."""
        cb_id = event.checkbox.id or ""
        entry = self._id_registry.get(cb_id)
        if entry and entry.get("type") == "mode-toggle":
            json_mode = event.value
            # Direct lookup — field IDs stored on the mode-toggle entry
            for fid in entry.get("form_fields", []):
                try:
                    self.query_one(f"#{fid}", Input).display = not json_mode
                except NoMatches:
                    pass
            json_id = entry.get("json_id", "")
            if json_id:
                try:
                    self.query_one(f"#{json_id}", Input).display = json_mode
                except NoMatches:
                    pass

    @on(Input.Changed, "#plugin-search")
    def _on_plugin_search(self, event: Input.Changed) -> None:
        self._plugin_filter = event.value
        self._refresh_plugin_table_worker()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        sw_id = event.switch.id or ""
        entry = self._lookup_id(sw_id)
        if entry and entry.get("type") == "menu-toggle":
            self._execute_toggle(entry, event.value)

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Warn in config status when leaving config tab with unsaved edits."""
        # When switching away from config, show persistent dirty warning
        if self._config_is_dirty():
            self._set_status("Unsaved changes — switch back to save or revert", error=True)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        try:
            if event.data_table.id == "plugin-table" and event.row_key is not None:
                self._update_plugin_detail(str(event.row_key.value))
            elif event.data_table.id == "log-table" and event.row_key is not None:
                self._show_log_detail(str(event.row_key.value))
        except Exception:
            pass

    # ─── Workers ─────────────────────────────────────────────────────

    @work(thread=False)
    async def _plugin_action(self, action: str) -> None:
        try:
            table = self.query_one("#plugin-table", DataTable)
        except NoMatches:
            return
        if table.cursor_row is None or table.row_count == 0:
            return
        try:
            plugin_name = str(table.get_row_at(table.cursor_row)[0])
        except Exception:
            return
        if not plugin_name:
            return

        if action in ("remove", "disable") and plugin_name == self.plugin_instance.plugin_name:
            self._set_status(f"Cannot {action} Dashboard from its own TUI", error=True)
            return

        try:
            if action == "enable":
                await self._run_on_main(self.plugin_core._enable_plugin(plugin_name))
            elif action == "disable":
                await self._run_on_main(self.plugin_core._disable_plugin(plugin_name))
            elif action == "reload":
                await self._run_on_main(self.plugin_core._reload_plugin(plugin_name))
            elif action == "remove":
                await self._run_on_main(self.plugin_core.pop_plugin(plugin_name))
            elif action == "open_tab":
                await self.open_plugin_tab(plugin_name)
                return
        except Exception as e:
            self._set_status(f"Error: {e}", error=True)
        self._refresh_plugin_table_worker()

    @work(thread=False)
    async def _update_plugin_detail(self, plugin_name: str) -> None:
        try:
            info = await self._run_on_main(self.plugin_core.get_plugin_info(plugin_name))
        except Exception:
            return
        if not info:
            return
        try:
            detail = self.query_one("#plugin-detail", Static)
            endpoints = await self._run_on_main(self.plugin_core.get_plugin_endpoints(plugin_name))
            ep_count = len(endpoints) if endpoints else 0

            # Per-plugin request stats
            pstats = self._tracker.per_plugin.get(plugin_name)
            req_info = ""
            if pstats:
                req_info = (
                    f"\nRequests: {pstats.total} total, "
                    f"{pstats.errors} errors, "
                    f"avg {pstats.avg_latency*1000:.0f}ms"
                )

            # Active requests for this plugin
            active_for = [r for r in self._tracker.active if r.plugin == plugin_name]
            active_info = f"\nActive: {len(active_for)}" if active_for else ""

            detail.update(
                f"[bold]{escape(info['name'])}[/bold] v{escape(str(info['version']))}  "
                f"[dim]UUID: {escape(str(info['uuid']))}[/dim]\n"
                f"{escape(info.get('description', ''))}\n"
                f"Endpoints: {ep_count} | Remote: {'Yes' if info['remote'] else 'No'}"
                f"{req_info}{active_info}"
            )
        except (NoMatches, Exception):
            pass

    @work(thread=False)
    async def _execute_toggle(self, entry: Dict[str, str], state: bool) -> None:
        try:
            await self._run_on_main(self.plugin_instance.execute(
                entry["plugin"], entry["endpoint"], {"state": state}, host="any"
            ))
        except Exception as e:
            logging.getLogger().error(f"Toggle error: {e}")

    @work(thread=False)
    async def _handle_endpoint_call(self, btn_id: str, entry: Dict[str, str]) -> None:
        plugin_name = entry["plugin"]
        access_name = entry["endpoint"]
        result_id = entry.get("result_id", "")
        json_id = entry.get("json_id", "")
        mode_id = entry.get("mode_id", "")
        form_fields = entry.get("form_fields", [])
        arg_names = entry.get("arg_names", [])

        args = None

        # Determine mode: JSON or form
        json_mode = False
        if mode_id:
            try:
                json_mode = self.query_one(f"#{mode_id}", Checkbox).value
            except NoMatches:
                json_mode = True  # fallback to JSON if no toggle

        if json_mode or not form_fields:
            # JSON mode
            try:
                inp = self.query_one(f"#{json_id}", Input)
                if inp.value.strip():
                    args = json.loads(inp.value.strip())
            except NoMatches:
                pass
            except json.JSONDecodeError as e:
                try:
                    self.query_one(f"#{result_id}", RichLog).write(f"[red]Invalid JSON: {escape(str(e))}[/red]")
                except NoMatches:
                    pass
                return
        else:
            # Form mode — build args dict from individual fields
            args = {}
            for field_id, arg_name in zip(form_fields, arg_names):
                try:
                    val = self.query_one(f"#{field_id}", Input).value.strip()
                    if val:
                        # Try to parse as JSON value (for numbers, bools, etc.)
                        try:
                            args[arg_name] = json.loads(val)
                        except json.JSONDecodeError:
                            args[arg_name] = val  # keep as string
                except NoMatches:
                    pass
            if not args:
                args = None

        # Execute
        try:
            result = await self._run_on_main(self.plugin_instance.execute(
                plugin_name, access_name, args, host="any"
            ))
            try:
                rl = self.query_one(f"#{result_id}", RichLog)
                rl.clear()
                # Pretty-print result
                if isinstance(result, (dict, list)):
                    formatted = json.dumps(result, indent=2, default=str)
                    rl.write(Syntax(formatted, "json", theme="monokai"))
                else:
                    rl.write(f"[green]{escape(str(result))}[/green]")
            except NoMatches:
                pass
        except Exception as e:
            try:
                self.query_one(f"#{result_id}", RichLog).write(f"[red]Error: {escape(str(e))}[/red]")
            except NoMatches:
                pass

    @work(thread=False)
    async def _handle_menu_action(self, btn_id: str, entry: Dict[str, str]) -> None:
        try:
            await self._run_on_main(self.plugin_instance.execute(entry["plugin"], entry["endpoint"], host="any"))
        except Exception as e:
            logging.getLogger().error(f"Menu action error: {e}")

    @work(thread=False)
    async def _handle_menu_input(self, btn_id: str, entry: Dict[str, str]) -> None:
        inp_id = entry.get("input_id", "")
        res_id = entry.get("result_id", "")
        try:
            val = self.query_one(f"#{inp_id}", Input).value.strip()
            args = {"input": val} if val else None
            result = await self._run_on_main(self.plugin_instance.execute(
                entry["plugin"], entry["endpoint"], args, host="any"
            ))
            try:
                self.query_one(f"#{res_id}", Static).update(f"[green]{escape(str(result))}[/green]")
            except NoMatches:
                pass
        except Exception as e:
            try:
                self.query_one(f"#{res_id}", Static).update(f"[red]Error: {escape(str(e))}[/red]")
            except NoMatches:
                pass

    # ─── Key bindings ────────────────────────────────────────────────

    def _switch_tab(self, tab_id: str) -> None:
        try:
            self.query_one("#main-tabs", TabbedContent).active = tab_id
        except (NoMatches, Exception):
            pass

    def action_tab_home(self) -> None:
        self._switch_tab("tab-home")

    def action_tab_plugins(self) -> None:
        self._switch_tab("tab-plugins")

    def action_tab_config(self) -> None:
        self._switch_tab("tab-config")

    def action_tab_logs(self) -> None:
        self._switch_tab("tab-logs")

    def action_tab_settings(self) -> None:
        self._switch_tab("tab-settings")

    def action_refresh(self) -> None:
        try:
            self._refresh_stats_worker()
            self._refresh_plugin_table_worker()
            self._refresh_requests_worker()
            self._build_config_file_list()
        except Exception:
            pass

    def action_request_quit(self) -> None:
        self._confirm_quit()

    def action_force_quit(self) -> None:
        self.exit()

    @work(thread=False, exclusive=True, group="quit")
    async def _confirm_quit(self) -> None:
        result = await self.push_screen(QuitConfirmScreen(), wait_for_dismiss=True)
        if result:
            self.exit()
