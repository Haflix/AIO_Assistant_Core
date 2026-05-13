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
import collections
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
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
from textual.worker import Worker, WorkerState
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

if "tui_dashboard.log_handler" in _sys.modules:
    TUILogHandler = _sys.modules["tui_dashboard.log_handler"].TUILogHandler
else:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "tui_dashboard.log_handler",
        os.path.join(os.path.dirname(__file__), "log_handler.py"),
    )
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    TUILogHandler = _mod.TUILogHandler

if "tui_dashboard.request_tracker" in _sys.modules:
    RequestTracker = _sys.modules["tui_dashboard.request_tracker"].RequestTracker
else:
    import importlib.util as _ilu2
    _spec2 = _ilu2.spec_from_file_location(
        "tui_dashboard.request_tracker",
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
DEFAULT_NETWORK_INTERVAL = 3.0  # Phase 1 — peers-table refresh tick


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
#config-container { height: 1fr; }
#config-selector { height: auto; layout: horizontal; padding: 0 0 1 0; }
#config-editor { height: 1fr; }
#config-actions { height: auto; layout: horizontal; padding: 1 0 0 0; }
#config-actions Button { margin: 0 1 0 0; }
#config-status { padding: 0 1; }

/* ── Logs ────────────────────────────────── */
#logs-container { height: 1fr; }
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
#top-plugins-table { height: auto; max-height: 10; border: round #404040; background: #2d2d2d; }

/* ── Networking ──────────────────────────── */
#net-scroll { height: 1fr; }
.net-card {
    border: round #404040;
    padding: 1 2;
    margin: 0 0 1 0;
    height: auto;
    background: #2d2d2d;
}
.net-card-title { color: #c7a06e; text-style: bold; padding: 0 0 1 0; }
#net-disabled-banner { padding: 1 2; color: #808080; height: auto; }
#net-bootstrap-card { border: round #c7a06e; }
.bootstrap-title { color: #c7a06e; text-style: bold; padding: 0 0 1 0; }
#net-peers-table { height: auto; max-height: 16; }
#net-peer-detail { padding: 1 2; height: auto; color: #d4d4d4; }
#net-bootstrap-fp, #net-bootstrap-instructions {
    padding: 0 0 1 0; height: auto;
}
/* Horizontal widget defaults to horizontal layout — only height + padding needed. */
.net-row { height: auto; padding: 0 0 1 0; }
.net-row-label { color: #808080; width: 25; }
.net-row-value { color: #d4d4d4; width: 1fr; }

/* Phase 2 — cluster summary line + counters card + event log + cert expiry. */
#net-cluster-summary { padding: 0 1; margin: 0 0 1 0; color: #c7a06e; }
.net-counter-row {
    layout: grid;
    grid-size: 5 1;
    grid-columns: 1fr 1fr 1fr 1fr 1fr;
    height: auto;
    padding: 0 0 1 0;
}
.net-counter-card {
    border: round #404040;
    background: #2d2d2d;
    padding: 0 1;
    margin: 0 1 0 0;
    height: 3;
}
.net-counter-label { color: #808080; }
.net-counter-value { color: #d4d4d4; text-style: bold; }
.net-counter-actions { height: auto; }
.cert-expiry-good { color: #73c991; }
.cert-expiry-warn { color: #cca75a; }
.cert-expiry-bad  { color: #d16969; }
#net-event-log {
    height: 10;
    max-height: 14;
    border: round #404040;
    background: #252525;
}

/* Phase 4a — per-peer drill-down tab. */
.peer-tab-body { height: 1fr; padding: 1 2; }
.peer-identity-box {
    border: round #404040;
    background: #2d2d2d;
    padding: 1 2;
    margin: 0 0 1 0;
    height: auto;
}
.peer-sparkline-row { height: 8; padding: 0 0 1 0; }
.peer-sparkline-box {
    border: round #404040;
    background: #2d2d2d;
    padding: 0 1;
    margin: 0 1 0 0;
    height: 6;
    width: 1fr;
}
.peer-sparkline-title { color: #9bb5a0; height: 1; }
.peer-actions-row { height: auto; padding: 1 0 0 0; }
.peer-action-btn { margin: 0 1 0 0; }

/* Phase 4b — drill-down subs tables + in-flight panel + per-peer log. */
.peer-subs-row { height: auto; padding: 0 0 1 0; }
.peer-subs-row > DataTable {
    width: 1fr;
    height: auto;
    max-height: 10;
    margin: 0 1 0 0;
}
.peer-inflight { padding: 0 0 1 0; color: #d4d4d4; }
.peer-eventlog {
    height: 6;
    max-height: 10;
    border: round #404040;
    background: #252525;
}

/* Phase 4c — drill-down quick-action feedback Static. */
.peer-action-status { color: #73c991; padding: 0 1; }

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
.settings-net-disabled { color: #808080; padding: 0 0 1 0; height: auto; }
/* Phase 5 — Settings → Networking additions. Without `height: auto` the
   rebuild Static collapses to zero height in a Vertical, defeating the
   indicator's purpose. The fingerprint row carries a [View cert] Button
   alongside a value Static; the explicit width:1fr + left margin on the
   Button keep the row laid out predictably. */
.settings-net-warn { color: #cca75a; padding: 0 0 1 0; height: auto; }
#info-settings-net-fingerprint { width: 1fr; }
#btn-settings-net-view-cert { margin: 0 0 0 1; }

/* ── Plugin view ─────────────────────────── */
.plugin-view-container { height: 1fr; padding: 1 2; }
.plugin-view-container > Horizontal { height: auto; padding: 0 0 1 0; }
.plugin-view-container > Horizontal > Button { margin: 0 1 0 0; }

.view-mode-bar {
    height: auto;
    layout: horizontal;
    padding: 0 0 1 0;
    dock: top;
}
.view-mode-bar Button {
    margin: 0 1 0 0;
    min-width: 16;
}
.view-mode-bar .view-bar-spacer {
    width: 1fr;
}
.view-mode-bar .close-tab-btn {
    margin: 0 0 0 1;
}
.view-mode-bar .active-mode {
    background: #2d3340;
    color: #7dade0;
    text-style: bold;
}
.view-mode-bar .inactive-mode {
    background: #3c3c3c;
    color: #808080;
}

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


class CertPEMScreen(ModalScreen[None]):
    """Modal display of a TLS cert PEM (own or peer).

    Phase 3 — replaces the Cert PEM `Collapsible` widgets that Phase 1
    dropped. Collapsibles reserve vertical space for their hidden body
    in Textual 8.2.3, which made the This-Node and Bootstrap cards
    bloat by ~25 lines per cert. A modal gives the operator the full
    PEM on demand without permanent layout cost.

    The constructor takes the cert PEM text and its fingerprint
    explicitly so the same screen renders both the own cert (called
    with `nm.own_fingerprint`) and any peer cert (called with
    `peer.fingerprint` from Phase 4a's drill-down View-cert button).
    """

    DEFAULT_CSS = """
    CertPEMScreen {
        align: center middle;
    }
    #cert-modal-body {
        width: 80;
        height: auto;
        max-height: 32;
        border: thick #c7a06e;
        background: #252525;
        padding: 1 2;
    }
    #cert-modal-title { color: #c7a06e; text-style: bold; padding: 0 0 1 0; }
    #cert-modal-fp { color: #c7a06e; padding: 0 0 1 0; }
    #cert-modal-pem {
        background: #1e1e1e;
        color: #9bb5a0;
        padding: 1 2;
        max-height: 25;
    }
    #cert-modal-actions { height: auto; padding: 1 0 0 0; }
    #cert-modal-actions Button { margin: 0 1 0 0; }
    """

    BINDINGS = [
        # `dismiss` resolves to Screen.action_dismiss inherited from
        # textual.screen — no custom action method needed.
        Binding("escape", "dismiss", "Close", show=False),
    ]

    def __init__(self, *, title: str, pem_text: str, fingerprint: str) -> None:
        super().__init__()
        self._title = title
        self._pem = pem_text
        self._fp = fingerprint

    def compose(self) -> ComposeResult:
        with Vertical(id="cert-modal-body"):
            yield Static(self._title, id="cert-modal-title")
            # markup=False so a future peer-cert fingerprint that
            # happens to contain `[` / `]` cannot be misparsed as Rich
            # markup tags (Phase 4a/c will reuse this modal for peers).
            yield Static(f"Fingerprint: {self._fp}",
                         id="cert-modal-fp", markup=False)
            yield Static(self._pem, id="cert-modal-pem", markup=False)
            with Horizontal(id="cert-modal-actions"):
                yield Button("Copy PEM", id="cert-copy")
                yield Button("Close", id="cert-close")

    @on(Button.Pressed, "#cert-copy")
    def _on_copy(self) -> None:
        # OSC 52 clipboard write; no-op on terminals without OSC 52
        # support (notably macOS Terminal). Best-effort copy.
        try:
            self.app.copy_to_clipboard(self._pem)
        except Exception:
            pass

    @on(Button.Pressed, "#cert-close")
    def _on_close(self) -> None:
        self.dismiss()


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
        Binding("5", "tab_networking", "Networking"),
        Binding("6", "tab_settings", "Settings"),
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
        self._network_interval = DEFAULT_NETWORK_INTERVAL  # Phase 1

        # Timer references for restart on settings change
        self._stats_timer = None
        self._plugin_timer = None
        self._request_timer = None
        self._log_timer = None
        self._network_timer = None  # Phase 1

        # ID registry for dynamic widgets
        self._id_counter = 0
        self._id_registry: Dict[str, Dict[str, str]] = {}
        self._plugin_tab_map: Dict[str, str] = {}
        self._plugin_tab_modes: Dict[str, str] = {}

        # Plugin search filter
        self._plugin_filter: str = ""

        # Phase 2 — cert-expiry cache. Bounded FIFO: keys are
        # `own:<path>:<mtime_ns>` or `peer:<hostname>:<sha256-prefix>`;
        # values are the parsed `cert.not_valid_after_utc` datetime.
        # `popitem(last=False)` on insert evicts the oldest entry once
        # the cap is reached. Bounded so a long-running TUI with many
        # peer-cert rotations cannot leak entries.
        self._cert_expiry_cache: collections.OrderedDict = collections.OrderedDict()
        self._cert_expiry_cache_cap = 64

        # Phase 4a — drill-down tab registry. OrderedDict so FIFO
        # eviction at the 5-tab cap drops the oldest-opened tab.
        # Values are the dynamically-created TabPane ids.
        self._peer_tabs: collections.OrderedDict = collections.OrderedDict()
        self._peer_tabs_cap = 5
        # Phase 4b — per-peer event log baseline (deque length at tab
        # mount). New events with index >= baseline are streamed into
        # the per-peer RichLog by `on_peer_event_bus`; rehydration on
        # mount renders the < baseline entries. Eliminates double-renders.
        self._peer_log_baselines: dict = {}
        # Hostnames whose per-peer log has been hydrated. The retry
        # path of `_populate_peer_drill_widgets` re-fetches a fresh
        # history snapshot; without this flag, events that arrived
        # between the first (bailed) call and the retry would be
        # written twice — once by `on_peer_event_bus` (the baseline
        # was set on the first call so the gate passed), once by the
        # retry's hydration.
        self._peer_log_hydrated: set = set()
        # Retry counter for `_populate_peer_drill_widgets` when the
        # parent containers aren't yet queryable. Capped per-tab.
        self._peer_drill_populate_attempts: dict = {}

        # Phase 5 — Settings → Networking group uptime tracker. Captures
        # `time.time()` on `is_ready` False → True transitions, AND
        # invalidates on NM instance swap (a hot-reload rebuild swaps
        # `pc.network` for a fresh NetworkManager whose own `is_ready`
        # flips independently of the previous one). Keyed by `id(nm)`
        # so a same-flag/different-instance situation resets cleanly.
        self._networking_started_at = None
        self._networking_instance_id = None
        # Phase 4a — per-peer ring buffers feeding the throughput sparklines.
        # Each host's entry is {bytes_sent_delta, bytes_recv_delta,
        # msgs_sent_delta, msgs_recv_delta, last_sample, last_sample_nm_id}.
        # last_sample_nm_id captures id(pc.network) at last sample so a
        # mid-session NM rebuild invalidates the prior cumulative counters
        # (peer_stats is recreated on the new NM and starts at 0).
        self._peer_ring_buffers: dict = {}
        # Single app-level 1s timer drives all open drill-down refreshes.
        # Created lazily on first open; stopped when last tab closes.
        self._peer_drill_timer = None

    # ─── Cross-loop dispatch ────────────────────────────────────────

    async def _run_on_main(self, coro, timeout: float = 30.0):
        """Schedule a coroutine on the main event loop and await its result.

        PluginCore's async methods (execute, _enable_plugin, etc.) use
        asyncio primitives bound to the main loop. Awaiting them directly
        from the TUI thread's loop would use the wrong event loop, breaking
        locks, tasks, and futures. This helper dispatches correctly.

        Returns None (instead of crashing) if the main loop is
        missing / closed / stopped — this happens during shutdown
        and must not take down the TUI. Also: tests using a hand-
        written FakePlugin pass `event_loop=None`, which makes
        `_main_loop` None; without the explicit None guard, the
        `.is_closed()` call would raise AttributeError and the
        outer try/except in callers would mask a silent no-op.
        """
        if self._main_loop is None or self._main_loop.is_closed():
            coro.close()  # prevent "coroutine never awaited" warning
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._main_loop)
        # Wrap the concurrent.futures.Future so we can await it on Textual's loop.
        # Timeout prevents a hung PluginCore call from freezing the entire TUI.
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
        except asyncio.TimeoutError:
            future.cancel()
            raise
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception:
            future.cancel()
            raise

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

    def _cleanup_registry_for_plugin(self, plugin_name: str,
                                      exclude_types: set | None = None) -> None:
        to_remove = [
            k for k, v in self._id_registry.items()
            if v.get("plugin") == plugin_name
            and (exclude_types is None
                 or v.get("type") not in exclude_types)
        ]
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
                with Vertical(id="config-container"):
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
                with Vertical(id="logs-container"):
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

            # ── 5. Networking ────────────────────────────────────
            with TabPane("Networking", id="tab-networking"):
                with VerticalScroll(id="net-scroll"):
                    # Banner — visible only when networking disabled.
                    yield Static(
                        "Networking is disabled. Set networking.enabled: "
                        "true in config.yml and Reload to enable.",
                        id="net-disabled-banner",
                    )

                    # Phase 2 — Cluster summary strip. Single-line glance:
                    #   `3 peers · 2/3 alive · own_fp:sha256:abc…`
                    # Updated by `_refresh_peers_table_worker` on each tick.
                    yield Static("...", id="net-cluster-summary",
                                 classes="net-card-title")

                    # Bootstrap helper — visible only when applicable.
                    with Vertical(id="net-bootstrap-card", classes="net-card"):
                        yield Static("Cluster bootstrap ready",
                                     classes="bootstrap-title")
                        yield Static("...", id="net-bootstrap-fp",
                                     markup=False)
                        yield Static(
                            "Paste this node's fingerprint and cert into "
                            "another node's networking.peers block. Once "
                            "peers reference each other, restart both nodes.",
                            id="net-bootstrap-instructions",
                        )
                        # Phase 3 — modal-backed View PEM. Replaces the
                        # height-reserving Collapsible deleted in Phase 1.
                        yield Button("View bootstrap PEM",
                                     id="btn-net-bootstrap-view-cert")

                    # This Node card — label/value rows replace heavy DataTable.
                    with Vertical(id="net-this-node", classes="net-card"):
                        yield Static("This Node", classes="net-card-title")
                        with Horizontal(classes="net-row"):
                            yield Static("Hostname:", classes="net-row-label")
                            yield Static("...", id="info-net-thisnode-hostname", classes="net-row-value")
                        with Horizontal(classes="net-row"):
                            yield Static("Port:", classes="net-row-label")
                            yield Static("...", id="info-net-thisnode-port", classes="net-row-value")
                        with Horizontal(classes="net-row"):
                            yield Static("Keys dir:", classes="net-row-label")
                            yield Static("...", id="info-net-thisnode-keysdir", classes="net-row-value")
                        with Horizontal(classes="net-row"):
                            yield Static("Pool size:", classes="net-row-label")
                            yield Static("...", id="info-net-thisnode-pool", classes="net-row-value")
                        with Horizontal(classes="net-row"):
                            yield Static("Discoverable:", classes="net-row-label")
                            yield Static("...", id="info-net-thisnode-discoverable", classes="net-row-value")
                        with Horizontal(classes="net-row"):
                            yield Static("Fingerprint:", classes="net-row-label")
                            yield Static("...", id="info-net-thisnode-fingerprint", classes="net-row-value")
                        # Phase 2 — cert-expiry warning (min across own + peer certs).
                        with Horizontal(classes="net-row"):
                            yield Static("Cert expires:", classes="net-row-label")
                            yield Static("...", id="net-cert-expiry", classes="net-row-value")
                        # Phase 3 — modal-backed View cert. Replaces the
                        # height-reserving Cert PEM Collapsible.
                        yield Button("View cert",
                                     id="btn-net-thisnode-view-cert")

                    # Discovery / heartbeat strip — label/value rows.
                    with Vertical(id="net-discovery", classes="net-card"):
                        yield Static("Discovery / heartbeat",
                                     classes="net-card-title")
                        with Horizontal(classes="net-row"):
                            yield Static("discover_nodes:", classes="net-row-label")
                            yield Static("...", id="info-net-disc-discover", classes="net-row-value")
                        with Horizontal(classes="net-row"):
                            yield Static("auto_discoverable:", classes="net-row-label")
                            yield Static("...", id="info-net-disc-auto", classes="net-row-value")
                        with Horizontal(classes="net-row"):
                            yield Static("direct_discoverable:", classes="net-row-label")
                            yield Static("...", id="info-net-disc-direct", classes="net-row-value")
                        with Horizontal(classes="net-row"):
                            yield Static("heartbeat_interval:", classes="net-row-label")
                            yield Static("...", id="info-net-disc-hb", classes="net-row-value")
                        with Horizontal(classes="net-row"):
                            yield Static("lookup_interval:", classes="net-row-label")
                            yield Static("...", id="info-net-disc-lookup", classes="net-row-value")
                        with Horizontal(classes="net-row"):
                            yield Static("liveness_timeout:", classes="net-row-label")
                            yield Static("...", id="info-net-disc-liveness", classes="net-row-value")

                    # Phase 2 — Counters card. Pending-ack badge + 4
                    # disconnect-reason counters + [Clear counters] button.
                    with Vertical(id="net-counters", classes="net-card"):
                        yield Static("Counters", classes="net-card-title")
                        with Horizontal(classes="net-counter-row"):
                            with Vertical(classes="net-counter-card"):
                                yield Static("Pending acks", classes="net-counter-label")
                                yield Static("0", id="net-counter-pending-acks", classes="net-counter-value")
                            with Vertical(classes="net-counter-card"):
                                yield Static("Disconnect: normal", classes="net-counter-label")
                                yield Static("0", id="net-counter-discon-normal", classes="net-counter-value")
                            with Vertical(classes="net-counter-card"):
                                yield Static("Disconnect: conn_error", classes="net-counter-label")
                                yield Static("0", id="net-counter-discon-conn", classes="net-counter-value")
                            with Vertical(classes="net-counter-card"):
                                yield Static("Disconnect: rce_attempt", classes="net-counter-label")
                                yield Static("0", id="net-counter-discon-rce", classes="net-counter-value")
                            with Vertical(classes="net-counter-card"):
                                yield Static("Disconnect: error", classes="net-counter-label")
                                yield Static("0", id="net-counter-discon-error", classes="net-counter-value")
                        with Horizontal(classes="net-counter-actions"):
                            yield Button("Clear counters",
                                         id="btn-net-clear-counters")

                    # Peers table.
                    with Vertical(id="net-peers", classes="net-card"):
                        yield Static("Peers", classes="net-card-title")
                        yield DataTable(id="net-peers-table",
                                        cursor_type="row")
                        yield Static("", id="net-peer-detail",
                                     markup=True)

                    # Phase 2 — Network event log strip. Bus-driven, no
                    # polling. Renders from `self.plugin_instance`'s
                    # `_recent_peer_events` deque on each new event.
                    yield RichLog(id="net-event-log",
                                  max_lines=200, markup=True,
                                  classes="net-card")

            # ── 6. Settings ──────────────────────────────────────
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
                    with Vertical(classes="settings-group", id="settings-net-group"):
                        yield Static("Networking", classes="settings-group-title")
                        # Disabled placeholder — visible only when networking off.
                        yield Static(
                            "Networking disabled. Set networking.enabled: true "
                            "in config.yml to enable.",
                            id="settings-net-disabled",
                            classes="settings-net-disabled",
                        )
                        # Data rows — hidden when networking off.
                        with Vertical(id="settings-net-data"):
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
                                yield Static("Peers:", classes="setting-label")
                                yield Static("...", id="info-net-nodes", classes="setting-value")
                            with Horizontal(classes="setting-row"):
                                yield Static("Heartbeat interval (s):", classes="setting-label")
                                yield Static("...", id="info-net-heartbeat", classes="setting-value")
                            with Horizontal(classes="setting-row"):
                                yield Static("Lookup interval (s):", classes="setting-label")
                                yield Static("...", id="info-net-lookup", classes="setting-value")
                            with Horizontal(classes="setting-row"):
                                yield Static("Liveness timeout (s):", classes="setting-label")
                                yield Static("...", id="info-net-liveness", classes="setting-value")
                            # Phase 5 — identity + uptime + secret status.
                            with Horizontal(classes="setting-row"):
                                yield Static("Fingerprint:", classes="setting-label")
                                yield Static("...",
                                             id="info-settings-net-fingerprint",
                                             classes="setting-value")
                                yield Button("View cert",
                                             id="btn-settings-net-view-cert")
                            with Horizontal(classes="setting-row"):
                                yield Static("Keys dir:", classes="setting-label")
                                yield Static("...",
                                             id="info-settings-net-keysdir",
                                             classes="setting-value")
                            with Horizontal(classes="setting-row"):
                                yield Static("Cert file:", classes="setting-label")
                                yield Static("...",
                                             id="info-settings-net-certfile",
                                             classes="setting-value")
                            with Horizontal(classes="setting-row"):
                                yield Static("Pool size:", classes="setting-label")
                                yield Static("...",
                                             id="info-settings-net-poolsize",
                                             classes="setting-value")
                            with Horizontal(classes="setting-row"):
                                yield Static("Uptime:", classes="setting-label")
                                yield Static("...",
                                             id="info-settings-net-uptime",
                                             classes="setting-value")
                            with Horizontal(classes="setting-row"):
                                yield Static("Secret status:", classes="setting-label")
                                yield Static("...",
                                             id="info-settings-net-secret",
                                             classes="setting-value")
                        # Phase 5 — rebuild indicator: visible only when
                        # `pc.networking_enabled AND pc.network is None`.
                        # Initial `display=False` so the one-frame window
                        # between compose and the first `on_mount` →
                        # `_populate_settings_info` call doesn't flash
                        # the rebuild banner on TUIs that start with
                        # networking disabled.
                        rebuild_static = Static(
                            "Networking rebuilding…",
                            id="settings-net-rebuilding",
                            classes="settings-net-warn",
                        )
                        rebuild_static.display = False
                        yield rebuild_static

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
            tpt = self.query_one("#top-plugins-table", DataTable)
            tpt.add_columns("Plugin", "Requests", "Errors", "Avg ms")
        except NoMatches:
            pass

        # Networking tab — Peers DataTable columns. This-Node + Discovery
        # are plain label/value Static rows; no DataTable bootstrap needed.
        try:
            np_t = self.query_one("#net-peers-table", DataTable)
            np_t.add_columns(
                "Hostname", "Address", "system_caller", "Alive", "Last HB",
                "Pool", "In subs", "Out subs", "Inflight", "Bytes (s/r)",
                "FP",
            )
        except NoMatches:
            pass

        # Populate settings info
        self._populate_settings_info()

        # Phase 1 — populate Networking tab static cards + initial
        # peers-table render. Visibility (banner vs cards) follows
        # `pc.networking_enabled`.
        self._populate_networking_static()
        self._refresh_peers_table_worker()

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
        if self._network_timer:  # Phase 1
            self._network_timer.stop()
        self._stats_timer = self.set_interval(self._stats_interval, self._refresh_stats_worker)
        self._plugin_timer = self.set_interval(self._plugin_interval, self._periodic_plugin_refresh)
        self._request_timer = self.set_interval(self._request_interval, self._refresh_requests_worker)
        self._log_timer = self.set_interval(0.5, self._refresh_log_table)
        self._network_timer = self.set_interval(  # Phase 1
            self._network_interval, self._refresh_peers_table_worker,
        )

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
        # Phase 5 — uptime tracker runs every stats tick, even before the
        # `#stat-hostname` widget exists (test harness ordering). Cheap
        # work, no DOM interaction so the early-guard below can stay.
        self._tick_networking_uptime()

        # Early guard — if key widget missing, DOM not ready / being torn down
        try:
            hostname_w = self.query_one("#stat-hostname", Static)
        except NoMatches:
            return

        # Phase 5 — refresh Settings → Networking group additions each
        # tick so uptime + rebuild indicator + cert-path-exists stay
        # current without their own timer.
        try:
            self._populate_settings_phase5_rows()
        except Exception:
            pass

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

            # Networking — derived from pc.network state.
            #   "OFF"                    — networking disabled in config
            #   "ON (N/A)"               — enabled but NetworkManager not yet built
            #                              (pre-start / mid-rebuild)
            #   "ON, X/Y peers alive"    — X = configured peers with fresh
            #                              heartbeat, Y = total configured peers
            self.query_one("#stat-networking", Static).update(
                self._format_net_stat_card()
            )

            # CPU & Memory (process / system)
            if HAS_PSUTIL:
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory()
                # cpu_percent() sums across cores — normalize to match Task Manager view
                raw_cpu = self._process.cpu_percent() if self._process else 0
                proc_cpu = raw_cpu / psutil.cpu_count(logical=True)
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

    @work(thread=False, exclusive=True, group="config-save")
    async def _save_config_file(self) -> None:
        if not self._current_config_file:
            self._set_status("No file loaded", error=True)
            return
        try:
            content = self.query_one("#config-editor", TextArea).text
            await self._run_on_main(
                self._save_config_on_main(self._current_config_file, content)
            )
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

    async def _save_config_on_main(self, path: str, content: str) -> None:
        """Dispatch config save to PluginCore (thread-safe with backup)."""
        self.plugin_core.save_config_file(path, content, backup=True)

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

            # Networking group: hide data rows when networking disabled,
            # show disabled placeholder. Single source of truth for the
            # split is `pc.networking_enabled`.
            net_enabled = getattr(self.plugin_core, "networking_enabled", False)
            try:
                self.query_one("#settings-net-disabled").display = not net_enabled
            except NoMatches:
                pass
            try:
                self.query_one("#settings-net-data").display = net_enabled
            except NoMatches:
                pass

            self.query_one("#info-net-enabled", Static).update("Yes" if net_enabled else "No")
            self.query_one("#info-net-port", Static).update(
                str(getattr(self.plugin_core, "networking_port", "?"))
            )
            auto = getattr(self.plugin_core, "networking_auto_discoverable", False)
            direct = getattr(self.plugin_core, "networking_direct_discoverable", False)
            self.query_one("#info-net-discoverable", Static).update(
                f"Auto: {'Y' if auto else 'N'} | Direct: {'Y' if direct else 'N'}"
            )

            # Peers display — sourced from pc.network.peers (PeerSpec list)
            # when the NetworkManager exists; falls back to YAML
            # networking.peers count when network is None (e.g. networking
            # off but config carries entries). PR4 K-3 removed `node_ips`;
            # reading it raises a hard config error in the framework.
            self.query_one("#info-net-nodes", Static).update(
                self._format_peers_display()
            )

            # B-069 runtime intervals — read from PluginCore-level attrs
            # which mirror the YAML at boot + on async_load_config_yaml.
            self.query_one("#info-net-heartbeat", Static).update(
                str(getattr(self.plugin_core, "networking_heartbeat_interval", "?"))
            )
            self.query_one("#info-net-lookup", Static).update(
                str(getattr(self.plugin_core, "networking_lookup_interval", "?"))
            )
            self.query_one("#info-net-liveness", Static).update(
                str(getattr(self.plugin_core, "networking_liveness_timeout", "?"))
            )

            # Phase 5 additions — identity paths, uptime, secret status,
            # rebuild indicator. All driven from `pc.network` when alive
            # and fall back to placeholder text otherwise.
            self._populate_settings_phase5_rows()

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

    # ─── Phase 5 — Settings → Networking group additions ─────────────

    def _populate_settings_phase5_rows(self) -> None:
        """Refresh the Settings-tab Phase 5 rows.

        Identity paths read from `pc.network` when alive; rebuild
        indicator visible iff `networking_enabled AND network is None`.
        Uptime is tracked via `_tick_networking_uptime` from the stats
        worker — this method just renders the latest value.
        """
        pc = self.plugin_core
        net_enabled = getattr(pc, "networking_enabled", False)
        nm = getattr(pc, "network", None)

        # Rebuild indicator: enabled + NM missing → mid-rebuild.
        try:
            self.query_one("#settings-net-rebuilding").display = (
                net_enabled and nm is None
            )
        except NoMatches:
            pass

        # Identity paths.
        if nm is None:
            self._set_row("#info-settings-net-fingerprint", "(NM not built)")
            self._set_row("#info-settings-net-keysdir", "(NM not built)")
            self._set_row("#info-settings-net-certfile", "(NM not built)")
            self._set_row("#info-settings-net-poolsize", "(NM not built)")
        else:
            fp = (getattr(nm, "own_fingerprint", "") or "(not loaded yet)")
            self._set_row("#info-settings-net-fingerprint", fp)
            self._set_row("#info-settings-net-keysdir",
                          str(getattr(nm, "keys_dir", "?")))
            cert_path = getattr(nm, "cert_path", None)
            if cert_path is None:
                cert_display = "(cert_path not set)"
            else:
                exists = "exists" if cert_path.exists() else "missing"
                cert_display = f"{cert_path} ({exists})"
            self._set_row("#info-settings-net-certfile", cert_display)
            self._set_row("#info-settings-net-poolsize",
                          str(getattr(nm, "pool_size", "?")))

        # Uptime.
        if self._networking_started_at is None:
            self._set_row("#info-settings-net-uptime", "(not started)")
        else:
            elapsed = int(time.time() - self._networking_started_at)
            h, r = divmod(elapsed, 3600)
            m, s = divmod(r, 60)
            self._set_row("#info-settings-net-uptime",
                          f"{h}:{m:02d}:{s:02d}")

        # Secret status — three possible labels per plan v5.
        secret_label = self._secret_status_label()
        self._set_row("#info-settings-net-secret", secret_label)

    def _secret_status_label(self) -> str:
        """Resolve the secret-status label.

        Priority:
          1. `pc.networking_secret` truthy → `set via config` (config takes
             precedence in `networking.py:219` which uses `secret or env`).
          2. else `os.environ.get("NETWORKING_SECRET")` truthy → `set via env`.
          3. else → `unset`.
        """
        pc_secret = getattr(self.plugin_core, "networking_secret", None)
        if pc_secret:
            return "set via config"
        env_secret = os.environ.get("NETWORKING_SECRET")
        if env_secret:
            return "set via env"
        return "unset"

    def _tick_networking_uptime(self) -> None:
        """Update `_networking_started_at` based on `nm.is_ready`
        transitions. Called from `_refresh_stats_worker` once per tick.

        Reset semantics (cycle-3 S-1 fix): `is_ready` toggles within a
        single NM are tracked, AND an NM instance swap (id change)
        resets the timestamp to `now` so a hot-reload rebuild does not
        carry the old uptime.
        """
        nm = getattr(self.plugin_core, "network", None)
        if nm is not None and getattr(nm, "is_ready", False):
            nm_id = id(nm)
            if nm_id != self._networking_instance_id:
                # New NM (or first start) → capture start time.
                self._networking_instance_id = nm_id
                self._networking_started_at = time.time()
        else:
            # NM gone or not ready → drop the tracker.
            if self._networking_instance_id is not None:
                self._networking_instance_id = None
                self._networking_started_at = None

    @on(Button.Pressed, "#btn-settings-net-view-cert")
    def _on_settings_view_cert(self) -> None:
        """Settings-tab View-cert button reuses Phase 3's modal helper."""
        self._open_cert_modal(title="Local node certificate")

    # ─── Phase 1 — Networking tab ────────────────────────────────────

    def _populate_networking_static(self) -> None:
        """One-shot population for the Networking tab's static cards.

        Called on `on_mount` and after every Reload-config click. The
        peers table is refreshed by the periodic worker, so this method
        only handles the cards whose contents change rarely (this-node
        info, discovery/heartbeat strip, bootstrap helper visibility +
        text). All `query_one` calls are wrapped in `try/except
        NoMatches` because the Networking tab may not be present yet
        during an early on-mount race or partial DOM teardown.
        """
        pc = self.plugin_core
        net_enabled = getattr(pc, "networking_enabled", False)

        # Banner vs cards visibility — single switch driven by
        # networking_enabled. Mounted once at compose, toggled here.
        # Phase 2 (H6) — new card IDs added so a mid-session
        # enable/disable flip hides ALL networking widgets uniformly.
        try:
            self.query_one("#net-disabled-banner").display = not net_enabled
        except NoMatches:
            pass
        for cid in ("#net-this-node", "#net-discovery", "#net-peers",
                    "#net-bootstrap-card", "#net-cluster-summary",
                    "#net-counters", "#net-event-log"):
            try:
                self.query_one(cid).display = net_enabled
            except NoMatches:
                pass
        if not net_enabled:
            return

        # Bootstrap helper visibility — additional gate on top of
        # net_enabled. Visible only when `peers=[]` AND the cert PEM
        # exists on disk. The card is hidden in the loop above already
        # if net_enabled is False; here we hide it again if the
        # bootstrap predicate is False even with networking on.
        bootstrap = self._bootstrap_visible()
        try:
            self.query_one("#net-bootstrap-card").display = bootstrap
        except NoMatches:
            pass

        nm = getattr(pc, "network", None)
        if nm is None:
            # Networking is enabled in config but the NM hasn't been
            # constructed yet (or is mid-rebuild). Render stub values
            # so cards don't show stale data from a prior NM.
            self._set_thisnode_rows(
                hostname=getattr(pc, "hostname", "?"),
                port=getattr(pc, "networking_port", "?"),
                keys_dir="(NM not built)",
                pool_size="?",
                discoverable=self._format_discoverable(pc),
                fingerprint="(NM not built)",
            )
            self._set_discovery_rows_pc_only(pc)
            self._set_bootstrap_card_text(
                fingerprint="(NM not built)",
            )
            return

        # NM exists — read its live state.
        self._set_thisnode_rows(
            hostname=getattr(pc, "hostname", "?"),
            port=getattr(pc, "networking_port", "?"),
            keys_dir=str(getattr(nm, "keys_dir", "?")),
            pool_size=getattr(nm, "pool_size", "?"),
            discoverable=self._format_discoverable(pc),
            fingerprint=(getattr(nm, "own_fingerprint", "") or
                         "(not loaded yet)"),
        )
        self._set_discovery_rows(nm, pc)
        self._set_bootstrap_card_text(
            fingerprint=(getattr(nm, "own_fingerprint", "") or
                         "(not loaded yet)"),
        )
        # Phase 2 — cert-expiry row populated alongside identity.
        self._set_cert_expiry_row(nm)

    @staticmethod
    def _format_discoverable(pc) -> str:
        auto = getattr(pc, "networking_auto_discoverable", False)
        direct = getattr(pc, "networking_direct_discoverable", False)
        return f"auto:{'Y' if auto else 'N'} | direct:{'Y' if direct else 'N'}"

    @staticmethod
    def _read_cert_pem_safe(nm) -> str:
        """Read the cert PEM from disk. Defensive against missing file
        + the rare null `cert_path` (theoretically always-set, but
        guard anyway per cycle-1 review).

        Retained in Phase 1 for Phase 3's cert modal — modal reads the
        PEM here on every open instead of caching in the DOM.
        """
        cert_path = getattr(nm, "cert_path", None)
        if cert_path is None:
            return "(cert_path not set)"
        try:
            if not cert_path.exists():
                return "(cert.pem not yet on disk)"
            return cert_path.read_text(encoding="utf-8")
        except Exception as e:
            return f"(read failed: {e})"

    def _set_row(self, widget_id: str, value: str) -> None:
        """Defensive Static.update for any label/value row Static.

        Used by the Networking-tab This-Node + Discovery rows
        (`#info-net-*`), the Settings → Networking group additions
        (`#info-settings-net-*`), and the per-peer drill-down identity
        strip (`#tab-peer-<host>-identity-*`).
        """
        try:
            self.query_one(widget_id, Static).update(value)
        except NoMatches:
            pass

    def _set_thisnode_rows(
        self, *, hostname, port, keys_dir, pool_size, discoverable,
        fingerprint,
    ) -> None:
        self._set_row("#info-net-thisnode-hostname", str(hostname))
        self._set_row("#info-net-thisnode-port", str(port))
        self._set_row("#info-net-thisnode-keysdir", str(keys_dir))
        self._set_row("#info-net-thisnode-pool", str(pool_size))
        self._set_row("#info-net-thisnode-discoverable", discoverable)
        self._set_row("#info-net-thisnode-fingerprint", str(fingerprint))

    def _set_discovery_rows(self, nm, pc) -> None:
        self._set_row("#info-net-disc-discover",
                      str(getattr(nm, "discover_nodes", "?")))
        self._set_row("#info-net-disc-auto",
                      str(getattr(pc, "networking_auto_discoverable", "?")))
        self._set_row("#info-net-disc-direct",
                      str(getattr(pc, "networking_direct_discoverable", "?")))
        self._set_row("#info-net-disc-hb",
                      str(getattr(pc, "networking_heartbeat_interval", "?")))
        self._set_row("#info-net-disc-lookup",
                      str(getattr(pc, "networking_lookup_interval", "?")))
        self._set_row("#info-net-disc-liveness",
                      str(getattr(pc, "networking_liveness_timeout", "?")))

    def _set_discovery_rows_pc_only(self, pc) -> None:
        """Stub variant when NM is None — only PC-level B-069 attrs are
        available; discover_nodes lives on NM only."""
        self._set_row("#info-net-disc-discover", "(NM not built)")
        self._set_row("#info-net-disc-auto",
                      str(getattr(pc, "networking_auto_discoverable", "?")))
        self._set_row("#info-net-disc-direct",
                      str(getattr(pc, "networking_direct_discoverable", "?")))
        self._set_row("#info-net-disc-hb",
                      str(getattr(pc, "networking_heartbeat_interval", "?")))
        self._set_row("#info-net-disc-lookup",
                      str(getattr(pc, "networking_lookup_interval", "?")))
        self._set_row("#info-net-disc-liveness",
                      str(getattr(pc, "networking_liveness_timeout", "?")))

    def _set_bootstrap_card_text(self, *, fingerprint: str) -> None:
        try:
            self.query_one("#net-bootstrap-fp", Static).update(
                f"Fingerprint: {fingerprint}"
            )
        except NoMatches:
            pass

    # ─── Phase 2 — cert expiry, cluster summary, counters ────────────

    def _min_cert_expiry_days(self, nm) -> tuple:
        """Walk own + per-peer certs and return (min_days, source_label).

        Caches parsed `cert.not_valid_after_utc` per (path, mtime) for the
        own cert, and per (hostname, sha256-prefix) for peer PEMs. The
        cache is a bounded FIFO (OrderedDict + popitem(last=False)) so a
        long-running TUI cannot leak entries across cert rotations.

        Malformed peer certs are silently skipped (PeerSpec construction
        already validates at config-load — this is defense in depth).
        Returns (None, "(no certs)") when nothing parses.
        """
        from cryptography import x509

        candidates: list = []
        now_utc = datetime.now(timezone.utc)

        # Own cert
        try:
            cert_path = getattr(nm, "cert_path", None)
            if cert_path is not None and cert_path.exists():
                mtime_ns = cert_path.stat().st_mtime_ns
                cache_key = f"own:{cert_path}:{mtime_ns}"
                cached = self._cert_expiry_cache.get(cache_key)
                if cached is None:
                    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
                    cached = cert.not_valid_after_utc
                    self._cert_expiry_cache[cache_key] = cached
                    while len(self._cert_expiry_cache) > self._cert_expiry_cache_cap:
                        self._cert_expiry_cache.popitem(last=False)
                candidates.append(((cached - now_utc).days, "own"))
        except Exception:
            pass  # malformed own cert — skip silently

        # Peer certs
        for peer in getattr(nm, "peers", []) or []:
            try:
                # S-E: empty PEM should never happen post-config-load
                # (NetworkManager._parse_one_peer validates), but guard
                # anyway so a future schema change can't silently collapse
                # all peers into one cache slot via empty-PEM hash collision.
                if not getattr(peer, "cert_pem", None):
                    continue
                pem_hash = hashlib.sha256(peer.cert_pem.encode()).hexdigest()[:16]
                cache_key = f"peer:{peer.hostname}:{pem_hash}"
                cached = self._cert_expiry_cache.get(cache_key)
                if cached is None:
                    cert = x509.load_pem_x509_certificate(peer.cert_pem.encode())
                    cached = cert.not_valid_after_utc
                    self._cert_expiry_cache[cache_key] = cached
                    while len(self._cert_expiry_cache) > self._cert_expiry_cache_cap:
                        self._cert_expiry_cache.popitem(last=False)
                candidates.append(((cached - now_utc).days, f"peer:{peer.hostname}"))
            except Exception:
                continue  # malformed peer cert — skip

        if not candidates:
            return (None, "(no certs)")
        return min(candidates, key=lambda x: x[0])

    def _set_cert_expiry_row(self, nm) -> None:
        """Render the cert-expiry warning row in the This-Node card."""
        try:
            widget = self.query_one("#net-cert-expiry", Static)
        except NoMatches:
            return
        days, source = self._min_cert_expiry_days(nm)
        # Reset CSS classes so a recompute can flip colors.
        for cls in ("cert-expiry-good", "cert-expiry-warn", "cert-expiry-bad"):
            widget.remove_class(cls)
        if days is None:
            widget.update("(no certs to check)")
            return
        if days >= 30:
            widget.add_class("cert-expiry-good")
        elif days >= 7:
            widget.add_class("cert-expiry-warn")
        else:
            widget.add_class("cert-expiry-bad")
        widget.update(f"in {days} days ({source})")

    def _format_cluster_summary(self) -> str:
        """Render the cluster-summary single-line label.

        Format: `N peers · X/N alive · own_fp:sha256:abc…`.
        Returns the OFF / N/A short-forms when networking isn't fully up.

        Phase 2 cycle-review fix: `alive` here counts nodes whose
        heartbeat is FRESH within `heartbeat_interval` — matching the
        peers-table column's "alive" band exactly. The previous
        implementation used `is_alive_sync(timeout=liveness_timeout)`
        which counts everything up to liveness_timeout, so the cluster
        summary would say "2/3 alive" while the peers table marked one
        of those rows as "degraded" (yellow). Single semantic now.
        """
        pc = self.plugin_core
        if not getattr(pc, "networking_enabled", False):
            return "Networking OFF"
        nm = getattr(pc, "network", None)
        if nm is None:
            return "Networking ON, NetworkManager not yet built"
        try:
            peers = list(getattr(nm, "peers", []) or [])
            nodes = list(getattr(nm, "nodes", []) or [])
            hb_interval = getattr(nm, "heartbeat_interval", 10)
            own_fp = getattr(nm, "own_fingerprint", "") or ""
        except Exception:
            return "Networking ON (N/A)"
        configured = {p.hostname for p in peers}
        nodes_by_host = {n.hostname: n for n in nodes
                         if n.hostname in configured}
        now = time.time()
        alive = 0
        # `node.enabled` is intentionally NOT checked here — the peers
        # table's alive-cell logic ignores it too, and consistency
        # between the two surfaces matters more than the philosophical
        # question of whether a disabled-but-heartbeating peer counts.
        for host in configured:
            node = nodes_by_host.get(host)
            if node is None:
                continue
            last_hb = getattr(node, "last_heartbeat", None)
            if last_hb is None:
                continue
            if now - last_hb < hb_interval:
                alive += 1
        fp_disp = (own_fp[:24] + "…") if own_fp else "(pending)"
        return f"{len(peers)} peers · {alive}/{len(peers)} alive · own_fp:{fp_disp}"

    def _refresh_cluster_summary(self) -> None:
        try:
            self.query_one("#net-cluster-summary", Static).update(
                self._format_cluster_summary()
            )
        except NoMatches:
            pass

    def _count_pending_acks(self, nm) -> int:
        """Count outbound advert entries past one heartbeat without ack."""
        try:
            outbound = getattr(nm, "_outbound_adverts", None) or {}
            hb_interval = getattr(nm, "heartbeat_interval", 10)
            now = time.time()
            count = 0
            # Outer-then-inner copy pattern (peers-table worker convention).
            outbound_copy = dict(outbound)
            for host, sub_map in outbound_copy.items():
                try:
                    sub_snapshot = dict(sub_map)
                except (RuntimeError, Exception):
                    continue
                for sub in sub_snapshot.values():
                    if getattr(sub, "state", "") != "pending":
                        continue
                    sent_at = getattr(sub, "sent_at", None)
                    if sent_at is None:
                        continue
                    if now - sent_at > hb_interval:
                        count += 1
            return count
        except (RuntimeError, Exception):
            return 0

    def _refresh_counters(self, nm) -> None:
        """Populate pending-ack badge + disconnect-reason counters.

        Each widget update is independently guarded so a missing widget
        (eg. compose race or future ID rename) only skips that one slot
        — the rest of the counter card still updates.
        """
        # Pending acks — independent guard so disconnect-reason loop below
        # still runs even if this specific widget is absent.
        try:
            pa = self.query_one("#net-counter-pending-acks", Static)
            pending = self._count_pending_acks(nm) if nm is not None else 0
            pa.update(str(pending))
            pa.remove_class("stat-val-bad")
            if pending > 0:
                pa.add_class("stat-val-bad")
        except NoMatches:
            pass

        # Disconnect-reason counts via plugin-side snapshot. Defensive in
        # case plugin teardown raced this tick (H-B) AND in case the
        # plugin_instance is a test-time mock that returns non-dict
        # auto-attrs from `get_disconnect_reason_counts()`.
        plugin = self.plugin_instance
        counts: dict
        try:
            getter = getattr(plugin, "get_disconnect_reason_counts", None)
            if callable(getter):
                raw = getter()
                counts = raw if isinstance(raw, dict) else {}
            else:
                counts = {}
        except Exception:
            counts = {}
        for cid, key in (
            ("#net-counter-discon-normal", "normal"),
            ("#net-counter-discon-conn", "connection_error"),
            ("#net-counter-discon-rce", "rce_attempt"),
            ("#net-counter-discon-error", "error"),
        ):
            try:
                widget = self.query_one(cid, Static)
            except NoMatches:
                continue
            raw_v = counts.get(key, 0)
            v = raw_v if isinstance(raw_v, int) else 0
            widget.update(str(v))
            widget.remove_class("stat-val-bad")
            if key == "rce_attempt" and v > 0:
                widget.add_class("stat-val-bad")

    def _bootstrap_visible(self) -> bool:
        """Predicate for showing the bootstrap-helper card.

        Visible iff networking is enabled AND no peers are configured
        AND the cert.pem file exists on disk. The third condition
        avoids advertising "ready" before identity provisioning has
        finished writing the cert.
        """
        pc = self.plugin_core
        if not getattr(pc, "networking_enabled", False):
            return False
        nm = getattr(pc, "network", None)
        if nm is None:
            return False
        if list(getattr(nm, "peers", []) or []):
            return False
        cert_path = getattr(nm, "cert_path", None)
        if cert_path is None:
            return False
        try:
            return cert_path.exists()
        except Exception:
            return False

    @work(thread=False, exclusive=True, group="networking")
    async def _refresh_peers_table_worker(self) -> None:
        """Periodic peers-table refresh.

        Snapshots the lock-protected NetworkManager dicts before
        iterating. Inner `AdvertSub` objects in the snapshot are
        SHARED references — the peers table only reads scalar fields
        for display, so eventual consistency is fine. Early-returns
        when networking is disabled (banner already covers this state).

        Phase 2 — also drives the cluster-summary line + counters card
        + cert-expiry row so they refresh on every tick alongside the
        peers table.
        """
        pc = self.plugin_core
        if not getattr(pc, "networking_enabled", False):
            return
        # Phase 2 — always re-render cluster summary (cheap, derived).
        self._refresh_cluster_summary()
        nm = getattr(pc, "network", None)
        if nm is None:
            try:
                t = self.query_one("#net-peers-table", DataTable)
            except NoMatches:
                return
            t.clear()
            # Counters still useful without an NM (only disconnect-reason
            # counts will be live; pending-acks defaults to 0).
            self._refresh_counters(None)
            return
        # Phase 2 — refresh counters + cert expiry on each tick.
        self._refresh_counters(nm)
        self._set_cert_expiry_row(nm)

        try:
            peers = list(getattr(nm, "peers", []) or [])
            nodes_list = list(getattr(nm, "nodes", []) or [])
            # Outer-then-inner snapshot pattern. `dict.copy()` /
            # `set(...)` are C-level atomic in CPython (single GIL
            # hold), so they survive concurrent structural mutations
            # of the source. Iterating `.items()` instead would risk
            # `RuntimeError: dictionary changed size during iteration`
            # because peer connect/disconnect mutates these outer
            # dicts under `_advert_locks` / `_adverts_struct_lock`
            # which the TUI thread does NOT hold.
            raw_inbound = getattr(nm, "_inbound_adverts", None) or {}
            raw_outbound = getattr(nm, "_outbound_adverts", None) or {}
            raw_inflight = getattr(nm, "_inflight_publishes", None) or {}
            raw_peer_stats = getattr(nm, "peer_stats", None) or {}
            raw_pools = getattr(nm, "connection_pools", None) or {}
            inbound_outer = raw_inbound.copy()
            outbound_outer = raw_outbound.copy()
            inflight_outer = raw_inflight.copy()
            peer_stats = raw_peer_stats.copy()
            connection_pools = raw_pools.copy()
            # Inner copies — also C-atomic, but if the inner dict
            # mutates during the outer iteration of our snapshot we'd
            # still hit RuntimeError. Bail to next tick on race.
            inbound = {h: d.copy() for h, d in inbound_outer.items()}
            outbound = {h: d.copy() for h, d in outbound_outer.items()}
            inflight = {h: set(s) for h, s in inflight_outer.items()}
            liveness_timeout = getattr(nm, "liveness_timeout", 30)
        except (RuntimeError, Exception):
            return

        nodes_by_host = {n.hostname: n for n in nodes_list}

        try:
            table = self.query_one("#net-peers-table", DataTable)
        except NoMatches:
            return
        table.clear()

        now = time.time()
        hb_interval = getattr(nm, "heartbeat_interval", 10)
        for peer in peers:
            node = nodes_by_host.get(peer.hostname)
            # Phase 2 — alive column is a colored Text cell whose state
            # is derived directly from `now - last_heartbeat`, not from
            # `is_alive_sync` (which is a single-threshold check).
            #   - alive    (#73c991 green)   = fresh within heartbeat_interval
            #   - degraded (#cca75a yellow)  = between hb_interval and liveness_timeout
            #   - down     (#d16969 red)     = beyond liveness_timeout
            #   - never    (dim)             = no heartbeat ever recorded
            alive_cell: Text
            hb_str = "never"
            if node is None:
                alive_cell = Text("never", style="dim")
            else:
                last_hb = getattr(node, "last_heartbeat", None)
                if last_hb is None:
                    alive_cell = Text("never", style="dim")
                else:
                    age = now - last_hb
                    hb_str = self._format_relative_hb(age)
                    if age < hb_interval:
                        alive_cell = Text("alive", style="#73c991")
                    elif age < liveness_timeout:
                        alive_cell = Text("degraded", style="#cca75a")
                    else:
                        alive_cell = Text("down", style="#d16969")

            pool = connection_pools.get((peer.ip, peer.port))
            try:
                # qsize() is GIL-atomic in CPython (returns len(deque));
                # cross-thread approximate but safe for display.
                pool_str = str(pool.qsize()) if pool is not None else "0"
            except Exception:
                pool_str = "?"

            in_subs = len(inbound.get(peer.hostname, {}))
            out_subs = len(outbound.get(peer.hostname, {}))
            inflight_count = len(inflight.get(peer.hostname, set()))
            stats = peer_stats.get(peer.hostname, {}) or {}
            bytes_str = (
                f"{stats.get('bytes_sent', 0)}/"
                f"{stats.get('bytes_recv', 0)}"
            )
            sysc = "Y" if getattr(peer, "system_caller", False) else "N"
            fp_short = (peer.fingerprint or "")[:12] + (
                "…" if peer.fingerprint and len(peer.fingerprint) > 12 else ""
            )

            table.add_row(
                peer.hostname,
                f"{peer.ip}:{peer.port}",
                sysc,
                alive_cell,
                hb_str,
                pool_str,
                str(in_subs),
                str(out_subs),
                str(inflight_count),
                bytes_str,
                fp_short,
                key=peer.hostname,
            )

    @staticmethod
    def _format_relative_hb(secs: float) -> str:
        if secs < 0:
            return "?"
        if secs < 60:
            return f"{int(secs)}s ago"
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        return f"{int(secs // 3600)}h ago"

    def _format_net_stat_card(self) -> str:
        """Render the Home tab 'Net' stat card text.

        Three states:
          - networking disabled in config: "OFF"
          - enabled but NetworkManager not yet built / mid-rebuild: "ON (N/A)"
          - enabled with live NM: "ON, X/Y peers alive" where X = configured
            peers (PeerSpec) whose matching Node entry is enabled AND
            heartbeated within `heartbeat_interval` (the "alive" band),
            Y = len(nm.peers)

        Uses the same heartbeat-age semantics as the Networking-tab
        cluster summary so a peer in the "degraded" band doesn't read
        as alive on Home but degraded on Networking. Configured-but-
        never-heartbeat peers count toward (Y - X). Auto-discovered
        peers not in `nm.peers` are excluded entirely.
        """
        pc = self.plugin_core
        if not getattr(pc, "networking_enabled", False):
            return "OFF"
        nm = getattr(pc, "network", None)
        if nm is None:
            return "ON (N/A)"
        try:
            peers = list(getattr(nm, "peers", []) or [])
            nodes = list(getattr(nm, "nodes", []) or [])
            hb_interval = getattr(nm, "heartbeat_interval", 10)
        except Exception:
            return "ON (N/A)"
        configured_hostnames = {p.hostname for p in peers}
        nodes_by_host = {n.hostname: n for n in nodes
                         if n.hostname in configured_hostnames}
        now = time.time()
        alive = 0
        # Aligned with the peers-table alive-cell logic (which doesn't
        # check `node.enabled`) — see `_format_cluster_summary` for the
        # rationale.
        for host in configured_hostnames:
            node = nodes_by_host.get(host)
            if node is None:
                continue
            last_hb = getattr(node, "last_heartbeat", None)
            if last_hb is None:
                continue
            if now - last_hb < hb_interval:
                alive += 1
        return f"ON, {alive}/{len(peers)} peers alive"

    def on_peer_event_bus(self, topic: str, payload: dict) -> None:
        """Bridge target for the TUI plugin's `_on_peer_event` callback.

        Called via `app.call_from_thread` → runs on the TUI loop.

        Phase 1: triggers a peers-table refresh.
        Phase 2: writes a colored line to the network event log (RichLog
            with `markup=True`), color-coded by topic + disconnect reason.
        Phase 4 (precursor): if a drill-down tab exists for the hostname
            and shows the `(gone)` suffix from a prior disconnect, restore
            it on reconnect.
        """
        try:
            self._refresh_peers_table_worker()
        except Exception:
            pass

        # Phase 2 — main event log line. Phase 4b — also stream to the
        # per-peer log if a drill-down for this host is open AND the
        # event was appended to the plugin deque AFTER the tab's mount
        # baseline (otherwise the same event was already rendered via
        # `_hydrate_peer_log`, and a naive append would double-render).
        line = self._format_peer_event_line(topic, payload)
        if line is not None:
            # Main log first.
            try:
                self.query_one("#net-event-log", RichLog).write(line)
            except NoMatches:
                pass
            # Per-peer log second — baseline-gated dedup. Baseline is a
            # snapshot of the plugin's monotonic `_event_seq` taken at
            # tab-mount time. We gate on `current_seq > baseline_seq` so
            # the dedup keeps working after the bounded deque saturates
            # (where the old `len()`-based check would silently lock the
            # log permanently once `len()` plateaued at maxlen).
            host_for_log = payload.get("hostname")
            if (
                host_for_log
                and host_for_log in self._peer_tabs
                and host_for_log in self._peer_log_baselines
            ):
                baseline = self._peer_log_baselines[host_for_log]
                plugin = self.plugin_instance
                current_seq = baseline  # forces the skip on plugin error
                try:
                    if plugin is not None and hasattr(plugin, "get_event_seq"):
                        candidate = plugin.get_event_seq()
                        if isinstance(candidate, int):
                            current_seq = candidate
                        else:
                            # MagicMock or other non-int: fall back to len.
                            current_seq = len(plugin._recent_peer_events)
                    else:
                        current_seq = len(plugin._recent_peer_events)
                except Exception:
                    current_seq = baseline
                if (
                    isinstance(current_seq, int)
                    and isinstance(baseline, int)
                    and current_seq > baseline
                ):
                    tab_id = self._peer_tabs[host_for_log]
                    try:
                        self.query_one(f"#{tab_id}-eventlog",
                                       RichLog).write(line)
                    except NoMatches:
                        pass

        # Phase 4a — drill-down tab title flips:
        #   * disconnected → `<host> (gone)`: surfaces the disconnect to
        #     a user staring at an open drill-down without auto-closing
        #     the tab (operator may want to study the last state).
        #   * connected → `<host>`: restores the title if the same peer
        #     reconnects, so a flap doesn't leave a stale "(gone)" label.
        host = payload.get("hostname")
        if host and host in self._peer_tabs:
            try:
                tab_id = self._peer_tabs[host]
                tabbed = self.query_one("#main-tabs", TabbedContent)
                tab = tabbed.get_tab(tab_id)
                if topic == "_core/peer/connected":
                    tab.label = host
                elif topic == "_core/peer/disconnected":
                    tab.label = f"{host} (gone)"
            except Exception:
                pass

    @on(Button.Pressed, "#btn-net-thisnode-view-cert")
    def _on_view_thisnode_cert(self) -> None:
        """Open the cert modal for the own (local-node) cert."""
        self._open_cert_modal(title="Local node certificate")

    @on(Button.Pressed, "#btn-net-bootstrap-view-cert")
    def _on_view_bootstrap_cert(self) -> None:
        """Open the cert modal from the bootstrap card (same own cert).

        Bootstrap PEM is identical to the own cert PEM — the card only
        exists when `peers=[]` and the operator needs to share their
        fingerprint + PEM with other nodes.
        """
        self._open_cert_modal(title="Bootstrap — local certificate")

    def _open_cert_modal(self, *, title: str) -> None:
        """Resolve own PEM + fingerprint and push the modal screen.

        Defensive: handles `pc.network is None` (pre-NM / mid-rebuild)
        with placeholder text instead of letting `_read_cert_pem_safe`
        crash on a missing `cert_path` attribute. Double-push guarded
        so a rapid double-click on a `View cert` button cannot stack
        two modals.
        """
        # Double-push guard — a modal is already up; do nothing.
        if any(isinstance(s, CertPEMScreen) for s in self.screen_stack):
            return
        nm = getattr(self.plugin_core, "network", None)
        if nm is None:
            self.push_screen(CertPEMScreen(
                title=title,
                pem_text="(NetworkManager not built — cert unavailable)",
                fingerprint="(NM not built)",
            ))
            return
        pem = self._read_cert_pem_safe(nm)
        fp = (getattr(nm, "own_fingerprint", "") or "(not loaded yet)")
        self.push_screen(CertPEMScreen(
            title=title, pem_text=pem, fingerprint=fp,
        ))

    @on(Button.Pressed, "#btn-net-clear-counters")
    def _on_clear_counters(self) -> None:
        """Reset disconnect-reason counters via plugin-side state.

        H-B (cycle 4) — guard against plugin teardown mid-press: a
        concurrent `on_disable` could pop attributes between the button
        press dispatch and this handler. The `getattr` check covers that.
        """
        plugin = self.plugin_instance
        if plugin is None or getattr(plugin, "_observer_lock", None) is None:
            return
        try:
            plugin.clear_disconnect_reason_counts()
        except Exception:
            # Broad catch — teardown races may raise more than just
            # AttributeError (lock contention, RuntimeError if the
            # plugin's loop was just stopped). The button is one-shot
            # and idempotent on success, so silently dropping on
            # error is safer than letting it crash out of a Textual
            # event handler.
            return
        # Trigger immediate counter refresh.
        try:
            self._refresh_peers_table_worker()
        except Exception:
            pass

    # ─── Phase 4a — per-peer drill-down tab ──────────────────────────

    @on(DataTable.RowSelected, "#net-peers-table")
    async def _on_peers_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the per-peer drill-down on Enter / row-activate."""
        if event.row_key is None:
            return
        host = str(event.row_key.value)
        await self._open_peer_drill_down(host)

    async def _open_peer_drill_down(self, hostname: str) -> None:
        """Spawn (or focus) a per-peer drill-down TabPane.

        Phase 4a layout: identity strip + 4 throughput sparklines + Close
        button. Phase 4b will append subs tables + in-flight panel +
        filtered event log; Phase 4c adds quick-action buttons.

        - Gated on `pc.networking_enabled` (no-op when off).
        - Cap at `_peer_tabs_cap` (5) open tabs: FIFO eviction of oldest.
        - Single app-level 1s timer drives all refreshes; created lazily
          on first open, stopped on last close.
        """
        if not getattr(self.plugin_core, "networking_enabled", False):
            return
        # Already open — just switch to it.
        if hostname in self._peer_tabs:
            try:
                self.query_one("#main-tabs", TabbedContent).active = (
                    self._peer_tabs[hostname]
                )
            except NoMatches:
                pass
            return

        # Cap enforcement — FIFO evict the oldest before opening.
        # `_close_peer_drilldown` keeps the dict entry intact when
        # `remove_pane` fails (non-NoMatches), so re-check the cap
        # after the await: if eviction silently bailed, refuse to
        # open the new tab — proceeding would orphan the failed
        # remove's pane AND push len(_peer_tabs) past the cap.
        if len(self._peer_tabs) >= self._peer_tabs_cap:
            oldest_host = next(iter(self._peer_tabs))
            await self._close_peer_drilldown(oldest_host)
            if len(self._peer_tabs) >= self._peer_tabs_cap:
                self.log.warning(
                    "Peer drill-down cap reached and eviction failed; "
                    "refusing to open drill-down for %s", hostname,
                )
                return

        tab_id = f"tab-peer-{self._sanitize_id(hostname)}"
        try:
            tabs = self.query_one("#main-tabs", TabbedContent)
        except NoMatches:
            return

        pane = TabPane(hostname, id=tab_id)
        await tabs.add_pane(pane)

        try:
            self.query_one(f"#{tab_id}", TabPane)
        except NoMatches:
            return

        # Build pane body via Phase 4a helper; appended via mount().
        scroll = VerticalScroll(classes="peer-tab-body",
                                id=f"{tab_id}-scroll")
        await pane.mount(scroll)
        for w in self._build_peer_drill_widgets(hostname, tab_id):
            await scroll.mount(w)

        self._peer_tabs[hostname] = tab_id
        # Initialise ring buffer for this peer.
        self._peer_ring_buffers[hostname] = {
            "bytes_sent_delta": collections.deque(maxlen=60),
            "bytes_recv_delta": collections.deque(maxlen=60),
            "msgs_sent_delta":  collections.deque(maxlen=60),
            "msgs_recv_delta":  collections.deque(maxlen=60),
            "last_sample": None,
            "last_sample_nm_id": None,
        }
        # Start the shared 1s timer on first drill-down open.
        if self._peer_drill_timer is None:
            self._peer_drill_timer = self.set_interval(
                1.0, self._refresh_peer_drilldowns,
            )

        tabs.active = tab_id

    def _build_peer_drill_widgets(self, hostname: str, tab_id: str) -> list:
        """Construct the per-peer drill-down body containers (Phase 4a+4b).

        Phase 4a content: identity strip + throughput sparklines + actions
        row. Phase 4b adds: inbound + outbound subs DataTables, in-flight
        publishes count Static, and a per-peer filtered event log.

        All widgets get IDs scoped under the tab_id prefix so multiple
        open drill-downs cannot collide on shared widget IDs.
        """
        widgets: list = []
        widgets.append(
            Static(f"Peer: {hostname}", classes="net-card-title")
        )

        # Identity strip — 7 label/value rows (populated post-mount).
        identity = Vertical(id=f"{tab_id}-identity",
                            classes="peer-identity-box")
        widgets.append(identity)

        # Throughput sparklines — 4 panels in a Horizontal grid.
        sparks = Horizontal(id=f"{tab_id}-sparklines",
                            classes="peer-sparkline-row")
        widgets.append(sparks)

        # Phase 4b — inbound + outbound subs DataTables side by side.
        subs_row = Horizontal(id=f"{tab_id}-subs-row",
                              classes="peer-subs-row")
        widgets.append(subs_row)

        # Phase 4b — In-flight publishes panel (just a count, not task names).
        widgets.append(Static("In-flight publishes: 0",
                              id=f"{tab_id}-inflight",
                              classes="peer-inflight"))

        # Phase 4b — per-peer event log strip. `markup=True` so we can
        # color-code by topic/reason like the main event log.
        widgets.append(RichLog(id=f"{tab_id}-eventlog",
                               max_lines=50, markup=True,
                               classes="peer-eventlog"))

        # View cert + close buttons row.
        actions = Horizontal(id=f"{tab_id}-actions",
                             classes="peer-actions-row")
        widgets.append(actions)

        # Schedule child mounting on the next tick — `compose` returns
        # the top-level containers; their internals get filled by the
        # post-mount initialiser. This lets `add_pane`'s mount cycle
        # complete before we add nested widgets.
        self.call_after_refresh(
            self._populate_peer_drill_widgets,
            hostname, tab_id,
        )
        return widgets

    def _populate_peer_drill_widgets(self, hostname: str, tab_id: str) -> None:
        """Mount the identity rows + sparkline panels + subs tables +
        in-flight panel + per-peer log + action buttons into their
        already-mounted parent containers. Runs once on tab spawn via
        `call_after_refresh`.

        Idempotent: re-entering after a successful populate is a no-op
        (every mount call would otherwise hit `DuplicateIds`). Detect
        via the identity-host row presence — that's the first child
        mounted by populate.

        Baseline + hydration are deferred to the END of the success
        path so `_peer_log_baselines[hostname]` becomes visible only
        AFTER hydration has finished. Test code (and `on_peer_event_bus`'s
        baseline-dedup gate) can therefore treat that key as "the per-peer
        log is hydrated + ready for live appends". If the parent-container
        query bails and we retry, baseline stays unset until the retry
        succeeds — bus events during the in-flight window simply route
        through the main log only, not the per-peer log.
        """
        # Idempotency check — if the identity-host row already exists,
        # populate already ran for this tab. Avoid DuplicateIds on a
        # second invocation. Also clear the retry counter on this exit
        # path for symmetry with the success path (housekeeping only —
        # the counter is also cleaned in `_close_peer_drilldown`).
        try:
            self.query_one(f"#{tab_id}-identity-host", Static)
            self._peer_drill_populate_attempts.pop(tab_id, None)
            return
        except NoMatches:
            pass

        try:
            identity = self.query_one(f"#{tab_id}-identity", Vertical)
            sparks = self.query_one(f"#{tab_id}-sparklines", Horizontal)
            subs_row = self.query_one(f"#{tab_id}-subs-row", Horizontal)
            actions = self.query_one(f"#{tab_id}-actions", Horizontal)
        except NoMatches:
            # Parent containers not queryable yet — retry on next
            # refresh. Cap the retry count via a per-tab counter so a
            # genuinely dead tab can't spin forever.
            attempts = self._peer_drill_populate_attempts.get(tab_id, 0)
            if attempts < 5:
                self._peer_drill_populate_attempts[tab_id] = attempts + 1
                self.call_after_refresh(
                    self._populate_peer_drill_widgets, hostname, tab_id,
                )
            return
        self._peer_drill_populate_attempts.pop(tab_id, None)

        # Identity rows.
        for label, sub_id in (
            ("Host:", "host"),
            ("Address:", "addr"),
            ("Alive:", "alive"),
            ("Last HB:", "hb"),
            ("Pool:", "pool"),
            ("system_caller:", "sysc"),
            ("Fingerprint:", "fp"),
        ):
            row = Horizontal(classes="net-row")
            identity.mount(row)
            row.mount(Static(label, classes="net-row-label"))
            row.mount(Static(
                "...",
                id=f"{tab_id}-identity-{sub_id}",
                classes="net-row-value",
            ))

        # Sparkline panels — 4 side-by-side boxes, each with a title
        # Static label + the Sparkline itself. The 60-sample rolling
        # delta is fed in via `_refresh_one_peer_drilldown`.
        for label, sub_id in (
            ("Bytes sent / s", "bsent"),
            ("Bytes recv / s", "brecv"),
            ("Msgs sent / s", "msent"),
            ("Msgs recv / s", "mrecv"),
        ):
            box = Vertical(classes="peer-sparkline-box")
            sparks.mount(box)
            box.mount(Static(label, classes="peer-sparkline-title"))
            box.mount(Sparkline([], id=f"{tab_id}-spark-{sub_id}"))

        # Phase 4b — inbound + outbound subs DataTables.
        inbound_tbl = DataTable(id=f"{tab_id}-subs-in",
                                cursor_type="none")
        outbound_tbl = DataTable(id=f"{tab_id}-subs-out",
                                 cursor_type="none")
        subs_row.mount(inbound_tbl)
        subs_row.mount(outbound_tbl)
        inbound_tbl.add_columns("Topic (in)", "Hosts", "Authors", "Sub UUID")
        outbound_tbl.add_columns(
            "Topic (out)", "State", "Sent ago", "Acked ago", "Retries",
        )

        # Phase 4a actions: View cert.
        actions.mount(Button("View cert",
                             id=f"{tab_id}-btn-view-cert",
                             classes="peer-action-btn"))
        # Phase 4c quick actions.
        actions.mount(Button("Copy fingerprint",
                             id=f"{tab_id}-btn-copy-fp",
                             classes="peer-action-btn"))
        actions.mount(Button("Copy PEM",
                             id=f"{tab_id}-btn-copy-pem",
                             classes="peer-action-btn"))
        actions.mount(Button("Jump to config",
                             id=f"{tab_id}-btn-jump-config",
                             classes="peer-action-btn"))
        actions.mount(Button("Close peer tab",
                             id=f"{tab_id}-btn-close",
                             classes="peer-action-btn",
                             variant="error"))
        # Phase 4c — status feedback Static (cleared after 2s via timer).
        actions.mount(Static("",
                             id=f"{tab_id}-action-status",
                             classes="peer-action-status"))

        # Snapshot deque + seq, hydrate, then publish the baseline.
        # The baseline write is the LAST observable side-effect — once
        # `_peer_log_baselines[hostname]` exists, the per-peer log is
        # both rendered (hydration done) and ready to accept live
        # appends from `on_peer_event_bus`. Seq is the monotonic event
        # counter (not `len()`) so the dedup gate keeps working after
        # the bounded deque saturates.
        plugin = self.plugin_instance
        history: list = []
        if plugin is not None and hasattr(plugin, "get_recent_peer_events"):
            try:
                history = plugin.get_recent_peer_events()
            except Exception:
                history = []
        baseline_seq = len(history)
        if plugin is not None and hasattr(plugin, "get_event_seq"):
            try:
                candidate = plugin.get_event_seq()
                if isinstance(candidate, int):
                    baseline_seq = candidate
            except Exception:
                pass
        if hostname not in self._peer_log_hydrated:
            try:
                self._hydrate_peer_log(tab_id, history, hostname)
            except Exception:
                pass
            self._peer_log_hydrated.add(hostname)
        self._peer_log_baselines[hostname] = baseline_seq

        # Kick a synchronous render so the operator sees data on first
        # paint instead of waiting up to 1s for the shared timer.
        pc = self.plugin_core
        nm = getattr(pc, "network", None)
        if nm is not None:
            try:
                self._refresh_one_peer_drilldown(
                    hostname, nm, id(nm), time.time(),
                )
            except Exception:
                pass

    def _hydrate_peer_log(self, tab_id: str, history: list, hostname: str) -> None:
        """Render the pre-mount filtered-event-history into the per-peer
        log. Called once at tab mount."""
        try:
            log = self.query_one(f"#{tab_id}-eventlog", RichLog)
        except NoMatches:
            return
        for topic, payload in history:
            if payload.get("hostname") != hostname:
                continue
            line = self._format_peer_event_line(topic, payload)
            if line is not None:
                log.write(line)

    @staticmethod
    def _format_peer_event_line(topic: str, payload: dict):
        """Return a markup-coloured log line for a peer event, or None if
        the event isn't a peer-lifecycle event. Shared by the per-peer
        log hydration + live append path."""
        ts = time.strftime("%H:%M:%SZ",
                           time.gmtime(payload.get("ts", time.time())))
        host = payload.get("hostname", "?")
        if topic == "_core/peer/connected":
            ip = payload.get("ip", "?")
            return f"[#73c991]{ts}  CONNECT     {host} from {ip}[/]"
        if topic == "_core/peer/disconnected":
            reason = payload.get("reason", "normal")
            if reason == "normal":
                return f"[dim]{ts}  disconnect  {host} reason=normal[/]"
            if reason == "connection_error":
                return (f"[#cca75a]{ts}  DISCONNECT  {host} "
                        f"reason=connection_error[/]")
            if reason == "rce_attempt":
                return f"[#d16969 bold]{ts}  RCE_ATTEMPT {host}[/]"
            return f"[#d16969]{ts}  DISCONNECT  {host} reason={reason}[/]"
        return None

    async def _close_peer_drilldown(self, hostname: str) -> None:
        """Pop the drill-down pane + ring buffer for `hostname`.

        Also stops the shared 1s refresh timer when no drill-down tabs
        remain so an idle TUI does not tick uselessly.

        Pane removal runs BEFORE the dict pops so a failed `remove_pane`
        does not orphan the DOM entry: if remove fails, the entry stays
        in `_peer_tabs` and a subsequent `_close_peer_drilldown` retry
        can reattempt the remove. The dict pops only happen after a
        successful remove.
        """
        tab_id = self._peer_tabs.get(hostname)
        if tab_id is None:
            return
        try:
            tabs = self.query_one("#main-tabs", TabbedContent)
            await tabs.remove_pane(tab_id)
        except NoMatches:
            # TabbedContent itself is gone (TUI tearing down) — treat
            # the pane as already removed.
            pass
        except Exception:
            # Other remove_pane failure: keep dict entries so a retry
            # can clean up later. Log + bail.
            self.log.debug(
                "remove_pane failed for %s", hostname, exc_info=True,
            )
            return
        tab_id = self._peer_tabs.pop(hostname, None)
        self._peer_ring_buffers.pop(hostname, None)
        # Phase 4b — drop the per-peer log baseline + hydration flag so
        # a future re-open of the same host rehydrates cleanly from a
        # fresh deque slice.
        self._peer_log_baselines.pop(hostname, None)
        self._peer_log_hydrated.discard(hostname)
        if tab_id is not None:
            self._peer_drill_populate_attempts.pop(tab_id, None)
        if not self._peer_tabs and self._peer_drill_timer is not None:
            try:
                self._peer_drill_timer.stop()
            except Exception:
                pass
            self._peer_drill_timer = None

    async def _refresh_peer_drilldowns(self) -> None:
        """1s tick across all open drill-down tabs.

        Survives one-tick-after-shutdown via `is_running` guard. When
        networking is mid-rebuild (`pc.network is None`) or fully
        disabled, every open drill-down tab is closed — there is no
        useful data left to render.
        """
        if not self.is_running:
            return
        if not self._peer_tabs:
            return
        pc = self.plugin_core
        nm = pc.network  # snapshot ONCE per tick (avoid mid-tick rebuild race)
        if not getattr(pc, "networking_enabled", False) or nm is None:
            for host in list(self._peer_tabs.keys()):
                await self._close_peer_drilldown(host)
            return
        nm_id = id(nm)
        now = time.time()
        for host in list(self._peer_tabs.keys()):
            try:
                self._refresh_one_peer_drilldown(host, nm, nm_id, now)
            except NoMatches:
                # Tab DOM torn down between check and update — skip.
                continue
            except Exception:
                self.log.debug(
                    "drill-down refresh failed for %s", host, exc_info=True,
                )

    def _refresh_one_peer_drilldown(self, host: str, nm, nm_id: int,
                                     now: float) -> None:
        """Identity + sparkline update for one peer's drill-down."""
        tab_id = self._peer_tabs.get(host)
        if tab_id is None:
            return

        # Snapshot peer state — outer-then-inner dict-copy + bail-on-race
        # matches the established peers-table worker convention.
        try:
            peers = list(getattr(nm, "peers", []) or [])
            nodes_list = list(getattr(nm, "nodes", []) or [])
            stats_snapshot = dict(getattr(nm, "peer_stats", None) or {})
            stats = (stats_snapshot.get(host) or {}).copy()
            pool_map = dict(getattr(nm, "connection_pools", None) or {})
            hb_interval = getattr(nm, "heartbeat_interval", 10)
            liveness = getattr(nm, "liveness_timeout", 30)
        except (RuntimeError, Exception):
            return

        peer = next((p for p in peers if p.hostname == host), None)
        node = next((n for n in nodes_list if n.hostname == host), None)

        # ── Identity rows ──────────────────────────────────────────
        ip_port = (f"{peer.ip}:{peer.port}" if peer is not None
                   else "(not in peers config)")
        sysc = "Y" if (peer and getattr(peer, "system_caller", False)) else "N"
        fp = getattr(peer, "fingerprint", "") if peer else ""
        fp_short = (fp[:24] + "…") if len(fp) > 24 else (fp or "(none)")

        # Alive cell mirrors the peers-table semantics exactly.
        if node is None:
            alive_text = "never"
            hb_str = "never"
        else:
            last_hb = getattr(node, "last_heartbeat", None)
            if last_hb is None:
                alive_text = "never"
                hb_str = "never"
            else:
                age = now - last_hb
                hb_str = self._format_relative_hb(age)
                if age < hb_interval:
                    alive_text = "alive"
                elif age < liveness:
                    alive_text = "degraded"
                else:
                    alive_text = "down"

        pool = pool_map.get((getattr(peer, "ip", None),
                             getattr(peer, "port", None)))
        try:
            pool_str = (f"{pool.qsize()}/{getattr(nm, 'pool_size', '?')}"
                        if pool is not None
                        else f"0/{getattr(nm, 'pool_size', '?')}")
        except Exception:
            pool_str = "?"

        self._set_row(f"#{tab_id}-identity-host", host)
        self._set_row(f"#{tab_id}-identity-addr", ip_port)
        self._set_row(f"#{tab_id}-identity-alive", alive_text)
        self._set_row(f"#{tab_id}-identity-hb", hb_str)
        self._set_row(f"#{tab_id}-identity-pool", pool_str)
        self._set_row(f"#{tab_id}-identity-sysc", sysc)
        self._set_row(f"#{tab_id}-identity-fp", fp_short)

        # ── Sparkline updates ─────────────────────────────────────
        rb = self._peer_ring_buffers.get(host)
        if rb is None:
            return
        # S-1: NM rebuild invalidates cumulative-counter delta math.
        # Reset last_sample on instance swap.
        if rb["last_sample_nm_id"] != nm_id:
            rb["last_sample"] = None
            rb["last_sample_nm_id"] = nm_id

        last = rb["last_sample"]
        if last is None or not stats:
            # First tick on this NM (or peer has no stats yet): push 0s
            # so the sparkline has data but doesn't lie about throughput.
            for k in ("bytes_sent_delta", "bytes_recv_delta",
                      "msgs_sent_delta", "msgs_recv_delta"):
                rb[k].append(0.0)
            if stats:
                rb["last_sample"] = stats
        else:
            for raw, k in (
                ("bytes_sent", "bytes_sent_delta"),
                ("bytes_recv", "bytes_recv_delta"),
                ("msgs_sent",  "msgs_sent_delta"),
                ("msgs_recv",  "msgs_recv_delta"),
            ):
                delta = max(0, stats.get(raw, 0) - last.get(raw, 0))
                rb[k].append(float(delta))
            rb["last_sample"] = stats

        for sub_id, deque_key in (
            ("bsent", "bytes_sent_delta"),
            ("brecv", "bytes_recv_delta"),
            ("msent", "msgs_sent_delta"),
            ("mrecv", "msgs_recv_delta"),
        ):
            try:
                self.query_one(f"#{tab_id}-spark-{sub_id}",
                               Sparkline).data = list(rb[deque_key])
            except NoMatches:
                continue

        # ── Phase 4b: subs tables + in-flight count ───────────────
        try:
            in_raw = dict(getattr(nm, "_inbound_adverts", None) or {})
            out_raw = dict(getattr(nm, "_outbound_adverts", None) or {})
            in_subs = dict(in_raw.get(host) or {})
            out_subs = dict(out_raw.get(host) or {})
            inflight_map = dict(
                getattr(nm, "_inflight_publishes", None) or {}
            )
            inflight_count = len(set(inflight_map.get(host) or set()))
        except (RuntimeError, Exception):
            in_subs, out_subs, inflight_count = {}, {}, 0

        # Inbound DataTable: rebuild on each tick (matches the main
        # peers-table convention — small data, simple semantics).
        try:
            in_tbl = self.query_one(f"#{tab_id}-subs-in", DataTable)
            in_tbl.clear()
            for sub_uuid, sub in in_subs.items():
                topic = getattr(sub, "topic_pattern", "?")
                hosts_v = getattr(sub, "hosts", None)
                authors_v = getattr(sub, "authors", None)
                in_tbl.add_row(
                    str(topic),
                    str(hosts_v) if hosts_v is not None else "(any)",
                    str(authors_v) if authors_v is not None else "(any)",
                    (sub_uuid[:12] + "…") if len(sub_uuid) > 12 else sub_uuid,
                )
        except NoMatches:
            pass

        # Outbound DataTable: shows ack lifecycle state derived from
        # sender-side AdvertSub fields (state / sent_at / acked_at /
        # retry_count) per networking.py:46-49.
        try:
            out_tbl = self.query_one(f"#{tab_id}-subs-out", DataTable)
            out_tbl.clear()
            for sub_uuid, sub in out_subs.items():
                topic = getattr(sub, "topic_pattern", "?")
                state = getattr(sub, "state", "pending")
                sent_at = getattr(sub, "sent_at", None)
                acked_at = getattr(sub, "acked_at", None)
                retry = getattr(sub, "retry_count", 0)
                sent_str = (self._format_relative_hb(now - sent_at)
                            if sent_at is not None else "—")
                acked_str = (self._format_relative_hb(now - acked_at)
                             if acked_at is not None else "—")
                out_tbl.add_row(
                    str(topic), state, sent_str, acked_str, str(retry),
                )
        except NoMatches:
            pass

        # In-flight publishes count (per N-3 nit + plan: count only).
        try:
            self.query_one(f"#{tab_id}-inflight", Static).update(
                f"In-flight publishes: {inflight_count}"
            )
        except NoMatches:
            pass

    @on(Button.Pressed)
    def _on_peer_drill_button_pressed(self, event: Button.Pressed) -> None:
        """Catch-all handler for drill-down per-peer buttons.

        Routes every `tab-peer-<sanitised>-btn-*` button to its handler.
        Each branch resolves the hostname via `_host_for_tab(tab_id)`.
        Phase 4a buttons: View cert, Close peer tab.
        Phase 4c buttons: Copy fingerprint, Copy PEM, Jump to config.
        """
        btn_id = event.button.id
        if not btn_id or not btn_id.startswith("tab-peer-"):
            return

        for suffix, action in (
            ("-btn-view-cert", "view_cert"),
            ("-btn-copy-fp", "copy_fp"),
            ("-btn-copy-pem", "copy_pem"),
            ("-btn-jump-config", "jump_config"),
            ("-btn-close", "close"),
        ):
            if not btn_id.endswith(suffix):
                continue
            tab_id = btn_id[: -len(suffix)]
            host = self._host_for_tab(tab_id)
            if host is None:
                return
            if action == "view_cert":
                self._open_peer_cert_modal(host)
            elif action == "copy_fp":
                self._peer_copy_fingerprint(host, tab_id)
            elif action == "copy_pem":
                self._peer_copy_pem(host, tab_id)
            elif action == "jump_config":
                self.run_worker(
                    self._peer_jump_to_config(host, tab_id),
                    name=f"peer-jump-config:{host}",
                    exclusive=False,
                    exit_on_error=False,
                )
            elif action == "close":
                self.run_worker(
                    self._close_peer_drilldown(host),
                    name=f"close-peer-tab:{host}",
                    exclusive=False,
                    exit_on_error=False,
                )
            return

    # ─── Phase 4c — Drill-down quick actions ─────────────────────────

    def _set_peer_action_status(self, tab_id: str, msg: str) -> None:
        """Update the per-tab status Static and clear it after 2s."""
        try:
            widget = self.query_one(f"#{tab_id}-action-status", Static)
        except NoMatches:
            return
        widget.update(msg)
        # Schedule a clear; safe to chain repeated calls — only the
        # latest timer's clear matters for visible state.
        self.set_timer(2.0, lambda: self._clear_peer_action_status(tab_id))

    def _clear_peer_action_status(self, tab_id: str) -> None:
        try:
            self.query_one(f"#{tab_id}-action-status", Static).update("")
        except NoMatches:
            pass

    def _peer_copy_fingerprint(self, host: str, tab_id: str) -> None:
        peer = self._lookup_peer(host)
        if peer is None:
            self._set_peer_action_status(tab_id, "Peer not found")
            return
        fp = getattr(peer, "fingerprint", "") or ""
        if not fp:
            self._set_peer_action_status(tab_id, "Fingerprint unavailable")
            return
        try:
            self.copy_to_clipboard(fp)
        except Exception:
            pass
        self._set_peer_action_status(tab_id, "Copied fingerprint")

    def _peer_copy_pem(self, host: str, tab_id: str) -> None:
        peer = self._lookup_peer(host)
        if peer is None:
            self._set_peer_action_status(tab_id, "Peer not found")
            return
        pem = getattr(peer, "cert_pem", "") or ""
        if not pem:
            self._set_peer_action_status(tab_id, "PEM unavailable")
            return
        try:
            self.copy_to_clipboard(pem)
        except Exception:
            pass
        self._set_peer_action_status(tab_id, "Copied PEM")

    def _lookup_peer(self, host: str):
        """Resolve a PeerSpec-like object from `nm.peers` by hostname."""
        nm = getattr(self.plugin_core, "network", None)
        if nm is None:
            return None
        for peer in getattr(nm, "peers", []) or []:
            if getattr(peer, "hostname", None) == host:
                return peer
        return None

    async def _peer_jump_to_config(self, host: str, tab_id: str) -> None:
        """Best-effort jump to the peer's entry in `config.yml`.

        Switches to the Config tab, selects `config.yml (main)`, loads
        the file, scans the text for the first `hostname: <peer>` line,
        and moves the cursor there (with `center=True` so the viewport
        scrolls). False-match on a commented `# hostname: peer-x` is
        acceptable v1.
        """
        try:
            tabs = self.query_one("#main-tabs", TabbedContent)
            tabs.active = "tab-config"
        except NoMatches:
            self._set_peer_action_status(tab_id, "Config tab missing")
            return

        # Set the Select to the main config and trigger load.
        try:
            sel = self.query_one("#config-select", Select)
            sel.value = "config.yml (main)"
        except NoMatches:
            pass

        # `_load_config_file` is a Textual @work coroutine; await its
        # completion via run_worker -> wait_for. We invoke the underlying
        # method directly to keep the flow synchronous-ish.
        try:
            label = "config.yml (main)"
            path = self._config_files.get(label)
            if path:
                # `read_text` is blocking; off-thread it so a slow disk
                # doesn't freeze Textual's message pump.
                content = await asyncio.to_thread(
                    Path(path).read_text, encoding="utf-8",
                )
                self.query_one("#config-editor", TextArea).load_text(content)
                self._current_config_file = path
                self._config_clean_hash = hashlib.md5(
                    content.encode()
                ).hexdigest()
            else:
                self._set_peer_action_status(tab_id, "Main config not found")
                return
        except Exception:
            self._set_peer_action_status(tab_id, "Config load failed")
            return

        # Scan + jump.
        try:
            ta = self.query_one("#config-editor", TextArea)
            # YAML peer entries are written as list items like
            # `- hostname: peer-foo`; the leading `-` is optional so we
            # also match a bare `hostname: peer-foo` form.
            pattern = re.compile(
                rf"^\s*-?\s*hostname:\s*['\"]?{re.escape(host)}['\"]?\s*$"
            )
            line_idx = None
            for idx, raw_line in enumerate(ta.text.splitlines()):
                if pattern.match(raw_line):
                    line_idx = idx
                    break
            if line_idx is not None:
                # center=True triggers scroll_cursor_visible so the
                # cursor jumps INTO view (per `_text_area.py:1925-1956`).
                ta.move_cursor((line_idx, 0), center=True)
                self._set_peer_action_status(
                    tab_id, f"Jumped to line {line_idx + 1}",
                )
            else:
                self._set_peer_action_status(
                    tab_id, f"hostname: {host} not found",
                )
        except NoMatches:
            self._set_peer_action_status(tab_id, "Config editor missing")

    def _host_for_tab(self, tab_id: str) -> Optional[str]:
        for host, tid in self._peer_tabs.items():
            if tid == tab_id:
                return host
        return None

    def _open_peer_cert_modal(self, host: str) -> None:
        """Push the CertPEMScreen with this peer's cert PEM + fingerprint.

        Shares the double-push guard with `_open_cert_modal`: at most ONE
        cert modal is on the stack at any time. If an own-cert modal is
        already open, the peer-cert button silently no-ops — user must
        close the existing modal first. Accepted UX trade-off: a noisy
        feedback toast adds plumbing for a vanishingly rare case.
        """
        if any(isinstance(s, CertPEMScreen) for s in self.screen_stack):
            return
        nm = getattr(self.plugin_core, "network", None)
        if nm is None:
            return
        peer = next((p for p in getattr(nm, "peers", []) or []
                     if p.hostname == host), None)
        if peer is None:
            return
        self.push_screen(CertPEMScreen(
            title=f"Peer certificate — {host}",
            pem_text=getattr(peer, "cert_pem", "") or "(no PEM)",
            fingerprint=getattr(peer, "fingerprint", "") or "(no fingerprint)",
        ))

    def _format_peers_display(self) -> str:
        """Render peers summary for the Settings-tab Networking group.

        Source priority:
          1. `pc.network.peers` (List[PeerSpec]) when NetworkManager exists.
          2. YAML `networking.peers` count when network is None (config
             carries entries but networking has not been started).
          3. "none" when neither produces entries.

        Output format: `count (host @ ip:port, host2 @ ip:port, ...)` capped
        at 4 entries; overflow elided as ` +N more`.
        """
        nm = getattr(self.plugin_core, "network", None)
        if nm is not None:
            peers = list(getattr(nm, "peers", []) or [])
            if not peers:
                return "none"
            entries = [
                f"{p.hostname} @ {p.ip}:{p.port}" for p in peers[:4]
            ]
            count = len(peers)
            extra = count - len(entries)
            tail = f" +{extra} more" if extra > 0 else ""
            return f"{count} ({', '.join(entries)}{tail})"
        # NM is None — read raw YAML so peers configured but-not-yet-built
        # still render. Defensive .get() against partial configs.
        net_cfg = (self.plugin_core.yaml_config or {}).get("networking", {}) or {}
        raw_peers = net_cfg.get("peers", []) or []
        if not raw_peers:
            return "none"
        entries = []
        for entry in raw_peers[:4]:
            if isinstance(entry, dict):
                host = entry.get("hostname", "?")
                ip = entry.get("ip", "?")
                port = entry.get("port", "?")
                entries.append(f"{host} @ {ip}:{port}")
            else:
                entries.append(str(entry))
        count = len(raw_peers)
        extra = count - len(entries)
        tail = f" +{extra} more" if extra > 0 else ""
        return f"{count} ({', '.join(entries)}{tail})"

    # ─── Plugin view generation ──────────────────────────────────────

    def _build_plugin_tab_content(self, plugin_name: str, plugin=None,
                                    force_mode: str = "auto",
                                    has_bar: bool = False) -> list:
        """Build tab content for a plugin.

        Args:
            plugin_name: Name of the plugin.
            plugin: Plugin instance (fetched from registry if None).
            force_mode: "auto" (normal priority chain), "custom" (only custom
                widget/menu), or "generated" (only auto-generated view).
            has_bar: True when a view-mode-bar exists above the scroll
                container (close button already in bar — skip duplicates).
        """
        _log = logging.getLogger("TUI.TabBuilder")
        if plugin is None:
            plugin = self.plugin_core.plugins.get(plugin_name)
        if not plugin:
            return [Static(f"Plugin '{escape(plugin_name)}' not found.")]

        if force_mode == "generated":
            _log.debug("[%s] force_mode=generated — skipping custom checks",
                       plugin_name)
            return self._auto_generate_plugin_view(plugin_name, plugin,
                                                   has_bar=has_bar)

        # Module-info based custom widget (Dashboard imports the TUI module)
        has_module_info = (hasattr(plugin, "get_tui_module_info")
                          and callable(plugin.get_tui_module_info))
        _log.debug("[%s] has get_tui_module_info: %s", plugin_name,
                   has_module_info)
        if has_module_info:
            try:
                from textual.widget import Widget as _Widget
                info = plugin.get_tui_module_info()
                _log.debug("[%s] get_tui_module_info() returned: %s",
                           plugin_name, info)
                if info and isinstance(info, dict):
                    w = self._load_tui_widget_from_module_info(plugin, info)
                    if w is not None and isinstance(w, _Widget):
                        return [w]
                    _log.warning("[%s] TUI module load returned None or "
                                "non-Widget — falling through", plugin_name)
                else:
                    _log.warning("[%s] get_tui_module_info() returned "
                                "non-dict — falling through", plugin_name)
            except Exception as e:
                _log.error("[%s] get_tui_module_info() raised: %s",
                           plugin_name, e, exc_info=True)
                return [Static(f"Error: {escape(str(e))}")]

        # Menu dict
        has_menu = (hasattr(plugin, "get_tui_menu")
                    and callable(plugin.get_tui_menu))
        _log.debug("[%s] has get_tui_menu: %s", plugin_name, has_menu)
        if has_menu:
            try:
                menu = plugin.get_tui_menu()
                if menu and isinstance(menu, dict):
                    return self._render_menu_dict(plugin_name, menu,
                                                  has_bar=has_bar)
                _log.warning("[%s] get_tui_menu() returned non-dict or empty",
                             plugin_name)
            except Exception as e:
                _log.error("[%s] get_tui_menu() raised: %s", plugin_name, e,
                           exc_info=True)
                return [Static(f"Error: {escape(str(e))}")]

        if force_mode == "custom":
            _log.warning("[%s] force_mode=custom but no custom view available",
                         plugin_name)
            return [Static("No custom view available for this plugin.")]

        # Auto-generate
        _log.debug("[%s] falling through to auto-generated view", plugin_name)
        return self._auto_generate_plugin_view(plugin_name, plugin)

    def _auto_generate_plugin_view(self, plugin_name: str, plugin,
                                    has_bar: bool = False) -> list:
        widgets = []

        # Header with close + config buttons (skip close when bar has one)
        config_id = self._make_id("cfg", plugin_name, "", "goto-config")
        if has_bar:
            btn_row = Horizontal(
                Button("Open Config", id=config_id, variant="primary"),
            )
        else:
            close_id = self._make_id("close", plugin_name, "", "close-tab")
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

        endpoints = getattr(plugin, "endpoints", {})
        if not endpoints:
            widgets.append(Static("[dim]No endpoints defined.[/dim]", markup=True))
            return widgets

        for ep_key, ep in endpoints.items():
            if not isinstance(ep, dict):
                continue
            access_name = ep_key
            internal_name = ep.get("internal_name", access_name)
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
                self._id_registry[call_id]["json_id"] = json_id
                self._id_registry[call_id]["result_id"] = result_id
                self._id_registry[call_id]["form_fields"] = form_field_ids_for_mode
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

    def _render_menu_dict(self, plugin_name: str, menu: dict,
                          has_bar: bool = False) -> list:
        widgets = []
        # Close button (skip when view-mode-bar already has one)
        if not has_bar:
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

    def _plugin_has_custom_view(self, plugin) -> bool:
        """Check whether a plugin provides a custom TUI view."""
        if hasattr(plugin, "get_tui_module_info") and callable(plugin.get_tui_module_info):
            return True
        if hasattr(plugin, "get_tui_menu") and callable(plugin.get_tui_menu):
            return True
        return False

    def _load_tui_widget_from_module_info(self, plugin, info: dict):
        """Import a TUI widget class from module_info and instantiate it.

        The import runs inside the Dashboard process where Textual is
        available, so plugins don't need Textual on their own import path.
        The module is cached in sys.modules after first load; subsequent
        calls reuse the cached module and only create a fresh widget.
        """
        _log = logging.getLogger("TUI.TabBuilder")
        tui_path = info.get("path", "")
        class_name = info.get("class_name", "")
        if not tui_path or not class_name:
            _log.error("get_tui_module_info() returned incomplete info: %s",
                       info)
            return None

        plugin_name = getattr(plugin, "plugin_name", "unknown")
        pkg_name = f"_tui_{plugin_name}"

        # Reuse cached module if already loaded
        mod = _sys.modules.get(pkg_name)
        if mod is not None:
            _log.debug("[%s] reusing cached TUI module %s", plugin_name,
                       pkg_name)
        else:
            # Register the tui/ directory as a package so internal relative
            # imports (from .css, from .sections, etc.) resolve correctly.
            init_path = os.path.join(tui_path, "__init__.py")
            if not os.path.isfile(init_path):
                _log.error("TUI package missing __init__.py: %s", init_path)
                return None

            import importlib.util
            spec = importlib.util.spec_from_file_location(
                pkg_name, init_path,
                submodule_search_locations=[tui_path],
            )
            if spec is None:
                _log.error("Could not create module spec for %s", init_path)
                return None

            mod = importlib.util.module_from_spec(spec)
            mod.__package__ = pkg_name
            _sys.modules[pkg_name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception as e:
                _log.error("Failed to load TUI module from %s: %s",
                           tui_path, e, exc_info=True)
                _sys.modules.pop(pkg_name, None)
                return None
            _log.debug("[%s] TUI module %s loaded successfully", plugin_name,
                       pkg_name)

        widget_cls = getattr(mod, class_name, None)
        if widget_cls is None:
            _log.error("Class %s not found in %s", class_name, pkg_name)
            return None

        try:
            return widget_cls(plugin)
        except Exception as e:
            _log.error("Failed to instantiate %s: %s", class_name, e,
                       exc_info=True)
            return None

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
        has_custom = self._plugin_has_custom_view(plugin_snapshot)

        # Default to custom view when available
        mode = "custom" if has_custom else "generated"
        content = self._build_plugin_tab_content(
            plugin_name, plugin_snapshot, force_mode=mode,
            has_bar=has_custom,
        )

        pane = TabPane(plugin_name, id=tab_id)
        await tabs.add_pane(pane)

        try:
            self.query_one(f"#{tab_id}", TabPane)
        except NoMatches:
            return

        # View-mode toggle bar (only when plugin has a custom view)
        if has_custom:
            bar = Horizontal(classes="view-mode-bar",
                             id=f"{tab_id}-view-bar")
            await pane.mount(bar)
            custom_cls = "active-mode" if mode == "custom" else "inactive-mode"
            gen_cls = "active-mode" if mode == "generated" else "inactive-mode"
            btn_custom_id = self._make_id(
                "vmode", plugin_name, "", "view-mode-custom")
            btn_gen_id = self._make_id(
                "vmode", plugin_name, "", "view-mode-generated")
            close_id = self._make_id(
                "close", plugin_name, "", "close-tab")
            await bar.mount(
                Button("Custom View", id=btn_custom_id,
                       classes=custom_cls),
                Button("Generated View", id=btn_gen_id,
                       classes=gen_cls),
                Static("", classes="view-bar-spacer"),
                Button("Close Tab", id=close_id,
                       variant="error", classes="close-tab-btn"),
            )

        scroll = VerticalScroll(classes="plugin-view-container",
                                id=f"{tab_id}-scroll")
        await pane.mount(scroll)
        for w in content:
            await scroll.mount(w)

        self._plugin_tab_map[tab_id] = plugin_name
        self._plugin_tab_modes[tab_id] = mode
        tabs.active = tab_id

    async def _close_plugin_tab(self, plugin_name: str) -> None:
        tab_id = f"tab-plugin-{self._sanitize_id(plugin_name)}"
        tabs = self.query_one("#main-tabs", TabbedContent)
        try:
            await tabs.remove_pane(tab_id)
        except Exception:
            pass
        self._cleanup_registry_for_plugin(plugin_name)
        self._cleanup_tui_module(plugin_name)
        self._plugin_tab_map.pop(tab_id, None)
        self._plugin_tab_modes.pop(tab_id, None)

    def _cleanup_tui_module(self, plugin_name: str) -> None:
        """Remove cached TUI package and submodules from sys.modules."""
        pkg_name = f"_tui_{plugin_name}"
        to_remove = [k for k in _sys.modules if k == pkg_name
                     or k.startswith(f"{pkg_name}.")]
        for key in to_remove:
            _sys.modules.pop(key, None)

    def _cleanup_stale_plugin_tabs(self) -> None:
        stale = []
        for tab_id, pname in list(self._plugin_tab_map.items()):
            try:
                self.query_one(f"#{tab_id}", TabPane)
            except NoMatches:
                self._cleanup_registry_for_plugin(pname)
                self._cleanup_tui_module(pname)
                stale.append(tab_id)
        for tid in stale:
            self._plugin_tab_map.pop(tid, None)
            self._plugin_tab_modes.pop(tid, None)

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
            await self._run_on_main(
                self.plugin_core.async_load_config_yaml(self.plugin_core.config_path)
            )
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
            elif t in ("view-mode-custom", "view-mode-generated"):
                self._switch_plugin_view_mode(entry["plugin"], t)
        except Exception:
            pass

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Contain crashes from custom plugin tab workers.

        If a worker owned by a widget inside a plugin tab fails, replace
        the tab content with an error message instead of killing the app.
        """
        if event.state != WorkerState.ERROR:
            return

        worker = event.worker
        node = getattr(worker, "node", None)
        if node is None:
            return

        # Walk up from the crashed widget to see if it lives inside a
        # plugin tab pane.
        current = node
        tab_id = None
        while current is not None:
            wid = getattr(current, "id", None) or ""
            if wid.startswith("tab-plugin-"):
                tab_id = wid
                break
            current = getattr(current, "parent", None)

        if tab_id is None:
            return  # not a plugin tab worker — let Textual handle it

        # Swallow the error so the app stays alive
        event.prevent_default()

        plugin_name = self._plugin_tab_map.get(tab_id, "unknown")
        error = getattr(worker, "error", None)
        error_msg = str(error) if error else "Unknown error"
        worker_name = getattr(worker, "name", "?")
        logging.getLogger("TUI.TabBuilder").error(
            "[%s] Worker '%s' crashed: %s", plugin_name, worker_name,
            error_msg, exc_info=error,
        )

        # Replace tab content with error message
        self._show_tab_error(tab_id, plugin_name, worker_name, error_msg)

    @work(thread=False)
    async def _show_tab_error(self, tab_id: str, plugin_name: str,
                              worker_name: str, error_msg: str) -> None:
        scroll_id = f"{tab_id}-scroll"
        try:
            scroll = self.query_one(f"#{scroll_id}", VerticalScroll)
        except NoMatches:
            return
        await scroll.remove_children()

        # Only add close button if no view-mode-bar (which already has one)
        bar_id = f"{tab_id}-view-bar"
        has_bar = False
        try:
            self.query_one(f"#{bar_id}", Horizontal)
            has_bar = True
        except NoMatches:
            pass

        if not has_bar:
            close_id = self._make_id("close", plugin_name, "", "close-tab")
            await scroll.mount(
                Horizontal(
                    Button("Close Tab", id=close_id, variant="error"),
                ),
            )
        await scroll.mount(
            Static(
                f"[bold red]Custom view crashed[/bold red]\n\n"
                f"Plugin: [bold]{escape(plugin_name)}[/bold]\n"
                f"Worker: {escape(worker_name)}\n"
                f"Error: {escape(error_msg)}\n\n"
                f"[dim]Switch to Generated View or close this tab.[/dim]",
                markup=True,
            ),
        )

    def _close_plugin_tab_sync(self, plugin_name: str) -> None:
        """Non-async wrapper to close a plugin tab from a button handler."""
        self._do_close_plugin_tab(plugin_name)

    @work(thread=False)
    async def _do_close_plugin_tab(self, plugin_name: str) -> None:
        await self._close_plugin_tab(plugin_name)

    def _switch_plugin_view_mode(self, plugin_name: str, mode_type: str) -> None:
        """Switch between custom and generated views for a plugin tab."""
        self._do_switch_view_mode(plugin_name, mode_type)

    _VIEW_MODE_TYPES = frozenset({
        "view-mode-custom", "view-mode-generated", "close-tab",
    })

    @work(thread=False)
    async def _do_switch_view_mode(self, plugin_name: str,
                                   mode_type: str) -> None:
        new_mode = ("custom" if mode_type == "view-mode-custom"
                    else "generated")
        tab_id = f"tab-plugin-{self._sanitize_id(plugin_name)}"

        if self._plugin_tab_modes.get(tab_id) == new_mode:
            return  # already in this mode

        # Mark mode early to guard against rapid clicks
        self._plugin_tab_modes[tab_id] = new_mode

        # Rebuild content (preserve toggle button registry entries)
        self._cleanup_registry_for_plugin(
            plugin_name, exclude_types=self._VIEW_MODE_TYPES,
        )
        plugin = self.plugin_core.plugins.get(plugin_name)
        content = self._build_plugin_tab_content(
            plugin_name, plugin, force_mode=new_mode,
            has_bar=True,
        )

        # Replace scroll container contents
        scroll_id = f"{tab_id}-scroll"
        try:
            scroll = self.query_one(f"#{scroll_id}", VerticalScroll)
        except NoMatches:
            return
        await scroll.remove_children()
        for w in content:
            await scroll.mount(w)

        # Update toggle button styles
        bar_id = f"{tab_id}-view-bar"
        try:
            bar = self.query_one(f"#{bar_id}", Horizontal)
            for btn in bar.query(Button):
                entry = self._id_registry.get(btn.id or "")
                if not entry:
                    continue
                is_active = entry["type"] == mode_type
                btn.remove_class("active-mode", "inactive-mode")
                btn.add_class("active-mode" if is_active else "inactive-mode")
        except NoMatches:
            pass

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
                await self._run_on_main(self.plugin_core.enable_plugin(plugin_name))
            elif action == "disable":
                await self._run_on_main(self.plugin_core.disable_plugin(plugin_name))
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

    @work(thread=False, exclusive=True, group="plugin-detail")
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
                entry["plugin"], entry["endpoint"], {"state": state}, hosts="any"
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
                plugin_name, access_name, args, hosts="any"
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
            await self._run_on_main(self.plugin_instance.execute(entry["plugin"], entry["endpoint"], hosts="any"))
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
                entry["plugin"], entry["endpoint"], args, hosts="any"
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

    def action_tab_networking(self) -> None:  # Phase 1
        self._switch_tab("tab-networking")

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
