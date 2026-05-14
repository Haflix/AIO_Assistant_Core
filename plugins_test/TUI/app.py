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

# Phase 3a — plugin_state imports used by `_update_plugin_detail` for
# FAILED_LOAD error retrieval (`PluginState.last_errors[Phase.LOAD]`).
# Imported at module load (NOT under TYPE_CHECKING) because runtime
# code reads `Phase.LOAD` as a dict key.
try:
    from plugin_state import Phase as _PluginPhase  # type: ignore[import]
except Exception:
    _PluginPhase = None  # tests may stub plugin_states with primitive types


# ─── Defaults ────────────────────────────────────────────────────────
MAX_GRAPH_POINTS = 60
DEFAULT_STATS_INTERVAL = 2.0
DEFAULT_PLUGIN_INTERVAL = 3.0
DEFAULT_REQUEST_INTERVAL = 1.0
DEFAULT_NETWORK_INTERVAL = 3.0  # Phase 1 — peers-table refresh tick

# Phase 3b — Logger-level Select options for the per-plugin tab Logger
# overrides section. `__keep__` is a string sentinel for "(none) — don't
# change this side" so the Select can use `allow_blank=False` (the
# Select.NULL singleton is reserved for the prepended allow_blank=True
# row, which would create a confusing dual-blank UX when an explicit
# "no change" option is also needed). Apply handler maps `__keep__` →
# Python None before calling PluginCore.set_logger_level.
_LOGGER_LEVEL_OPTIONS = [
    ("(none) — don't change", "__keep__"),
    ("DEBUG", "DEBUG"),
    ("INFO", "INFO"),
    ("WARNING", "WARNING"),
    ("ERROR", "ERROR"),
    ("CRITICAL", "CRITICAL"),
    ("MUTE", "MUTE"),
]


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
.phase-dim { color: #808080; text-style: bold; width: 1fr; }

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
    height: 1fr; max-height: 18;
    border: round #404040; padding: 1 2; margin: 1 0 0 0;
    background: #2d2d2d;
}
#plugin-detail Collapsible { background: transparent; padding: 0; }
#plugin-detail Collapsible CollapsibleTitle { background: #2d2d2d; color: #9bb5a0; padding: 0 1; }
.plugin-detail-section-body { height: auto; padding: 0 1; }
.plugin-detail-overrides-badge { color: #cca75a; text-style: italic; height: auto; padding: 0 0 1 0; }
.plugin-detail-failed-banner { color: #d16969; text-style: bold; height: auto; padding: 0 0 1 0; }
.plugin-detail-traceback { color: #cca75a; height: auto; padding: 0 1 1 1; }

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

/* ── Phase 2b — Events tab ───────────────────────── */
#events-outer { height: 1fr; }
/* TabbedContent itself fills the outer TabPane; inner TabPanes use 1fr
   per the CLAUDE.md "no height:100% in TabPane" rule. */
#events-tabs { height: 1fr; }
#events-subs-body, #events-cat-body, #events-live-body { height: 1fr; }
#events-subs-filters, #events-cat-filters, #events-live-filters,
#events-live-types {
    height: auto;
    padding: 0 0 1 0;
}
.events-filter {
    margin: 0 1 0 0;
    width: auto;
}
#events-subs-filter-topic, #events-subs-filter-hostname,
#events-subs-filter-uuid,
#events-cat-filter-topic,
#events-live-filter-topic, #events-live-filter-publisher {
    width: 1fr;
    min-width: 12;
}
#events-subs-filter-plugin, #events-cat-filter-plugin { min-width: 18; }
.events-counter {
    color: #808080;
    padding: 1 0 0 1;
    width: auto;
}
.events-hint { color: #808080; padding: 0 0 0 0; height: auto; }
.events-types-label { color: #c7a06e; padding: 1 1 0 0; }
#events-live-types Checkbox { margin: 0 1 0 0; }
#events-live-clear-btn { margin: 0 0 0 2; }
#events-subs-table, #events-cat-table {
    height: 1fr;
    min-height: 6;
    border: round #404040;
    background: #252525;
}
#events-live-table {
    height: 1fr;
    min-height: 6;
    border: round #404040;
    background: #1e1e1e;
}
.sub-disabled { color: #606060; }

/* ── Phase 3b — Per-plugin tab additions ─────────────────────────── */
.lifecycle-strip {
    height: auto;
    layout: horizontal;
    padding: 0 0 1 0;
}
.lifecycle-strip .stat-card { min-width: 16; height: 3; }
.per-plugin-section {
    height: auto;
    padding: 1 0 0 0;
}
.per-plugin-section-title {
    color: #c7a06e;
    text-style: bold;
    height: auto;
    padding: 0 0 0 0;
}
.per-plugin-section-hint {
    color: #808080;
    height: auto;
    padding: 0 0 1 0;
}
.per-plugin-empty { color: #808080; height: auto; padding: 0 1; }
.per-plugin-table {
    height: auto;
    max-height: 8;
    border: round #404040;
    background: #2d2d2d;
    margin: 0 0 1 0;
}
.per-plugin-logger-add-row {
    height: auto;
    layout: horizontal;
    padding: 0 0 1 0;
}
.per-plugin-logger-add-row Input,
.per-plugin-logger-add-row Select {
    width: 1fr;
    margin: 0 1 0 0;
}
.per-plugin-logger-add-row Button { width: auto; min-width: 8; }
.per-plugin-logger-header {
    height: auto;
    layout: horizontal;
}
.per-plugin-logger-header Static { width: 1fr; }
.per-plugin-logger-header Button { width: auto; min-width: 8; }
.per-plugin-logger-list { height: auto; }
.per-plugin-logger-row {
    height: auto;
    layout: horizontal;
    padding: 0 0 0 0;
}
.per-plugin-logger-row Static { width: 1fr; }
.per-plugin-logger-row Button { width: auto; min-width: 8; }
.per-plugin-jump-button { margin: 0 1 1 0; min-width: 24; }
.per-plugin-copy-uuid { width: auto; min-width: 6; }
.per-plugin-verbose-switch { width: auto; }
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


class SubscriptionDetailScreen(ModalScreen[None]):
    """Phase 2b — read-only modal showing every field of a Subscription.

    The Subs browser table truncates long values; this modal renders
    them in full. `sub_uuid` + `plugin_uuid` + `target_plugin_uuid` are
    shown explicitly so an operator pasting from logs can verify the
    full identity. A dedicated [c] binding copies the sub_uuid via
    OSC 52 (same approach as `CertPEMScreen._on_copy`).
    """

    DEFAULT_CSS = """
    SubscriptionDetailScreen {
        align: center middle;
    }
    #sub-modal-body {
        width: 96;
        height: auto;
        max-height: 36;
        border: thick #c7a06e;
        background: #252525;
        padding: 1 2;
    }
    #sub-modal-title { color: #c7a06e; text-style: bold; padding: 0 0 1 0; }
    .sub-modal-row { height: auto; padding: 0 0 0 0; layout: horizontal; }
    .sub-modal-key { color: #808080; width: 22; }
    .sub-modal-val { color: #d4d4d4; width: 1fr; }
    #sub-modal-actions { height: auto; padding: 1 0 0 0; }
    #sub-modal-actions Button { margin: 0 1 0 0; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("c", "copy_uuid", "Copy UUID", show=True),
    ]

    def __init__(self, *, sub_dict: dict) -> None:
        super().__init__()
        # Plain dict (NOT live Subscription) so a concurrent pop_plugin
        # between modal-open and modal-close can't yank fields out
        # underneath the rendered view.
        self._sub = dict(sub_dict)

    def compose(self) -> ComposeResult:
        with Vertical(id="sub-modal-body"):
            yield Static("Subscription", id="sub-modal-title")
            for label, key in (
                ("Topic:", "topic_pattern"),
                ("Owner:", "plugin_name"),
                ("Owner UUID:", "plugin_uuid"),
                ("Target plugin:", "target_plugin"),
                ("Target endpoint:", "target_access_name"),
                ("Target UUID:", "target_plugin_uuid"),
                ("Hosts:", "hosts"),
                ("Blocked hosts:", "blocked_hosts"),
                ("Authors:", "authors"),
                ("Blocked authors:", "blocked_authors"),
                ("Enabled:", "enabled"),
                ("Type:", "declared_kind"),
                ("Declared id:", "declared_id"),
                ("sub_uuid:", "sub_uuid"),
            ):
                with Horizontal(classes="sub-modal-row"):
                    yield Static(label, classes="sub-modal-key")
                    yield Static(
                        str(self._sub.get(key, "")),
                        classes="sub-modal-val",
                        markup=False,
                    )
            with Horizontal(id="sub-modal-actions"):
                yield Button("Copy UUID", id="sub-copy")
                yield Button("Close", id="sub-close")

    @on(Button.Pressed, "#sub-copy")
    def _on_copy(self) -> None:
        self.action_copy_uuid()

    @on(Button.Pressed, "#sub-close")
    def _on_close(self) -> None:
        self.dismiss()

    def action_copy_uuid(self) -> None:
        try:
            self.app.copy_to_clipboard(str(self._sub.get("sub_uuid", "")))
        except Exception:
            pass


class EventDetailScreen(ModalScreen[None]):
    """Phase 2b — read-only modal showing every field of a declared event.

    Topic is rendered with `[yellow]{var}[/]` markup highlighting on
    runtime placeholders (same renderer the Events catalogue uses).
    Markup escape is applied to the topic body BEFORE the yellow tags
    so a topic accidentally containing `[red]inject[/]` cannot break
    formatting.
    """

    DEFAULT_CSS = """
    EventDetailScreen {
        align: center middle;
    }
    #event-modal-body {
        width: 96;
        height: auto;
        max-height: 28;
        border: thick #c7a06e;
        background: #252525;
        padding: 1 2;
    }
    #event-modal-title { color: #c7a06e; text-style: bold; padding: 0 0 1 0; }
    .event-modal-row { height: auto; padding: 0 0 0 0; layout: horizontal; }
    .event-modal-key { color: #808080; width: 22; }
    .event-modal-val { color: #d4d4d4; width: 1fr; }
    #event-modal-actions { height: auto; padding: 1 0 0 0; }
    #event-modal-actions Button { margin: 0 1 0 0; }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
    ]

    def __init__(self, *, event_dict: dict) -> None:
        super().__init__()
        self._event = dict(event_dict)

    def compose(self) -> ComposeResult:
        with Vertical(id="event-modal-body"):
            yield Static("Event", id="event-modal-title")
            for label, key, plain in (
                ("Plugin:", "plugin", True),
                ("Event ID:", "event_id", True),
                ("Topic:", "topic_rendered", False),
                ("Hosts:", "hosts", True),
                ("Blocked hosts:", "blocked_hosts", True),
                ("Enabled:", "enabled", True),
                ("Description:", "description", True),
            ):
                with Horizontal(classes="event-modal-row"):
                    yield Static(label, classes="event-modal-key")
                    if plain:
                        yield Static(
                            str(self._event.get(key, "")),
                            classes="event-modal-val",
                            markup=False,
                        )
                    else:
                        yield Static(
                            str(self._event.get(key, "")),
                            classes="event-modal-val",
                            markup=True,
                        )
            with Horizontal(id="event-modal-actions"):
                yield Button("Close", id="event-close")

    @on(Button.Pressed, "#event-close")
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
        Binding("6", "tab_events", "Events"),
        Binding("7", "tab_settings", "Settings"),
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

        # ── Phase 2b — Events tab state ─────────────────────────────
        # Outer/inner visibility flags drive Live-stream `_live_visible`.
        # Both False by default — Home is the active tab on mount and the
        # outer/inner handlers haven't fired yet, so a Subs/Catalogue
        # refresh worker that fires off `on_mount` doesn't try to render
        # against a tab that isn't visible.
        self._outer_is_events: bool = False
        self._inner_is_live: bool = False
        # Cursor into the plugin-side live deque — `get_live_events_since`
        # returns rows whose `_seq > self._live_last_seen_seq`.
        self._live_last_seen_seq: int = 0
        # 100ms flush timer ref — created in `on_mount`, no stop until
        # app shutdown (cheap no-op when `_live_visible` is False or
        # the deque is empty).
        self._live_flush_timer = None
        # 250ms debounce-tick timer (cycle 1 review fix — was leaked
        # as orphan on `_start_timers` re-entry from settings-apply).
        self._debounce_timer = None
        # Refresh-debounce flags — set by bus observers, cleared by the
        # debounce timer / immediate-refresh on tab activation.
        self._subs_refresh_pending: bool = False
        self._cat_refresh_pending: bool = False
        # Phase 3a — Plugins-tab refresh-debounce flag. Set by
        # `_on_plugin_state_changed` (consolidated bus observer for
        # `_core/plugin/state_changed`); cleared by `_debounce_refresh_tick`
        # which spawns `_refresh_plugin_table_worker`. Unlike the Events-tab
        # flags above, this one is processed regardless of which outer tab
        # is active — the Plugins-tab table is always-visible-when-tabbed-to
        # so we want it fresh whenever the operator switches over.
        self._plugins_refresh_pending: bool = False
        # Phase 3a — name of the plugin currently displayed in the detail
        # pane (i.e. the row whose `RowHighlighted` last fired). None when
        # no row is selected. Read by `_on_plugin_state_changed` to decide
        # whether to direct-refresh the detail pane on a state transition
        # for THIS plugin (without waiting for the next row-highlight event).
        self._currently_displayed_plugin: Optional[str] = None
        # Phase 3a — per-plugin remembered open/collapsed state of each
        # detail-pane Collapsible section. Keyed by plugin name; value is
        # the set of section ids the operator has expanded. Used to
        # preserve open state across row-selection changes (Info section
        # always opens for the new plugin; the other four preserve their
        # last-seen state for that specific plugin).
        self._plugin_detail_open_sections: Dict[str, set] = {}
        # Filter-dirty flag for Live-stream: any Input/Checkbox change
        # in the Live-stream filters sets this; the flush timer reads
        # AND clears under no lock (same-thread mutation).
        self._live_filter_dirty: bool = False
        # Cached rendered rows for Live-stream so the same render isn't
        # recomputed unless `_live_filter_dirty` or new events arrived.
        # Stores the raw row dicts produced by `_classify_and_normalize`
        # (each carries its own `_seq` key — no extra tuple wrapper).
        self._live_rendered_rows: list = []
        # Phase 2b — internal-event-bus observers registered by the app
        # for refresh-debounce on the Subs / Catalogue tables. Mirrors
        # the pattern in plugin.py's on_enable — register on app mount,
        # unregister on app shutdown. Plugin uuid filled at register
        # time so `_unobserve_plugin` cleans up if the plugin pops.
        self._app_bus_observers: list = []

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
                    yield VerticalScroll(id="plugin-detail")

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

            # ── 6. Events (Phase 2b) ─────────────────────────────
            with TabPane("Events", id="tab-events"):
                with Vertical(id="events-outer"):
                    with TabbedContent(id="events-tabs"):
                        # ── Subscriptions browser ──────────────────
                        with TabPane("Subscriptions", id="events-tab-subs"):
                            with Vertical(id="events-subs-body"):
                                with Horizontal(id="events-subs-filters"):
                                    yield Select(
                                        [("All plugins", "__all__")],
                                        id="events-subs-filter-plugin",
                                        value="__all__",
                                        prompt="Plugin...",
                                        classes="events-filter",
                                    )
                                    yield Input(
                                        placeholder="Topic substring...",
                                        id="events-subs-filter-topic",
                                        classes="events-filter",
                                    )
                                    yield Input(
                                        placeholder="Hostname substring...",
                                        id="events-subs-filter-hostname",
                                        classes="events-filter",
                                    )
                                    yield Input(
                                        placeholder="sub_uuid substring...",
                                        id="events-subs-filter-uuid",
                                        classes="events-filter",
                                    )
                                    yield Checkbox(
                                        "Enabled only",
                                        value=False,
                                        id="events-subs-filter-enabled-only",
                                        classes="events-filter",
                                    )
                                    yield Static(
                                        "Showing 0 / 0",
                                        id="events-subs-counter",
                                        classes="events-counter",
                                    )
                                yield DataTable(
                                    id="events-subs-table",
                                    cursor_type="row",
                                )
                                yield Static(
                                    "[dim][e] Toggle  [c] Copy UUID  "
                                    "[t] Copy topic  [Enter] Details[/dim]",
                                    id="events-subs-hint",
                                    classes="events-hint",
                                    markup=True,
                                )

                        # ── Events catalogue ───────────────────────
                        with TabPane("Catalogue", id="events-tab-cat"):
                            with Vertical(id="events-cat-body"):
                                with Horizontal(id="events-cat-filters"):
                                    yield Select(
                                        [("All plugins", "__all__")],
                                        id="events-cat-filter-plugin",
                                        value="__all__",
                                        prompt="Plugin...",
                                        classes="events-filter",
                                    )
                                    yield Input(
                                        placeholder="Topic substring...",
                                        id="events-cat-filter-topic",
                                        classes="events-filter",
                                    )
                                    yield Checkbox(
                                        "Enabled only",
                                        value=False,
                                        id="events-cat-filter-enabled-only",
                                        classes="events-filter",
                                    )
                                    yield Static(
                                        "Showing 0 / 0",
                                        id="events-cat-counter",
                                        classes="events-counter",
                                    )
                                yield Static(
                                    "[dim]Yellow segments are runtime "
                                    "placeholders (resolved at publish "
                                    "time via topic_vars)[/dim]",
                                    id="events-cat-legend",
                                    classes="events-hint",
                                    markup=True,
                                )
                                yield DataTable(
                                    id="events-cat-table",
                                    cursor_type="row",
                                )
                                yield Static(
                                    "[dim][e] Toggle  [c] Copy topic  "
                                    "[Enter] Details[/dim]",
                                    id="events-cat-hint",
                                    classes="events-hint",
                                    markup=True,
                                )

                        # ── Live-stream ────────────────────────────
                        with TabPane("Live-stream", id="events-tab-live"):
                            with Vertical(id="events-live-body"):
                                with Horizontal(id="events-live-filters"):
                                    yield Input(
                                        placeholder="Topic substring...",
                                        id="events-live-filter-topic",
                                        classes="events-filter",
                                    )
                                    yield Input(
                                        placeholder="Publisher substring...",
                                        id="events-live-filter-publisher",
                                        classes="events-filter",
                                    )
                                    yield Static(
                                        "Showing 0 / 0 events",
                                        id="events-live-counter",
                                        classes="events-counter",
                                    )
                                with Horizontal(id="events-live-types"):
                                    yield Static(
                                        "Types:",
                                        classes="events-types-label",
                                    )
                                    yield Checkbox(
                                        "pub", value=True,
                                        id="events-live-type-pub",
                                    )
                                    yield Checkbox(
                                        "req", value=True,
                                        id="events-live-type-req",
                                    )
                                    yield Checkbox(
                                        "first", value=True,
                                        id="events-live-type-first",
                                    )
                                    yield Checkbox(
                                        "end", value=True,
                                        id="events-live-type-end",
                                    )
                                    yield Checkbox(
                                        "sub", value=True,
                                        id="events-live-type-sub",
                                    )
                                    yield Checkbox(
                                        "evt", value=True,
                                        id="events-live-type-evt",
                                    )
                                    yield Button(
                                        "Clear",
                                        id="events-live-clear-btn",
                                    )
                                yield DataTable(
                                    id="events-live-table",
                                    cursor_type="row",
                                )

            # ── 7. Settings ──────────────────────────────────────
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
            # Phase 3a — 8 columns: Name / Phase / Version / Remote /
            # Endpoints count / Subs count (`declared` or `declared+runtime`) /
            # Events count / Description. Phase column derived by
            # `_plugin_phase` from `pc.plugin_states[name].state` plus the
            # plugin instance's `_lifecycle_ready` and `ready` events.
            t.add_columns(
                "Name", "Phase", "Ver", "R", "Eps", "Subs", "Evs",
                "Description",
            )
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

        # Phase 2b — Events tab columns. Eight Subs columns, five
        # Catalogue columns, five Live-stream columns. We set the
        # column counts here so workers can do `add_row` directly
        # without re-defining columns each refresh.
        try:
            subs_t = self.query_one("#events-subs-table", DataTable)
            subs_t.add_columns(
                "Topic", "Owner", "Target", "Hosts", "Authors",
                "Enabled", "Type", "sub_uuid",
            )
        except NoMatches:
            pass
        try:
            cat_t = self.query_one("#events-cat-table", DataTable)
            cat_t.add_columns(
                "Plugin", "Event ID", "Topic", "Hosts", "Enabled",
            )
        except NoMatches:
            pass
        try:
            live_t = self.query_one("#events-live-table", DataTable)
            live_t.add_columns(
                "Time", "Type", "Topic", "Publisher", "Detail",
            )
        except NoMatches:
            pass

        # Phase 2b — register the app-side bus observers (debounce flags
        # for Subs / Catalogue refresh). The plugin owns the 5 live-stream
        # observers; the app owns the refresh-debounce observers because
        # the trigger is "rebuild the table" which is TUI-side state.
        # We register them on the plugin's uuid so framework auto-cleanup
        # via `_unobserve_plugin` fires on plugin pop.
        try:
            plugin_uuid = self.plugin_instance.plugin_uuid
        except AttributeError:
            plugin_uuid = None
        if plugin_uuid is not None:
            pc = self.plugin_core
            self._app_bus_observers = [
                ("_core/subscription/state_changed", self._on_subs_refresh_signal),
                ("_core/event/state_changed", self._on_cat_refresh_signal),
                # Phase 3a — consolidated observer for plugin lifecycle.
                # Pre-Phase-3 had two entries here (subs + cat handlers,
                # each setting their own pending flag). The new handler
                # sets all three pending flags + direct-refreshes the
                # detail pane when the changed plugin is currently
                # displayed. 4 entries → 3; one listener-iteration per
                # emit instead of two.
                ("_core/plugin/state_changed", self._on_plugin_state_changed),
            ]
            for topic, cb in self._app_bus_observers:
                try:
                    pc.internal_observe(plugin_uuid, topic, cb)
                except Exception:
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
        if self._live_flush_timer:  # Phase 2b
            self._live_flush_timer.stop()
        if self._debounce_timer:  # Phase 2b
            self._debounce_timer.stop()
        self._stats_timer = self.set_interval(self._stats_interval, self._refresh_stats_worker)
        self._plugin_timer = self.set_interval(self._plugin_interval, self._periodic_plugin_refresh)
        self._request_timer = self.set_interval(self._request_interval, self._refresh_requests_worker)
        self._log_timer = self.set_interval(0.5, self._refresh_log_table)
        self._network_timer = self.set_interval(  # Phase 1
            self._network_interval, self._refresh_peers_table_worker,
        )
        # Phase 2b — 100ms Live-stream flush + 250ms Subs/Catalogue
        # debounce. Both are cheap no-ops when nothing changed
        # (`_live_visible` False, or no `_*_refresh_pending` flag set).
        self._live_flush_timer = self.set_interval(0.1, self._flush_live_events)
        self._debounce_timer = self.set_interval(0.25, self._debounce_refresh_tick)

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
        # Phase 2b — explicitly unregister the app-side bus observers
        # registered in `on_mount`. PluginCore's `_unobserve_plugin`
        # auto-cleans on plugin pop, but app exit (e.g. user presses
        # `q`) is independent of plugin pop. Without explicit
        # cleanup, these observers stay registered in
        # `pc._internal_observers` until the plugin is later popped
        # — a leak on the app-exits-but-plugin-keeps-running path.
        try:
            plugin_uuid = self.plugin_instance.plugin_uuid
        except AttributeError:
            plugin_uuid = None
        if plugin_uuid is not None:
            pc = self.plugin_core
            for topic, cb in getattr(self, "_app_bus_observers", []):
                try:
                    pc.internal_unobserve(plugin_uuid, topic, cb)
                except Exception:
                    pass
            self._app_bus_observers = []
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

    def _plugin_phase(self, name: str, plugin) -> tuple[str, str]:
        """Phase 3a — derive the Plugins-tab Phase column cell for a plugin.

        Reads `pc.plugin_states[name].state` plus the readiness events
        on the plugin instance and returns a (label, css_class) pair.
        Centralized so the table renderer and the per-plugin tab's
        Lifecycle strip (Phase 3b) share one source of truth.

        Mapping:
          - state == ENABLED:
              - lifecycle_ready set AND ready set                    -> READY  (good)
              - lifecycle_ready set AND not ready set                -> WAITING (warn)
              - otherwise (defensive — lifecycle_ready guaranteed
                set when state == ENABLED per framework contract)    -> READY  (good)
          - state == ENABLING                                        -> LOADING   (warn)
          - state == DISABLING                                       -> DISABLING (warn)
          - state == INACTIVE                                        -> DISABLED  (dim)
          - state == UNLOADED                                        -> UNLOADED  (dim)
          - state == FAILED_LOAD                                     -> FAILED    (bad)
          - state absent / enum value unknown                        -> upper-case (warn)

        Compares `state.value` (string) rather than the enum directly
        so this method doesn't need to import `plugin_state.State` — keeps
        the TUI module independent of framework-internal enum identity
        and tolerant of future enum additions per `plugin_state.py:19-21`
        ("External tooling reading `state.value` should handle unknown
        values gracefully").
        """
        # MagicMock auto-creates any attribute as another MagicMock, so a
        # plain `hasattr` check passes even when the fixture didn't set
        # plugin_states. Require an actual mapping so test fixtures that
        # don't install Phase 3a's plugin_states surface fall through to
        # the `?` warn cell rather than crashing on MagicMock `.get()` /
        # `.value` cascades.
        plugin_states = getattr(self.plugin_core, "plugin_states", None)
        if not isinstance(plugin_states, dict):
            return ("?", "stat-val-warn")
        ps = plugin_states.get(name)
        if ps is None:
            return ("?", "stat-val-warn")
        state = getattr(ps, "state", None)
        val = getattr(state, "value", None)
        if not isinstance(val, str):
            return ("?", "stat-val-warn")
        if val == "unloaded":
            return ("UNLOADED", "phase-dim")
        if val == "inactive":
            return ("DISABLED", "phase-dim")
        if val == "enabling":
            return ("LOADING", "stat-val-warn")
        if val == "disabling":
            return ("DISABLING", "stat-val-warn")
        if val == "failed_load":
            return ("FAILED", "stat-val-bad")
        if val == "enabled":
            if plugin is None:
                return ("READY", "stat-val-good")
            lifecycle_ready = getattr(plugin, "_lifecycle_ready", None)
            ready = getattr(plugin, "ready", None)
            lifecycle_set = bool(lifecycle_ready.is_set()) \
                if lifecycle_ready is not None and hasattr(lifecycle_ready, "is_set") \
                else False
            ready_set = bool(ready.is_set()) \
                if ready is not None and hasattr(ready, "is_set") \
                else False
            if lifecycle_set and not ready_set:
                return ("WAITING", "stat-val-warn")
            return ("READY", "stat-val-good")
        return (val.upper(), "stat-val-warn")

    def _resolve_plugin_config_dict(self, plugin_name: str) -> Optional[dict]:
        """Phase 3a — read the on-disk plugin_config.yml for ``plugin_name``.

        Returns the parsed dict on success, or None when the plugin is
        not in the main config, the plugin directory or file is missing,
        or the YAML parse fails. Used by:

          * `_build_args_section` — diff against `plugin.arguments` to
            decide whether to render the "overrides applied" badge.
          * `_update_plugin_detail` FAILED_LOAD path — read endpoints/
            events/subscriptions sections directly when the plugin
            instance never came up (per Section 7 of the Phase 3 plan;
            `pc.get_unloaded_metadata` returns None for non-UNLOADED so
            it can't be used as a fallback here).

        Path resolution mirrors `PluginCore.load_plugin_with_conf`:
        the plugin entry's `path` field is preferred; otherwise
        `{plugin_package}/{plugin_name}`. Errors are swallowed at the
        boundary — callers receive None and render their own placeholder.
        """
        try:
            entry = next(
                (
                    p for p in (self.plugin_core.yaml_config.get("plugins") or [])
                    if isinstance(p, dict) and p.get("name") == plugin_name
                ),
                None,
            )
        except Exception:
            return None
        if entry is None:
            return None
        try:
            base_path = entry.get("path") or os.path.join(
                getattr(self.plugin_core, "plugin_package", "plugins"),
                plugin_name,
            )
            cfg_path = os.path.join(os.path.abspath(base_path), "plugin_config.yml")
            if not os.path.isfile(cfg_path):
                return None
            with open(cfg_path, "r", encoding="utf-8") as f:
                parsed = yaml.safe_load(f)
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    @work(thread=False, exclusive=True, group="plugins")
    async def _refresh_plugin_table_worker(self) -> None:
        """Phase 3a — rebuild the Plugins-tab table.

        Row source changed pre-Phase-3 → Phase 3a: was `pc.plugins.items()`
        (instances dict, misses UNLOADED plugins); now iterates
        `pc.plugin_states` keys (snapshot via `dict(...)` per the iteration
        contract in `plugin_state.py:9-17`). This makes UNLOADED plugins
        visible in the table (config has an entry but no live instance) —
        the Phase column then surfaces their state explicitly.

        Columns (8): Name / Phase / Ver / R / Eps / Subs / Evs / Description.

          * Phase  — derived by `_plugin_phase` (state enum + readiness
                     events). UNLOADED plugins have no instance, so phase
                     reads enum only.
          * Subs   — `{declared}` or `{declared}+{runtime}`. declared =
                     `len(plugin.subscriptions)`; runtime =
                     `len(plugin._sub_uuids) - declared` (NOT
                     `len(_sub_uuids) > 0` — `_sub_uuids` contains BOTH
                     YAML and runtime sub_uuids per
                     `PluginCore._register_yaml_subscriptions` /
                     `subscribe_event` writing to the same list).
                     Renders `?` when the instance is absent (UNLOADED).
          * Evs    — `len(plugin.events)`. `?` when instance absent.
        """
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

        # Snapshot under the iteration contract. Require an actual dict —
        # MagicMock fixtures that didn't install plugin_states auto-create
        # a MagicMock attribute, which `dict(...)` would either iterate
        # spuriously or raise. Fall back to {} so the merge with pc.plugins
        # below produces a coherent row list.
        ps_attr = getattr(self.plugin_core, "plugin_states", None)
        try:
            states_snapshot = dict(ps_attr) if isinstance(ps_attr, dict) else {}
        except Exception:
            states_snapshot = {}

        # Fall-back for tests / older fixtures that didn't populate
        # plugin_states: include any plugin currently in pc.plugins that
        # doesn't have a state entry. Treats them as DISABLED-style for
        # the phase column (`_plugin_phase` returns `?` when ps is None).
        try:
            plugins_attr = getattr(self.plugin_core, "plugins", None)
            if isinstance(plugins_attr, dict):
                for name in list(plugins_attr):
                    if name not in states_snapshot:
                        states_snapshot[name] = None
        except Exception:
            pass

        filt = self._plugin_filter.lower()

        for name in sorted(states_snapshot.keys()):
            # Live instance (None for UNLOADED / never-loaded / fallback rows).
            plugin = None
            try:
                plugin = self.plugin_core.plugins.get(name)
            except Exception:
                plugin = None

            # Plugin metadata. For UNLOADED rows the on-disk metadata
            # could be read via _resolve_plugin_config_dict, but that's
            # I/O per row in a hot worker — defer to the detail pane.
            # Table row shows `?` for fields we can't cheaply derive.
            version = getattr(plugin, "version", "?") if plugin else "?"
            remote = bool(getattr(plugin, "remote", False)) if plugin else False
            desc = getattr(plugin, "description", "") if plugin else ""
            endpoints = getattr(plugin, "endpoints", {}) if plugin else {}
            ep_count_int = len(endpoints) if isinstance(endpoints, dict) else 0
            ep_count = str(ep_count_int) if plugin is not None else "?"

            # Subs cell: declared + runtime (`+N` suffix when runtime > 0).
            if plugin is not None:
                declared_subs = getattr(plugin, "subscriptions", {}) or {}
                declared_count = len(declared_subs) if isinstance(declared_subs, dict) else 0
                sub_uuids = getattr(plugin, "_sub_uuids", []) or []
                total_count = len(sub_uuids) if isinstance(sub_uuids, list) else 0
                runtime_count = max(0, total_count - declared_count)
                subs_cell = (
                    f"{declared_count}+{runtime_count}"
                    if runtime_count > 0
                    else str(declared_count)
                )
            else:
                subs_cell = "?"

            # Evs cell.
            if plugin is not None:
                events = getattr(plugin, "events", {}) or {}
                evs_cell = str(len(events)) if isinstance(events, dict) else "0"
            else:
                evs_cell = "?"

            # Filter — applies to Name and Description.
            if filt and filt not in name.lower() and filt not in (desc or "").lower():
                continue

            phase_label, phase_class = self._plugin_phase(name, plugin)
            # Render Phase as a Text with the matching color so the
            # operator can spot READY/WAITING/FAILED visually. Map
            # phase css class -> Text style. Mirrors the inline-style
            # pattern used for the legacy Status column.
            _PHASE_STYLE = {
                "stat-val-good": "green",
                "stat-val-warn": "#cca75a",
                "stat-val-bad": "red",
                "phase-dim": "#808080",
            }
            phase_cell = Text(phase_label, style=_PHASE_STYLE.get(phase_class, ""))

            remote_str = Text("R", style="cyan") if remote else Text("L", style="dim")
            desc_short = (desc[:40] + "...") if len(desc) > 43 else desc

            table.add_row(
                name,
                phase_cell,
                version,
                remote_str,
                ep_count,
                subs_cell,
                evs_cell,
                desc_short,
                key=name,
            )

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

    # ───────────────────────────────────────────────────────────────────
    # Phase 2b — Events tab logic (Subs / Catalogue / Live-stream)
    # ───────────────────────────────────────────────────────────────────
    # Layout note:
    #   * Bus-driven debounce signals + the 250ms tick that consumes them
    #   * Three workers: `_refresh_subs_browser_worker`,
    #     `_refresh_events_catalogue_worker`, `_flush_live_events`
    #   * Key bindings (`e`/`c`/`t`/Enter) routed via DataTable focus
    #   * Helpers: `_render_topic_with_placeholders` (markup escape),
    #     `_truncate_cell`, filter functions
    # Implementation order in this block mirrors plan Section 10:
    #   subs first, catalogue second, live-stream third.

    # ── Phase 2b helpers — shared by Subs + Catalogue + Live-stream ────

    # Template-var regex defined locally (NOT imported from PluginCore)
    # so the TUI plugin doesn't take a hard dependency on framework
    # internals — see plan Section 5.3 cycle 2 M10 fix.
    _TEMPLATE_VAR_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")

    @staticmethod
    def _truncate_cell(value, max_len: int) -> str:
        """Truncate a stringified value to `max_len` chars; append '...'
        when truncated. Always returns a `str` even when `value` is None
        or a list. Modal opens with `Enter` for the full uncapped value.
        """
        s = str(value) if value is not None else ""
        if len(s) > max_len:
            return s[: max(1, max_len - 3)] + "..."
        return s

    @classmethod
    def _render_topic_with_placeholders(cls, topic: str) -> str:
        """Return a Rich-markup string highlighting `{var}` placeholders
        in yellow. The full topic is escaped FIRST so a topic containing
        `[red]inject[/]` cannot inject formatting.

        Per plan Section 5.3 — yellow color picked over italic+dim
        because italic+dim is invisible on many terminal emulators.
        Markup escape inside the substitution lambda's match group too,
        defensively, in case future variable names introduce special
        characters (currently the regex restricts to identifier-style).
        """
        escaped = escape(topic)
        return cls._TEMPLATE_VAR_RE.sub(
            lambda m: f"[yellow]{escape(m.group(0))}[/]", escaped,
        )

    def _on_subs_refresh_signal(self, topic: str, payload: dict) -> None:
        """Bus observer: set the debounce flag for the Subs browser.

        Runs on the loop thread. Setting a bool is GIL-atomic so no
        lock is required; the 250ms `_debounce_refresh_tick` runs on
        the TUI thread which clears the flag + spawns the worker.

        Observer callback contract: must return < 1ms and must not
        raise — Exception subclasses are swallowed by `_internal_emit`,
        but we still keep this method side-effect-free to avoid cascading
        errors into other observers in the same emit.
        """
        self._subs_refresh_pending = True

    def _on_cat_refresh_signal(self, topic: str, payload: dict) -> None:
        """Bus observer: set the debounce flag for the Events catalogue.

        Same shape as `_on_subs_refresh_signal`.
        """
        self._cat_refresh_pending = True

    def _on_plugin_state_changed(self, topic: str, payload: dict) -> None:
        """Phase 3a — consolidated bus observer for `_core/plugin/state_changed`.

        Plugin lifecycle changes invalidate three views at once: the
        Plugins-tab table (Phase/Subs/Evs columns + UNLOADED visibility),
        the Subs browser (registered subs come and go with on_enable /
        on_disable), and the Events catalogue (declared events change
        with load/unload). This handler sets all three pending flags so
        the debounce tick refreshes whichever tables are currently
        rendered. The Plugins-tab flag is processed regardless of which
        outer tab is active (Plugins tab isn't `_outer_is_events`-gated);
        the Subs/Cat flags stay gated on outer-tab visibility — same
        rationale as pre-Phase-3 (those tables only exist when the
        Events tab is rendered, so refreshing them while invisible is
        wasted work).

        Also direct-refreshes the detail pane when the changed plugin is
        the one currently displayed — without this the operator would
        see stale Phase / Endpoints / Subs sections until they re-select
        the row.

        Runs on the loop thread. Sync — flag-set is GIL-atomic and the
        direct call to `_update_plugin_detail` is a `@work`-decorated
        method (spawns its own task without awaiting here). Observer
        callbacks must return < 1ms per the framework's observer
        contract; this body is bounded.
        """
        self._plugins_refresh_pending = True
        self._subs_refresh_pending = True
        self._cat_refresh_pending = True
        name = payload.get("name") if isinstance(payload, dict) else None
        if name is not None and name == self._currently_displayed_plugin:
            try:
                self._update_plugin_detail(name)
            except Exception:
                # Observer must not raise. _update_plugin_detail is a
                # @work method so the spawn itself shouldn't fail, but
                # defensive against widget-not-mounted-yet edge cases.
                pass

    def _debounce_refresh_tick(self) -> None:
        """Read-and-clear the debounce flags; spawn workers when set.

        Runs on the TUI thread at 250ms (the timer registered in
        `_start_timers`).

        Phase 3a — split path: the Plugins-tab flag fires regardless of
        which outer tab is active (the Plugins-tab table itself isn't
        `_outer_is_events`-gated). The Events-tab flags remain gated on
        outer-tab visibility — those tables only exist when the Events
        tab is rendered, so refreshing them while invisible is wasted
        work. Pre-Phase-3 the whole body early-returned on `not
        self._outer_is_events`; folding the new Plugins-tab flag into
        the same early-return would silently swallow plugin state
        changes whenever any non-Events tab was active.
        """
        if self._plugins_refresh_pending:
            self._plugins_refresh_pending = False
            self._refresh_plugin_table_worker()
        if not self._outer_is_events:
            return
        if self._subs_refresh_pending:
            self._subs_refresh_pending = False
            self._refresh_subs_browser_worker()
        if self._cat_refresh_pending:
            self._cat_refresh_pending = False
            self._refresh_events_catalogue_worker()

    def _get_subs_filter_state(self) -> dict:
        """Snapshot the Subs browser filter widget values. Returns a
        dict the worker uses to filter rows. Missing widgets default
        to permissive values so the worker can run before `on_mount`
        finishes (test-harness ordering).
        """
        state = {
            "plugin": "__all__",
            "topic": "",
            "hostname": "",
            "uuid": "",
            "enabled_only": False,
        }
        try:
            state["plugin"] = str(self.query_one(
                "#events-subs-filter-plugin", Select,
            ).value or "__all__")
        except (NoMatches, Exception):
            pass
        try:
            state["topic"] = self.query_one(
                "#events-subs-filter-topic", Input,
            ).value
        except NoMatches:
            pass
        try:
            state["hostname"] = self.query_one(
                "#events-subs-filter-hostname", Input,
            ).value
        except NoMatches:
            pass
        try:
            state["uuid"] = self.query_one(
                "#events-subs-filter-uuid", Input,
            ).value
        except NoMatches:
            pass
        try:
            state["enabled_only"] = self.query_one(
                "#events-subs-filter-enabled-only", Checkbox,
            ).value
        except NoMatches:
            pass
        return state

    def _sub_passes_filter(self, sub_row: dict, flt: dict) -> bool:
        """Apply the filter state to a Subs row dict. Substring matches
        are case-insensitive; the Plugin select uses a `__all__`
        sentinel so the empty default doesn't accidentally hide everything.

        Cycle 2 fresh-eyes fix: hostname filter renders ``None`` as
        the empty string (NOT the literal `"None"`) before substring
        matching. Without this, typing `"one"` in the hostname filter
        unexpectedly matched every sub with `hosts=None` because
        ``str(None) == "None"`` contains the substring `"one"`.
        """
        if flt["enabled_only"] and not sub_row["enabled"]:
            return False
        if flt["plugin"] != "__all__" and sub_row["plugin_name"] != flt["plugin"]:
            return False
        if flt["topic"]:
            needle = flt["topic"].lower()
            if needle not in str(sub_row["topic_pattern"]).lower():
                return False
        if flt["hostname"]:
            needle = flt["hostname"].lower()
            hosts_val = sub_row.get("hosts")
            hosts_str = "" if hosts_val is None else str(hosts_val)
            if needle not in hosts_str.lower():
                return False
        if flt["uuid"]:
            needle = flt["uuid"].lower()
            if needle not in str(sub_row["sub_uuid"]).lower():
                return False
        return True

    def _populate_subs_plugin_filter_options(self, sub_rows: list) -> None:
        """Refresh the Plugin select's options from the current snapshot.

        Adds an `All plugins` sentinel + every distinct plugin_name in
        sub_rows. Preserves the current selection when it survives the
        rebuild; otherwise reverts to `__all__`.

        Cycle 1 review fix: suppress `Select.Changed` events for the
        duration of the rebuild via `self.prevent(...)` so the
        post-rebuild value re-assignment doesn't loop back into the
        worker through `_on_subs_filter_plugin_changed`. The worker
        is `exclusive=True` in its group so a self-trigger would
        cancel the in-flight render mid-row and produce a visible
        flicker (counter snapping to `M / M` for one frame).
        """
        try:
            sel = self.query_one("#events-subs-filter-plugin", Select)
        except NoMatches:
            return
        plugin_names = sorted({r["plugin_name"] for r in sub_rows})
        options = [("All plugins", "__all__")] + [
            (n, n) for n in plugin_names
        ]
        current = str(sel.value or "__all__")
        with self.prevent(Select.Changed):
            try:
                sel.set_options(options)
            except Exception:
                return
            if current == "__all__" or current in plugin_names:
                sel.value = current
            else:
                sel.value = "__all__"

    @work(thread=False, exclusive=True, group="events-subs")
    async def _refresh_subs_browser_worker(self) -> None:
        """Rebuild the Subs browser table from a `list_local_subs` snapshot.

        Runs on the TUI loop via Textual's worker. The PluginCore call
        is async and acquires `topic_registry._lock`, so we bridge via
        `_run_on_main` (it runs on the main loop, where the registry
        lock lives).
        """
        pc = self.plugin_core
        try:
            subs = await self._run_on_main(
                pc.topic_registry.list_local_subs()
            )
        except Exception:
            subs = None
        if subs is None:
            subs = []

        rows = []
        for s in subs:
            target_plugin = getattr(s, "target_plugin", "") or getattr(
                s, "plugin_name", ""
            )
            target_access = getattr(s, "target_access_name", "") or ""
            if target_plugin == getattr(s, "plugin_name", ""):
                target_render = f".{target_access}"
            else:
                target_render = f"{target_plugin}.{target_access}"
            rows.append({
                "topic_pattern": getattr(s, "topic_pattern", ""),
                "plugin_name": getattr(s, "plugin_name", ""),
                "plugin_uuid": getattr(s, "plugin_uuid", ""),
                "target_plugin": target_plugin,
                "target_access_name": target_access,
                "target_plugin_uuid": getattr(s, "target_plugin_uuid", None),
                "target_render": target_render,
                "hosts": getattr(s, "hosts", None),
                "blocked_hosts": getattr(s, "blocked_hosts", None),
                "authors": getattr(s, "authors", None),
                "blocked_authors": getattr(s, "blocked_authors", None),
                "enabled": bool(getattr(s, "enabled", True)),
                "declared_id": getattr(s, "declared_id", None),
                "declared_kind": (
                    "YAML" if getattr(s, "declared_id", None) is not None
                    else "runtime"
                ),
                "sub_uuid": getattr(s, "sub_uuid", ""),
            })

        # Plugin filter dropdown options reflect the current snapshot —
        # do this BEFORE applying the filter so a newly-loaded plugin
        # shows up in the select.
        self._populate_subs_plugin_filter_options(rows)

        flt = self._get_subs_filter_state()
        visible = [r for r in rows if self._sub_passes_filter(r, flt)]

        try:
            table = self.query_one("#events-subs-table", DataTable)
        except NoMatches:
            return
        table.clear()
        # Cache the row dicts onto the app so toggle / detail handlers
        # can look up the full Subscription state by row index without
        # re-querying the registry.
        self._subs_rendered_rows = visible
        for r in visible:
            # Enabled cell carries our own static markup — plain string
            # passed through `default_cell_formatter`'s `Text.from_markup`
            # path renders the green/dim correctly.
            #
            # User-controlled fields (`topic_pattern`, `hosts`,
            # `authors_cell`) come from registry data that plugin
            # authors write — wrap in `Text(...)` so accidental
            # `[`/`]` in the values renders literally rather than
            # attempting markup interpretation. Validated-identifier
            # and framework-controlled fields skip the wrap (they
            # can't contain markup chars by construction).
            enabled_cell = (
                "[green]on[/]" if r["enabled"] else "[dim]off[/]"
            )
            authors_cell = (
                "*" if r["authors"] is None
                else self._truncate_cell(r["authors"], 12)
            )
            sub_uuid = r["sub_uuid"] or ""
            uuid_cell = (
                sub_uuid[:8] + "..." if len(sub_uuid) > 8 else sub_uuid
            )
            table.add_row(
                Text(self._truncate_cell(r["topic_pattern"], 20)),
                self._truncate_cell(r["plugin_name"], 16),
                self._truncate_cell(r["target_render"], 20),
                Text(self._truncate_cell(r["hosts"], 12)),
                Text(authors_cell),
                enabled_cell,
                r["declared_kind"],
                uuid_cell,
            )

        # Counter update.
        try:
            self.query_one("#events-subs-counter", Static).update(
                f"Showing {len(visible)} / {len(rows)}"
            )
        except NoMatches:
            pass

    # ── Subs browser interactions ──────────────────────────────────────

    @on(Input.Changed, "#events-subs-filter-topic")
    @on(Input.Changed, "#events-subs-filter-hostname")
    @on(Input.Changed, "#events-subs-filter-uuid")
    def _on_subs_filter_input_changed(self, event: Input.Changed) -> None:
        # Inline re-render of the existing snapshot via the worker —
        # avoids hitting the registry repeatedly while typing.
        self._refresh_subs_browser_worker()

    @on(Select.Changed, "#events-subs-filter-plugin")
    def _on_subs_filter_plugin_changed(self, event: Select.Changed) -> None:
        self._refresh_subs_browser_worker()

    @on(Checkbox.Changed, "#events-subs-filter-enabled-only")
    def _on_subs_filter_enabled_only_changed(self, event: Checkbox.Changed) -> None:
        self._refresh_subs_browser_worker()

    @on(DataTable.RowSelected, "#events-subs-table")
    def _on_subs_row_selected(self, event: DataTable.RowSelected) -> None:
        self._open_sub_detail_modal()

    def _selected_sub_row(self) -> Optional[dict]:
        """Return the cached row dict for the current cursor row, or
        None if there is no rendered selection. Called by the e/c/t
        key handlers + the Enter→modal handler.
        """
        rows = getattr(self, "_subs_rendered_rows", []) or []
        try:
            table = self.query_one("#events-subs-table", DataTable)
        except NoMatches:
            return None
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(rows):
            return None
        return rows[idx]

    def _open_sub_detail_modal(self) -> None:
        row = self._selected_sub_row()
        if row is None:
            return
        # Double-push guard mirroring `_open_cert_modal`.
        if any(isinstance(s, SubscriptionDetailScreen) for s in self.screen_stack):
            return
        self.push_screen(SubscriptionDetailScreen(sub_dict=row))

    @work(thread=False, exclusive=True, group="events-subs-toggle")
    async def _toggle_selected_sub(self) -> None:
        row = self._selected_sub_row()
        if row is None:
            return
        await self._apply_subscription_toggle(row)

    async def _apply_subscription_toggle(self, row: dict) -> None:
        """Body of the subscription-toggle action. Extracted from the
        @work wrapper so tests can drive it directly without depending
        on Textual's worker scheduling semantics.
        """
        sub_uuid = row["sub_uuid"]
        current = bool(row["enabled"])
        new_value = not current
        try:
            result = await self._run_on_main(
                self.plugin_core.set_subscription_enabled(sub_uuid, new_value)
            )
        except asyncio.TimeoutError:
            self.notify("Toggle timed out (30s)", severity="warning")
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.notify(f"Toggle failed: {e}", severity="error")
            return
        if result is None:
            self.notify(
                "Cannot toggle — main loop unavailable",
                severity="warning",
            )
            return
        if result is False:
            self.notify(
                "Subscription no longer exists",
                severity="warning",
            )
            return
        # `set_subscription_enabled` returns True for both "toggled
        # successfully" AND "no-op (already at target value)". The
        # bus emit only fires on actual change, so the refresh hook
        # only triggers when state moved. Operator's view reconciles
        # via the next emit-driven refresh.

    def _copy_to_clipboard_safe(self, text: str, label: str) -> None:
        """OSC 52 clipboard write with status-line toast. Best-effort:
        terminals without OSC 52 silently no-op (notably macOS Terminal);
        the toast still fires so the operator gets feedback that the
        keystroke was acknowledged.
        """
        try:
            self.copy_to_clipboard(str(text))
            self.notify(f"Copied {label}", timeout=2.0)
        except Exception:
            self.notify(f"Copy failed: {label}", severity="warning")

    @on(DataTable.HeaderSelected, "#events-subs-table")
    def _on_subs_header_selected(self, event) -> None:
        # No sorting in Phase 2b; header click is a no-op so the cell
        # selection doesn't accidentally trigger toggle. Future phase
        # could implement column sort here.
        pass

    # Key bindings on the DataTable — Textual's `Binding` on App level
    # would intercept globally. Instead, we add bindings to the
    # DataTable widget by overriding `on_key` for the relevant table.
    # Simpler approach used here: action methods on the App with
    # priority key bindings constrained to focused widget via custom
    # routing. Textual idiomatic path: `key_e` / `key_c` / `key_t` on
    # the DataTable subclass, but we don't subclass DataTable to keep
    # the changeset focused.

    def on_key(self, event) -> None:
        """Key router: e/c/t/Enter actions on Events tab tables.

        Only fires when one of the Events tables is focused. Returning
        early without consuming the event lets Textual dispatch the
        keystroke to default handlers — important so global bindings
        (q/r/1-7) still work when an Events table is focused.
        """
        if not self._outer_is_events:
            return
        focused = self.focused
        if focused is None:
            return
        fid = getattr(focused, "id", "") or ""
        if fid == "events-subs-table":
            if event.key == "e":
                event.stop()
                self._toggle_selected_sub()
            elif event.key == "c":
                event.stop()
                row = self._selected_sub_row()
                if row is not None:
                    self._copy_to_clipboard_safe(
                        row["sub_uuid"], "sub_uuid",
                    )
            elif event.key == "t":
                event.stop()
                row = self._selected_sub_row()
                if row is not None:
                    self._copy_to_clipboard_safe(
                        row["topic_pattern"], "topic",
                    )
        elif fid == "events-cat-table":
            if event.key == "e":
                event.stop()
                self._toggle_selected_event()
            elif event.key == "c":
                event.stop()
                row = self._selected_event_row()
                if row is not None:
                    self._copy_to_clipboard_safe(row["topic"], "topic")

    # ── Events catalogue ────────────────────────────────────────────

    def _get_cat_filter_state(self) -> dict:
        state = {"plugin": "__all__", "topic": "", "enabled_only": False}
        try:
            state["plugin"] = str(self.query_one(
                "#events-cat-filter-plugin", Select,
            ).value or "__all__")
        except (NoMatches, Exception):
            pass
        try:
            state["topic"] = self.query_one(
                "#events-cat-filter-topic", Input,
            ).value
        except NoMatches:
            pass
        try:
            state["enabled_only"] = self.query_one(
                "#events-cat-filter-enabled-only", Checkbox,
            ).value
        except NoMatches:
            pass
        return state

    def _event_passes_filter(self, row: dict, flt: dict) -> bool:
        if flt["enabled_only"] and not row["enabled"]:
            return False
        if flt["plugin"] != "__all__" and row["plugin"] != flt["plugin"]:
            return False
        if flt["topic"]:
            needle = flt["topic"].lower()
            if needle not in str(row["topic"]).lower():
                return False
        return True

    def _populate_cat_plugin_filter_options(self, rows: list) -> None:
        """Refresh the catalogue's Plugin select options. Same
        `prevent(Select.Changed)` rebuild pattern as the Subs browser —
        see `_populate_subs_plugin_filter_options` for rationale.
        """
        try:
            sel = self.query_one("#events-cat-filter-plugin", Select)
        except NoMatches:
            return
        plugin_names = sorted({r["plugin"] for r in rows})
        options = [("All plugins", "__all__")] + [
            (n, n) for n in plugin_names
        ]
        current = str(sel.value or "__all__")
        with self.prevent(Select.Changed):
            try:
                sel.set_options(options)
            except Exception:
                return
            if current == "__all__" or current in plugin_names:
                sel.value = current
            else:
                sel.value = "__all__"

    @work(thread=False, exclusive=True, group="events-cat")
    async def _refresh_events_catalogue_worker(self) -> None:
        """Rebuild the Events catalogue table from `plugin.events`.

        Iterates `dict(pc.plugins).items()` — snapshot to avoid
        cross-thread RuntimeError if plugins mutate during iteration.
        Only enabled plugins are surfaced (matches the dispatch path —
        events on a disabled plugin can never fire anyway).
        """
        pc = self.plugin_core
        try:
            plugins_snapshot = list(pc.plugins.items())
        except RuntimeError:
            plugins_snapshot = []

        rows = []
        for name, plugin in plugins_snapshot:
            if not getattr(plugin, "enabled", False):
                continue
            events_dict = getattr(plugin, "events", None) or {}
            if not isinstance(events_dict, dict):
                continue
            description = getattr(plugin, "description", "") or ""
            for event_id, entry in events_dict.items():
                if not isinstance(entry, dict):
                    continue
                rows.append({
                    "plugin": name,
                    "event_id": event_id,
                    "topic": entry.get("topic", ""),
                    "hosts": entry.get("hosts"),
                    "blocked_hosts": entry.get("blocked_hosts"),
                    "enabled": bool(entry.get("enabled", True)),
                    "description": description,
                })

        self._populate_cat_plugin_filter_options(rows)
        flt = self._get_cat_filter_state()
        visible = [r for r in rows if self._event_passes_filter(r, flt)]

        try:
            table = self.query_one("#events-cat-table", DataTable)
        except NoMatches:
            return
        table.clear()
        self._cat_rendered_rows = visible
        for r in visible:
            # Topic cell: `_render_topic_with_placeholders` already
            # applies `rich.markup.escape` to the topic body before
            # wrapping `{var}` placeholders in yellow tags — so the
            # output is safe to interpret as markup via
            # `Text.from_markup`.
            topic_render = self._render_topic_with_placeholders(
                self._truncate_cell(r["topic"], 24)
            )
            hosts_display = (
                "default" if r["hosts"] is None
                else self._truncate_cell(r["hosts"], 12)
            )
            enabled_cell = (
                "[green]on[/]" if r["enabled"] else "[dim]off[/]"
            )
            topic_text = Text.from_markup(topic_render)
            enabled_text = Text.from_markup(enabled_cell)
            # `hosts_display` comes from user-controlled plugin config —
            # wrap to defuse accidental `[`/`]`. Plugin name + event ID
            # are validated identifiers so they're safe as plain strings
            # (default_cell_formatter wraps them itself).
            table.add_row(
                self._truncate_cell(r["plugin"], 16),
                self._truncate_cell(r["event_id"], 16),
                topic_text,
                Text(hosts_display),
                enabled_text,
            )
        try:
            self.query_one("#events-cat-counter", Static).update(
                f"Showing {len(visible)} / {len(rows)}"
            )
        except NoMatches:
            pass

    @on(Input.Changed, "#events-cat-filter-topic")
    def _on_cat_filter_topic_changed(self, event: Input.Changed) -> None:
        self._refresh_events_catalogue_worker()

    @on(Select.Changed, "#events-cat-filter-plugin")
    def _on_cat_filter_plugin_changed(self, event: Select.Changed) -> None:
        self._refresh_events_catalogue_worker()

    @on(Checkbox.Changed, "#events-cat-filter-enabled-only")
    def _on_cat_filter_enabled_only_changed(self, event: Checkbox.Changed) -> None:
        self._refresh_events_catalogue_worker()

    @on(DataTable.RowSelected, "#events-cat-table")
    def _on_cat_row_selected(self, event: DataTable.RowSelected) -> None:
        self._open_event_detail_modal()

    def _selected_event_row(self) -> Optional[dict]:
        rows = getattr(self, "_cat_rendered_rows", []) or []
        try:
            table = self.query_one("#events-cat-table", DataTable)
        except NoMatches:
            return None
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(rows):
            return None
        return rows[idx]

    def _open_event_detail_modal(self) -> None:
        row = self._selected_event_row()
        if row is None:
            return
        if any(isinstance(s, EventDetailScreen) for s in self.screen_stack):
            return
        # Pre-render the topic with placeholders so the modal's
        # `topic_rendered` field carries the markup; the rest of the
        # fields are plain strings (markup escape applied by the modal).
        modal_row = dict(row)
        modal_row["topic_rendered"] = self._render_topic_with_placeholders(
            row["topic"]
        )
        self.push_screen(EventDetailScreen(event_dict=modal_row))

    @work(thread=False, exclusive=True, group="events-cat-toggle")
    async def _toggle_selected_event(self) -> None:
        row = self._selected_event_row()
        if row is None:
            return
        plugin_name = row["plugin"]
        event_id = row["event_id"]
        current = bool(row["enabled"])
        new_value = not current
        try:
            result = await self._run_on_main(
                self.plugin_core.set_event_enabled(
                    plugin_name, event_id, new_value,
                )
            )
        except asyncio.TimeoutError:
            self.notify("Toggle timed out (30s)", severity="warning")
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.notify(f"Toggle failed: {e}", severity="error")
            return
        if result is None:
            self.notify(
                "Cannot toggle — main loop unavailable",
                severity="warning",
            )
            return
        if result is False:
            self.notify(
                f"Event {plugin_name}/{event_id} no longer exists",
                severity="warning",
            )
            return
        # True → either toggled (emit fires + observer triggers refresh)
        # or no-op. Local view + new_value should differ; nothing else
        # to do here.

    # ── Live-stream ─────────────────────────────────────────────────

    # Topic-raw → checkbox-id mapping used by the live filter. The
    # 6 visible labels collapse to 5 source topics because
    # `_core/event/streamed` discriminates on `phase` between
    # `» first` (cyan) and `« end` (dim cyan). Both checkboxes
    # share the same source topic but apply different phase filters.
    _LIVE_TYPE_FILTERS = {
        "_core/event/published": ("events-live-type-pub", None),
        "_core/event/requested": ("events-live-type-req", None),
        "_core/event/streamed:first_chunk": (
            "events-live-type-first", None,
        ),
        "_core/event/streamed:ended": ("events-live-type-end", None),
        "_core/subscription/state_changed": (
            "events-live-type-sub", None,
        ),
        "_core/event/state_changed": (
            "events-live-type-evt", None,
        ),
        # `stream:unknown` (defensive fallback for future stream
        # phases — plan cycle 1 L1) deliberately has no checkbox.
        # Cycle 1 review HIGH-2 fix: was mapped to the `first_chunk`
        # key which caused unknown rows to be hidden when the operator
        # unticked `first`, defeating the diagnostic visibility of
        # the fallback. Keyed to a checkbox-less id so `.get(key,
        # (None, None))[0]` returns None and the row stays visible
        # regardless of checkbox state.
        "_core/event/streamed:unknown": ("_no_checkbox_", None),
    }

    def _get_live_filter_state(self) -> dict:
        state = {
            "topic": "",
            "publisher": "",
            "types": {
                "events-live-type-pub": True,
                "events-live-type-req": True,
                "events-live-type-first": True,
                "events-live-type-end": True,
                "events-live-type-sub": True,
                "events-live-type-evt": True,
            },
        }
        try:
            state["topic"] = self.query_one(
                "#events-live-filter-topic", Input,
            ).value
        except NoMatches:
            pass
        try:
            state["publisher"] = self.query_one(
                "#events-live-filter-publisher", Input,
            ).value
        except NoMatches:
            pass
        for cid in state["types"]:
            try:
                state["types"][cid] = self.query_one(
                    f"#{cid}", Checkbox,
                ).value
            except NoMatches:
                pass
        return state

    @staticmethod
    def _live_row_type_filter_key(row: dict) -> str:
        """Convert a row's `topic_raw` (+ phase if streamed) into the
        filter-key used by `_LIVE_TYPE_FILTERS`. Keeps the dispatch
        keyed on the same string the type-label was derived from.

        Cycle 2 fresh-eyes fix: discriminates on the raw ``phase``
        field carried in the row dict (set by
        `plugin._classify_and_normalize`) instead of substring-
        matching the Rich-markup `type_label`. The label is a UI
        string (`"[cyan]» first[/]"` etc.) and substring matches
        like `"first" in label` happen to work today but would
        silently misclassify if the markup glyphs ever change.

        Cycle 1 review HIGH-2 fix: the unknown-phase fallback
        returns ``stream:unknown`` (which has no checkbox in
        ``_LIVE_TYPE_FILTERS``) instead of being silently grouped
        with ``first_chunk``. Operator unchecking ``first`` no
        longer hides ``stream:unknown`` diagnostic rows.
        """
        topic_raw = row.get("topic_raw", "")
        if topic_raw == "_core/event/streamed":
            phase = row.get("phase")
            if phase == "first_chunk":
                return "_core/event/streamed:first_chunk"
            if phase == "ended":
                return "_core/event/streamed:ended"
            return "_core/event/streamed:unknown"
        return topic_raw

    def _live_row_passes_filter(self, row: dict, flt: dict) -> bool:
        # Type checkbox.
        key = self._live_row_type_filter_key(row)
        type_id = self._LIVE_TYPE_FILTERS.get(key, (None, None))[0]
        if type_id is not None and not flt["types"].get(type_id, True):
            return False
        if flt["topic"]:
            needle = flt["topic"].lower()
            if needle not in str(row.get("topic", "")).lower():
                return False
        if flt["publisher"]:
            needle = flt["publisher"].lower()
            if needle not in str(row.get("publisher", "")).lower():
                return False
        return True

    def _flush_live_events(self) -> None:
        """100ms tick: pull new rows from the plugin, append filtered
        ones to the table.

        Gated on `_live_visible` so events queued in the plugin deque
        DON'T render against an inactive tab — when the outer/inner
        edge flips back to True, the catch-up flush fires immediately
        (handled by the activation handlers, not by this method).

        Filter-dirty flag triggers a FULL re-render against the entire
        cached row list — without this, changing a Type checkbox
        wouldn't hide already-rendered rows.
        """
        live_visible = self._outer_is_events and self._inner_is_live
        if not live_visible:
            # Skip render while Live-stream is hidden. We deliberately
            # do NOT advance `_live_last_seen_seq` here — the
            # tab-activation handlers fire `_flush_live_events()`
            # directly on the False→True visibility edge as a
            # catch-up flush, which re-fetches all events since the
            # cursor was last advanced. Advancing here would cause
            # those accumulated events to be silently dropped.
            return

        plugin = self.plugin_instance
        if plugin is None or not hasattr(plugin, "get_live_events_since"):
            return

        try:
            new_rows, current_seq = plugin.get_live_events_since(
                self._live_last_seen_seq
            )
        except Exception:
            return
        self._live_last_seen_seq = current_seq

        if not new_rows and not self._live_filter_dirty:
            return

        # Append new rows to the cached list; trim to deque maxlen so
        # the cached list doesn't grow unbounded vs the plugin's deque.
        if new_rows:
            self._live_rendered_rows.extend(new_rows)
            # Plugin deque cap is 1000; mirror here.
            if len(self._live_rendered_rows) > 1000:
                self._live_rendered_rows = self._live_rendered_rows[-1000:]

        flt = self._get_live_filter_state()
        if self._live_filter_dirty or new_rows:
            try:
                table = self.query_one("#events-live-table", DataTable)
            except NoMatches:
                return
            visible = [
                r for r in self._live_rendered_rows
                if self._live_row_passes_filter(r, flt)
            ]
            table.clear()
            for r in visible:
                ts = float(r.get("ts", 0.0) or 0.0)
                time_cell = ""
                if ts > 0:
                    try:
                        time_cell = datetime.fromtimestamp(ts).strftime(
                            "%H:%M:%S.%f"
                        )[:-3]
                    except (OSError, ValueError):
                        time_cell = "?"
                # Type cell renders Rich markup (color glyphs) — those
                # strings come from the plugin's own static dispatch
                # table so markup is intentional.
                type_text = Text.from_markup(r.get("type_label", ""))
                # `topic` can be plugin-author-written and the topic
                # validator doesn't reject `[`/`]` characters — wrap in
                # plain `Text(...)` so the literal text renders instead
                # of attempting markup interpretation. `publisher` is
                # always a validated plugin_name (identifier-only) and
                # `detail` is locally formatted by `_detail_for` — both
                # safe as plain strings.
                topic_text = Text(
                    self._truncate_cell(r.get("topic", ""), 24)
                )
                publisher_cell = self._truncate_cell(
                    r.get("publisher", ""), 16,
                )
                detail_cell = self._truncate_cell(r.get("detail", ""), 40)
                table.add_row(
                    time_cell,
                    type_text,
                    topic_text,
                    publisher_cell,
                    detail_cell,
                )
            try:
                self.query_one("#events-live-counter", Static).update(
                    f"Showing {len(visible)} / "
                    f"{len(self._live_rendered_rows)} events"
                )
            except NoMatches:
                pass
            self._live_filter_dirty = False

    @on(Input.Changed, "#events-live-filter-topic")
    @on(Input.Changed, "#events-live-filter-publisher")
    def _on_live_filter_input_changed(self, event: Input.Changed) -> None:
        # Same-thread mutation; no lock needed. Next 100ms tick reads +
        # clears the flag.
        self._live_filter_dirty = True

    @on(Checkbox.Changed, "#events-live-type-pub")
    @on(Checkbox.Changed, "#events-live-type-req")
    @on(Checkbox.Changed, "#events-live-type-first")
    @on(Checkbox.Changed, "#events-live-type-end")
    @on(Checkbox.Changed, "#events-live-type-sub")
    @on(Checkbox.Changed, "#events-live-type-evt")
    def _on_live_type_checkbox_changed(self, event: Checkbox.Changed) -> None:
        self._live_filter_dirty = True

    @on(Button.Pressed, "#events-live-clear-btn")
    def _on_live_clear_pressed(self, event: Button.Pressed) -> None:
        """Atomic clear: wipe the plugin deque AND advance our cursor
        to the new high-water mark in ONE plugin-side lock acquisition.

        Without the cursor-resync under the same lock, a concurrent
        `_on_bus_event` between `clear_live_events()` and a separate
        seq read would leave the new event in the deque with a seq
        equal to our resync value — the next flush would skip it
        forever. The plugin method returns the post-clear seq so we
        can resync atomically.
        """
        plugin = self.plugin_instance
        if plugin is None or not hasattr(plugin, "clear_live_events"):
            return
        try:
            new_cursor = plugin.clear_live_events()
        except Exception:
            return
        if isinstance(new_cursor, int):
            self._live_last_seen_seq = new_cursor
        self._live_rendered_rows = []
        try:
            table = self.query_one("#events-live-table", DataTable)
            table.clear()
        except NoMatches:
            pass
        try:
            self.query_one("#events-live-counter", Static).update(
                "Showing 0 / 0 events"
            )
        except NoMatches:
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
            # Phase 3b — UNLOADED / FAILED_LOAD plugins lack a live
            # instance in `pc.plugins` (per `PluginCore.py:2466-2474`,
            # the assignment only runs on the success path). When the
            # plugin still has a `plugin_states` entry, fall through
            # to the generated view with `plugin=None` so the per-plugin
            # Lifecycle / Events / Subs / Logger sections render their
            # "(plugin not loaded)" placeholders. The endpoint loop
            # also tolerates `plugin=None` (defaults to `{}`).
            plugin_states = getattr(self.plugin_core, "plugin_states", None)
            if isinstance(plugin_states, dict) and plugin_name in plugin_states:
                _log.debug(
                    "[%s] plugin instance missing — rendering Phase 3b "
                    "placeholders via generated view (state-only)",
                    plugin_name,
                )
                return self._auto_generate_plugin_view(
                    plugin_name, None, has_bar=has_bar,
                )
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

        # ── Phase 3b — four new sections between title Static and Rule
        # (plan Section 4.4 insertion order: Lifecycle strip → Events →
        # Subs → Logger overrides → Rule → endpoints).
        widgets.append(self._build_lifecycle_strip(plugin_name, plugin))
        widgets.append(self._build_per_plugin_events_section(plugin_name, plugin))
        widgets.append(self._build_per_plugin_subs_section(plugin_name, plugin))
        widgets.append(self._build_per_plugin_logger_section(plugin_name, plugin))
        # Runtime-subs + logger-list population are scheduled by
        # `_finalize_per_plugin_phase3b_sections` from `open_plugin_tab`
        # / `_do_switch_view_mode` AFTER the mount loop completes —
        # call_after_refresh inside the builder fired too early
        # (callback ran before trailing widgets mounted).

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

        # Phase 3b — populate the runtime subs DataTable + logger
        # overrides list AFTER the mount loop completes. Builders
        # can't reliably schedule this themselves; call_after_refresh
        # from inside the builder fires before trailing widgets mount.
        if mode == "generated":
            self._finalize_per_plugin_phase3b_sections(plugin_name)

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
            # ── Phase 3b — per-plugin tab button routing ──────────────
            elif t == "per-plugin-copy-uuid":
                self._handle_per_plugin_copy_uuid(entry["plugin"])
            elif t == "per-plugin-jump-events":
                self._jump_to_events_catalogue_for_plugin(entry["plugin"])
            elif t == "per-plugin-jump-subs":
                self._jump_to_subs_browser_for_plugin(entry["plugin"])
            elif t == "per-plugin-logger-apply":
                self._handle_per_plugin_logger_apply(entry["plugin"])
            elif t == "per-plugin-logger-clear":
                self._handle_per_plugin_logger_clear(
                    entry["plugin"], entry.get("endpoint", ""),
                )
            elif t == "per-plugin-logger-refresh":
                self._handle_per_plugin_logger_refresh(entry["plugin"])
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

        # Phase 3b — populate the post-mount Phase 3b sections only
        # when we're switching INTO the generated view.
        if new_mode == "generated":
            self._finalize_per_plugin_phase3b_sections(plugin_name)

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
        if not entry:
            return
        t = entry.get("type")
        if t == "menu-toggle":
            self._execute_toggle(entry, event.value)
        elif t == "per-plugin-verbose":
            # Phase 3b — direct attr write on the loop-bound Plugin
            # instance (plan Section 4.4 cycle-1 LOW fix #6: no lock
            # needed; GIL-safe; faster than `_run_on_main` bridge).
            plugin_name = entry.get("plugin", "")
            target = self.plugin_core.plugins.get(plugin_name)
            if target is not None:
                try:
                    target.verbose_notifier = bool(event.value)
                except Exception:
                    pass

    # ── Phase 2b — Events tab activation handlers ─────────────────
    # The existing undecorated `on_tabbed_content_tab_activated` below
    # stays as a catch-all for the config-dirty banner. These two
    # decorated handlers fire IN ADDITION (selector-filtered to specific
    # `TabbedContent` instances) so the outer-vs-inner activation logic
    # for the Events tab doesn't pollute the catch-all body.
    #
    # Defensive `event.tabbed_content.id` check inside each handler:
    # Textual's `@on(..., "#id")` decorator selector should already
    # filter, but the Phase 4b regression (where nested TabbedContent
    # events bubbled up to an outer handler) is the precedent for the
    # belt-and-suspenders guard.

    @on(TabbedContent.TabActivated, "#main-tabs")
    def _on_main_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        if event.tabbed_content.id != "main-tabs":
            return
        pane_id = event.pane.id or ""
        was_events = self._outer_is_events
        self._outer_is_events = pane_id == "tab-events"
        if self._outer_is_events:
            # Immediate refresh of whichever Events sub-tab is active so
            # the operator sees fresh data instead of stale rows on
            # tab-enter. Live-stream catch-up flush handled by
            # `_flush_live_events` via the (False -> True) edge below.
            self._refresh_subs_browser_worker()
            self._refresh_events_catalogue_worker()
        if not was_events and self._outer_is_events:
            # Edge False -> True: trigger one immediate Live-stream flush
            # if Live-stream is the active inner tab (so the operator
            # doesn't see a stale table waiting on the next 100ms tick).
            if self._inner_is_live:
                self._flush_live_events()

    @on(TabbedContent.TabActivated, "#events-tabs")
    def _on_events_inner_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        if event.tabbed_content.id != "events-tabs":
            return
        pane_id = event.pane.id or ""
        was_live = self._inner_is_live
        self._inner_is_live = pane_id == "events-tab-live"
        # Immediate refresh of the just-activated inner tab.
        if pane_id == "events-tab-subs":
            self._refresh_subs_browser_worker()
        elif pane_id == "events-tab-cat":
            self._refresh_events_catalogue_worker()
        if not was_live and self._inner_is_live:
            # Edge False -> True for Live-stream — immediate catch-up
            # flush. Mirrors the outer-tab edge handler above.
            self._flush_live_events()

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

    # Phase 3a — section ids for the detail-pane Collapsibles. Kept as
    # module-class constants so the worker, the build helpers, and the
    # tests share one source of truth (test #10 asserts the exact ids).
    _DETAIL_SECTION_INFO = "plugin-detail-info"
    _DETAIL_SECTION_ENDPOINTS = "plugin-detail-endpoints"
    _DETAIL_SECTION_EVENTS = "plugin-detail-events"
    _DETAIL_SECTION_SUBS = "plugin-detail-subs"
    _DETAIL_SECTION_ARGS = "plugin-detail-args"
    _DETAIL_SECTION_IDS = (
        _DETAIL_SECTION_INFO,
        _DETAIL_SECTION_ENDPOINTS,
        _DETAIL_SECTION_EVENTS,
        _DETAIL_SECTION_SUBS,
        _DETAIL_SECTION_ARGS,
    )
    _DETAIL_NON_INFO_SECTIONS = (
        _DETAIL_SECTION_ENDPOINTS,
        _DETAIL_SECTION_EVENTS,
        _DETAIL_SECTION_SUBS,
        _DETAIL_SECTION_ARGS,
    )

    @work(thread=False, exclusive=True, group="plugin-detail")
    async def _update_plugin_detail(self, plugin_name: str) -> None:
        """Phase 3a — rebuild the 5-section detail pane for ``plugin_name``.

        Behaviour:

          * Captures the previously-open non-Info sections from the
            currently-mounted Collapsibles BEFORE destroying them, so
            the operator's section open state survives across rapid
            row-selection changes (the `@work(exclusive=True)` group
            cancels in-flight invocations, so capture-then-clear has
            no race with itself).
          * Records ``plugin_name`` as the currently displayed plugin
            so `_on_plugin_state_changed` can direct-refresh on a
            state transition for this plugin.
          * Reads framework state (Plugin instance + `plugin_states`
            entry) directly — does NOT call ``get_plugin_info`` /
            ``get_plugin_endpoints`` (those return None for UNLOADED
            and incomplete data for FAILED_LOAD; the on-disk fallback
            via `_resolve_plugin_config_dict` covers both cases).

        UNLOADED / FAILED_LOAD plugins fall back to reading the on-disk
        ``plugin_config.yml`` for Endpoints/Events/Subs/Args content via
        `_resolve_plugin_config_dict`. The Info section reports state +
        (for FAILED_LOAD) the captured `last_errors[Phase.LOAD]`
        exception and traceback.
        """
        # Snapshot live framework state. Both lookups are GIL-atomic dict
        # reads — no `_run_on_main` needed (no awaits, no asyncio locks
        # acquired). pc.plugin_states.get() is a `.get(name)` on a
        # standard dict.
        plugin = None
        try:
            plugin = self.plugin_core.plugins.get(plugin_name)
        except Exception:
            plugin = None
        ps = None
        try:
            states = getattr(self.plugin_core, "plugin_states", None) or {}
            ps = states.get(plugin_name) if hasattr(states, "get") else None
        except Exception:
            ps = None

        # Locate the container. Failure here means the Plugins tab hasn't
        # been mounted yet — nothing to render against. Worker re-fires
        # on the next row-highlight after the tab mounts. NOTE: we do
        # NOT set `_currently_displayed_plugin` until AFTER this resolves
        # — otherwise an observer-driven re-render against an unmounted
        # pane would re-schedule itself forever via the `name ==
        # _currently_displayed_plugin` direct-call gate in
        # `_on_plugin_state_changed` (cycle 2 fresh-eyes finding).
        try:
            scroll = self.query_one("#plugin-detail", VerticalScroll)
        except NoMatches:
            return

        # Pane is real — commit the displayed-plugin tracker for the
        # observer's direct-refresh gate.
        self._currently_displayed_plugin = plugin_name

        # Capture the previously-open non-Info section state for THIS
        # plugin BEFORE removing children. The Info section's collapsed
        # state isn't tracked — Info always re-opens after a refresh per
        # Section 4.3 of the plan.
        try:
            prior_open: set = set()
            prior_total = 0
            for col in scroll.query(Collapsible):
                col_id = getattr(col, "id", None)
                if col_id is None or col_id not in self._DETAIL_SECTION_IDS:
                    continue
                prior_total += 1
                if col_id in self._DETAIL_NON_INFO_SECTIONS and not col.collapsed:
                    prior_open.add(col_id)
            # The captured snapshot is only meaningful for the plugin
            # whose detail was previously displayed AND when the prior
            # render actually populated the scroll. Three guards:
            #   1. Coalesce a pre-initialized-to-None
            #      `_detail_render_target` (`_make_dashboard_app` test
            #      fixture path) to plugin_name. `getattr(..., default)`
            #      doesn't fire when the attribute exists with value
            #      None.
            #   2. Skip the save when prior_plugin == plugin_name —
            #      a same-plugin re-render (e.g. `_on_plugin_state_changed`
            #      direct-refresh while a prior worker is mid-`await
            #      scroll.remove_children()`) must not overwrite its
            #      own saved open-state set with the empty set Worker2
            #      observes post-remove.
            #   3. Skip the save when prior_total == 0 — Worker2 observed
            #      zero detail-pane Collapsibles, meaning the prior
            #      worker was cancelled BEFORE mounting (during the
            #      `await remove_children()`) and our `prior_open=={}`
            #      capture is an artifact of cancellation, not a
            #      legitimate "all collapsed" observation. Saving the
            #      empty set here would clobber prior_plugin's
            #      legitimate saved state from an earlier session.
            prior_plugin = getattr(self, "_detail_render_target", None) or plugin_name
            same_plugin_rerender = (prior_plugin == plugin_name)
            if (
                not same_plugin_rerender
                and prior_total > 0
                and (prior_open or prior_plugin in self._plugin_detail_open_sections)
            ):
                self._plugin_detail_open_sections[prior_plugin] = prior_open
        except Exception:
            pass

        # Pin the new render target for the NEXT invocation's capture.
        self._detail_render_target = plugin_name

        # Determine open-state for non-Info sections on THIS plugin from
        # the persisted dict. Info is always force-open.
        saved_open = self._plugin_detail_open_sections.get(plugin_name, set())

        # Drop registry entries for any prior detail-pane button on this
        # plugin so a long-running TUI doesn't leak ids across re-renders.
        try:
            self._cleanup_registry_for_plugin(
                plugin_name, exclude_types=frozenset({"close-tab", "view-mode-custom", "view-mode-generated"}),
            )
        except Exception:
            pass

        try:
            await scroll.remove_children()
        except Exception:
            return

        # Read on-disk plugin_config.yml once — Args section needs it for
        # the overrides-applied badge AND the UNLOADED/FAILED_LOAD paths
        # need it for endpoints/events/subs fallback rendering. Sync read
        # is fine: small file, cached by OS.
        on_disk_cfg = self._resolve_plugin_config_dict(plugin_name)

        # Build the five sections.
        sections = [
            self._build_info_section(plugin_name, plugin, ps, on_disk_cfg),
            self._build_endpoints_section(plugin_name, plugin, on_disk_cfg, saved_open),
            self._build_events_section(plugin_name, plugin, on_disk_cfg, saved_open),
            self._build_subs_section(plugin_name, plugin, on_disk_cfg, saved_open),
            self._build_args_section(plugin_name, plugin, on_disk_cfg, saved_open),
        ]
        try:
            for sec in sections:
                await scroll.mount(sec)
        except Exception:
            # Container destroyed mid-mount (e.g. plugin tab popped). Swallow.
            return

    # ── Phase 3a — detail-pane section builders ────────────────────────

    def _build_info_section(
        self, plugin_name: str, plugin, ps, on_disk_cfg: Optional[dict],
    ) -> Collapsible:
        """Info section — name / uuid / version / description / remote / phase.

        For FAILED_LOAD plugins: also surfaces the captured exception
        (type + message) and a short slice of the traceback. For
        UNLOADED plugins (config-has-entry-but-no-instance): reads
        version/description from on-disk yaml.

        Includes request stats + active request count (carried over from
        pre-Phase-3 detail pane — preserves existing UX per maintainer
        decision logged in conversation thread).
        """
        # Source of metadata fields per state.
        version = "?"
        description = ""
        remote = False
        uuid_str = "?"
        if plugin is not None:
            version = str(getattr(plugin, "version", "?"))
            description = str(getattr(plugin, "description", "") or "")
            remote = bool(getattr(plugin, "remote", False))
            uuid_str = str(getattr(plugin, "plugin_uuid", "?"))
        elif isinstance(on_disk_cfg, dict):
            version = str(on_disk_cfg.get("version") or "?")
            description = str(on_disk_cfg.get("description") or "")
            remote = bool(on_disk_cfg.get("remote") or False)

        phase_label, phase_class = self._plugin_phase(plugin_name, plugin)

        body: list = []
        body.append(Static(
            f"[bold]{escape(plugin_name)}[/bold]  "
            f"v{escape(version)}",
            markup=True,
        ))
        body.append(Static(
            f"[dim]UUID:[/dim] {escape(uuid_str)}",
            markup=True,
        ))
        # Phase pill rendered using the same css class the table uses
        # (`stat-val-good` / `stat-val-warn` / `stat-val-bad` / `phase-dim`).
        body.append(Static(
            f"[dim]Phase:[/dim] [bold]{escape(phase_label)}[/bold]",
            markup=True, classes=phase_class,
        ))
        if description:
            body.append(Static(escape(description), markup=True))
        body.append(Static(
            f"[dim]Remote:[/dim] {'Yes' if remote else 'No'}",
            markup=True,
        ))

        # FAILED_LOAD: surface last_errors[Phase.LOAD].
        if ps is not None and getattr(getattr(ps, "state", None), "value", None) == "failed_load":
            err_rec = None
            try:
                last_errors = getattr(ps, "last_errors", None) or {}
                if _PluginPhase is not None:
                    err_rec = last_errors.get(_PluginPhase.LOAD)
                else:
                    # Tests may pass a stubbed dict keyed by the string
                    # value of the Phase enum. Tolerant fallback.
                    err_rec = (
                        last_errors.get("load")
                        if isinstance(last_errors, dict) else None
                    )
            except Exception:
                err_rec = None
            if err_rec is not None:
                exc = getattr(err_rec, "exception", None)
                exc_type = type(exc).__name__ if exc is not None else "Exception"
                exc_msg = str(exc) if exc is not None else ""
                body.append(Static(
                    f"[bold]FAILED_LOAD:[/bold] "
                    f"{escape(exc_type)}: {escape(exc_msg)}",
                    markup=True, classes="plugin-detail-failed-banner",
                ))
                tb = getattr(err_rec, "traceback", None)
                if tb:
                    # Traceback content can be long — wrap in an inner
                    # Collapsible so it doesn't blow up the Info section
                    # height on a deep stack.
                    body.append(Collapsible(
                        Static(escape(str(tb)),
                               markup=True,
                               classes="plugin-detail-traceback"),
                        title="Show traceback",
                        collapsed=True,
                    ))

        # Request stats — preserved from pre-Phase-3 detail pane per
        # maintainer Option A decision.
        pstats = None
        try:
            pstats = self._tracker.per_plugin.get(plugin_name)
        except Exception:
            pstats = None
        if pstats is not None:
            try:
                body.append(Static(
                    f"[dim]Requests:[/dim] {pstats.total} total, "
                    f"{pstats.errors} errors, "
                    f"avg {pstats.avg_latency * 1000:.0f}ms",
                    markup=True,
                ))
            except Exception:
                pass
        try:
            active_for = [
                r for r in self._tracker.active if r.plugin == plugin_name
            ]
        except Exception:
            active_for = []
        if active_for:
            body.append(Static(
                f"[dim]Active:[/dim] {len(active_for)}",
                markup=True,
            ))

        return Collapsible(
            *body, title="Info",
            collapsed=False,  # Info always opens on refresh per Section 4.3
            id=self._DETAIL_SECTION_INFO,
            classes="plugin-detail-section-body",
        )

    def _build_endpoints_section(
        self,
        plugin_name: str,
        plugin,
        on_disk_cfg: Optional[dict],
        saved_open: set,
    ) -> Collapsible:
        """Endpoints section — list each endpoint with flags + tags.

        Live plugin: reads `plugin.endpoints` (validated dict). UNLOADED
        / FAILED_LOAD fallback: reads `on_disk_cfg["endpoints"]` (the
        un-merged YAML). Empty / missing → `(none)` placeholder.
        """
        endpoints_dict: Optional[dict] = None
        if plugin is not None:
            cand = getattr(plugin, "endpoints", None)
            if isinstance(cand, dict):
                endpoints_dict = cand
        if endpoints_dict is None and isinstance(on_disk_cfg, dict):
            cand = on_disk_cfg.get("endpoints")
            if isinstance(cand, dict):
                endpoints_dict = cand

        body: list = []
        if not endpoints_dict:
            body.append(Static("[dim](none)[/dim]", markup=True))
        else:
            for ep_key, ep in endpoints_dict.items():
                if not isinstance(ep, dict):
                    continue
                access_name = str(ep_key)
                internal_name = str(ep.get("internal_name") or access_name)
                remote_flag = "R" if ep.get("remote") else ""
                accessible_flag = "A" if ep.get("accessible_by_other_plugins") else ""
                flags = " ".join(f for f in (remote_flag, accessible_flag) if f)
                tags = ep.get("tags") or []
                tags_str = (
                    ", ".join(escape(str(t)) for t in tags) if tags else ""
                )
                line = (
                    f"[bold]{escape(access_name)}[/bold]"
                    + (f" → {escape(internal_name)}" if internal_name != access_name else "")
                    + (f"  [cyan]{escape(flags)}[/cyan]" if flags else "")
                    + (f"  [dim]tags:[/dim] {tags_str}" if tags_str else "")
                )
                body.append(Static(line, markup=True))

        return Collapsible(
            *body, title=f"Endpoints ({len(endpoints_dict or {})})",
            collapsed=(self._DETAIL_SECTION_ENDPOINTS not in saved_open),
            id=self._DETAIL_SECTION_ENDPOINTS,
            classes="plugin-detail-section-body",
        )

    def _build_events_section(
        self,
        plugin_name: str,
        plugin,
        on_disk_cfg: Optional[dict],
        saved_open: set,
    ) -> Collapsible:
        """Events section — declared events with topic + hosts + enabled.

        Live plugin: reads `plugin.events` (post-load-time placeholder
        resolution). Fallback: `on_disk_cfg["events"]`.
        """
        events_dict: Optional[dict] = None
        if plugin is not None:
            cand = getattr(plugin, "events", None)
            if isinstance(cand, dict):
                events_dict = cand
        if events_dict is None and isinstance(on_disk_cfg, dict):
            cand = on_disk_cfg.get("events")
            if isinstance(cand, dict):
                events_dict = cand

        body: list = []
        if not events_dict:
            body.append(Static("[dim](none)[/dim]", markup=True))
        else:
            for evt_id, entry in events_dict.items():
                if not isinstance(entry, dict):
                    continue
                topic = entry.get("topic", "")
                hosts = entry.get("hosts")
                enabled = entry.get("enabled", True)
                enabled_marker = "✓" if enabled else "✗"
                enabled_color = "green" if enabled else "red"
                hosts_str = "" if hosts is None else f"  [dim]hosts:[/dim] {escape(str(hosts))}"
                body.append(Static(
                    f"[bold]{escape(str(evt_id))}[/bold]  "
                    f"[yellow]{escape(str(topic))}[/yellow]"
                    f"{hosts_str}  "
                    f"[{enabled_color}]{enabled_marker}[/{enabled_color}]",
                    markup=True,
                ))

        return Collapsible(
            *body, title=f"Events ({len(events_dict or {})})",
            collapsed=(self._DETAIL_SECTION_EVENTS not in saved_open),
            id=self._DETAIL_SECTION_EVENTS,
            classes="plugin-detail-section-body",
        )

    def _build_subs_section(
        self,
        plugin_name: str,
        plugin,
        on_disk_cfg: Optional[dict],
        saved_open: set,
    ) -> Collapsible:
        """Subscriptions section — declared subs (with [YAML] tag) plus
        runtime subs (tagged [runtime]). Phase 3a renders both inside
        one section; Phase 3b will move richer per-plugin sub views to
        the dedicated per-plugin tab.
        """
        declared_dict: Optional[dict] = None
        if plugin is not None:
            cand = getattr(plugin, "subscriptions", None)
            if isinstance(cand, dict):
                declared_dict = cand
        if declared_dict is None and isinstance(on_disk_cfg, dict):
            cand = on_disk_cfg.get("subscriptions")
            if isinstance(cand, dict):
                declared_dict = cand

        # Runtime subs: derive from `plugin._sub_uuids` minus the declared
        # set. `_sub_uuids` is appended-to by both YAML registration AND
        # runtime subscribe; we only have the count, not per-sub detail
        # here (full detail lives in the per-plugin tab's Subs section
        # added in Phase 3b).
        runtime_count = 0
        if plugin is not None and isinstance(declared_dict, dict):
            try:
                sub_uuids = getattr(plugin, "_sub_uuids", []) or []
                runtime_count = max(
                    0, len(sub_uuids) - len(declared_dict),
                )
            except Exception:
                runtime_count = 0

        body: list = []
        if not declared_dict and runtime_count == 0:
            body.append(Static("[dim](none)[/dim]", markup=True))
        else:
            if declared_dict:
                for dec_id, entry in declared_dict.items():
                    if not isinstance(entry, dict):
                        continue
                    topic = entry.get("topic", "")
                    target = entry.get("target_access_name") or "?"
                    target_plugin = entry.get("target_plugin") or plugin_name
                    enabled = entry.get("enabled", True)
                    enabled_marker = "✓" if enabled else "✗"
                    enabled_color = "green" if enabled else "red"
                    body.append(Static(
                        f"[dim][YAML][/dim] "
                        f"[bold]{escape(str(dec_id))}[/bold]  "
                        f"[yellow]{escape(str(topic))}[/yellow]  "
                        f"→ {escape(str(target_plugin))}.{escape(str(target))}  "
                        f"[{enabled_color}]{enabled_marker}[/{enabled_color}]",
                        markup=True,
                    ))
            if runtime_count > 0:
                body.append(Static(
                    f"[dim][runtime][/dim] [bold]{runtime_count}[/bold] "
                    f"runtime subscription(s) registered. "
                    f"[dim](See per-plugin tab for details.)[/dim]",
                    markup=True,
                ))

        declared_count = len(declared_dict or {})
        title = (
            f"Subscriptions ({declared_count}"
            + (f"+{runtime_count}" if runtime_count > 0 else "")
            + ")"
        )
        return Collapsible(
            *body, title=title,
            collapsed=(self._DETAIL_SECTION_SUBS not in saved_open),
            id=self._DETAIL_SECTION_SUBS,
            classes="plugin-detail-section-body",
        )

    def _build_args_section(
        self,
        plugin_name: str,
        plugin,
        on_disk_cfg: Optional[dict],
        saved_open: set,
    ) -> Collapsible:
        """Args section — `plugin.arguments` rendered as YAML.

        Shows an "overrides applied" badge when the live merged
        `plugin.arguments` differs from the raw `on_disk_cfg["arguments"]`
        block. Detection is a shallow `==` against the YAML-loaded base —
        catches both deep-merge additions/replacements AND `__replace__:
        true` subtree replacements (the loader strips `__replace__`
        before storing, so the merged result already reflects the
        replacement). The `__replace__` distinction itself is deferred
        per Section 4.3 plan note — v1 just signals "something changed".
        """
        live_args = None
        if plugin is not None:
            live_args = getattr(plugin, "arguments", None)

        # Raw base args from on-disk config — needed for the badge.
        base_args = None
        if isinstance(on_disk_cfg, dict):
            base_args = on_disk_cfg.get("arguments")

        body: list = []

        # Overrides-applied badge: only renders when we have BOTH a live
        # plugin AND an on-disk base AND they differ. For UNLOADED/FAILED
        # plugins, live_args is None so we skip the badge (the badge is
        # meaningless without a merged result to compare).
        overrides_applied = (
            plugin is not None
            and isinstance(on_disk_cfg, dict)
            and live_args is not None
            and live_args != base_args
        )
        if overrides_applied:
            body.append(Static(
                "[italic]overrides applied[/italic]",
                markup=True, classes="plugin-detail-overrides-badge",
            ))

        # Args content — prefer live, fall back to on-disk base.
        rendered_args = live_args if live_args is not None else base_args

        if rendered_args is None or (
            isinstance(rendered_args, (dict, list)) and not rendered_args
        ):
            body.append(Static("[dim](none)[/dim]", markup=True))
        else:
            try:
                yaml_text = yaml.safe_dump(
                    rendered_args, default_flow_style=False, sort_keys=False,
                )
            except Exception:
                yaml_text = repr(rendered_args)
            body.append(Static(
                f"[dim]{escape(yaml_text)}[/dim]", markup=True,
            ))

        return Collapsible(
            *body, title="Args",
            collapsed=(self._DETAIL_SECTION_ARGS not in saved_open),
            id=self._DETAIL_SECTION_ARGS,
            classes="plugin-detail-section-body",
        )

    # ── Phase 3b — Per-plugin tab section builders ────────────────────

    def _per_plugin_widget_id(self, kind: str, plugin_name: str) -> str:
        """Deterministic widget ID for Phase 3b per-plugin tab widgets.

        Same plugin name yields the same suffix across re-renders so
        tests can locate widgets without inspecting the counter-driven
        `_id_registry`. Combines a fixed `kind` prefix with the
        sanitized plugin name (md5-suffixed, see `_sanitize_id`).
        """
        return f"{kind}-{self._sanitize_id(plugin_name)}"

    def _finalize_per_plugin_phase3b_sections(self, plugin_name: str) -> None:
        """Phase 3b — kick off post-mount population for the per-plugin
        tab's Phase 3b sections.

        Called from `open_plugin_tab` / `_do_switch_view_mode` AFTER the
        widget mount loop completes (call_after_refresh inside the
        builders fires too early when the trailing widgets are still
        being mounted). Triggers:

          * `_refresh_per_plugin_logger_list_for(plugin_name)` — sync;
            mounts per-prefix Horizontal rows or the empty Static
            placeholder into the overrides Vertical container.
          * `_populate_per_plugin_runtime_subs(plugin_name, uuid)` —
            sync wrapper around the async worker body
            (`_do_populate_per_plugin_runtime_subs`). Schedules the
            worker with a plugin-scoped exclusive group so cross-plugin
            invocations don't cancel each other. Worker awaits
            `list_local_subs()`, filters by plugin_uuid + declared_id,
            populates the runtime DataTable.

        Both helpers are no-ops when the section widgets aren't found
        (e.g. plugin is UNLOADED/FAILED_LOAD and the Logger Add row
        wasn't built, or the operator closed the tab between trigger
        and execute).
        """
        # Logger list — sync helper that mounts rows into the Vertical.
        try:
            self._refresh_per_plugin_logger_list_for(plugin_name)
        except Exception:
            pass

        # Runtime subs worker — skip for plugins without a live instance
        # (UNLOADED/FAILED_LOAD have no plugin_uuid to filter by).
        plugin = self.plugin_core.plugins.get(plugin_name)
        if plugin is None:
            return
        plugin_uuid = str(getattr(plugin, "plugin_uuid", "") or "")
        if not plugin_uuid:
            return
        try:
            self._populate_per_plugin_runtime_subs(plugin_name, plugin_uuid)
        except Exception:
            pass

    def _build_lifecycle_strip(self, plugin_name: str, plugin) -> Horizontal:
        """Phase 3b — Lifecycle strip at the top of the per-plugin tab.

        Six cells in a Horizontal `.lifecycle-strip` container:
        Phase pill, UUID short + Copy button, _lifecycle_ready ✓/✗,
        ready ✓/✗, verbose_notifier Switch, version.

        For UNLOADED / FAILED_LOAD plugins (plugin is None per the
        framework — `PluginCore.py:2466-2474` only assigns to
        `pc.plugins` on the success path), the strip renders best-effort
        cells: phase + version from state/on-disk-cfg fallback,
        readiness/uuid cells render `?`, no Copy / Switch widgets.
        """
        phase_label, phase_class = self._plugin_phase(plugin_name, plugin)
        phase_id = self._per_plugin_widget_id("per-plugin-phase", plugin_name)

        # UUID — first 8 hex chars + Copy button. For unloaded plugins,
        # plugin is None so render `?` without a Copy button.
        uuid_str = "?"
        version_str = "?"
        lifecycle_marker = "?"
        ready_marker = "?"
        verbose_value = False
        if plugin is not None:
            uuid_full = str(getattr(plugin, "plugin_uuid", "") or "")
            uuid_str = (uuid_full[:8] + "…") if len(uuid_full) > 8 else uuid_full or "?"
            version_str = str(getattr(plugin, "version", "?"))
            lcr = getattr(plugin, "_lifecycle_ready", None)
            rdy = getattr(plugin, "ready", None)
            if lcr is not None and hasattr(lcr, "is_set"):
                lifecycle_marker = "✓" if lcr.is_set() else "✗"
            if rdy is not None and hasattr(rdy, "is_set"):
                ready_marker = "✓" if rdy.is_set() else "✗"
            verbose_value = bool(getattr(plugin, "verbose_notifier", False))

        # Phase cell.
        phase_cell = Vertical(
            Static("[dim]Phase[/dim]", markup=True, classes="stat-key"),
            Static(phase_label, classes=phase_class, id=phase_id),
            classes="stat-card",
        )

        # UUID cell with Copy button.
        if plugin is not None:
            copy_id = self._make_id(
                "copy-uuid", plugin_name, "", "per-plugin-copy-uuid",
            )
            uuid_cell = Vertical(
                Static("[dim]UUID[/dim]", markup=True, classes="stat-key"),
                Horizontal(
                    Static(uuid_str, classes="stat-val"),
                    Button("Copy", id=copy_id, classes="per-plugin-copy-uuid"),
                ),
                classes="stat-card",
            )
        else:
            uuid_cell = Vertical(
                Static("[dim]UUID[/dim]", markup=True, classes="stat-key"),
                Static(uuid_str, classes="stat-val"),
                classes="stat-card",
            )

        # _lifecycle_ready cell.
        lcr_color = "green" if lifecycle_marker == "✓" else ("red" if lifecycle_marker == "✗" else "yellow")
        lcr_cell = Vertical(
            Static("[dim]lifecycle_ready[/dim]", markup=True, classes="stat-key"),
            Static(
                f"[{lcr_color}]{lifecycle_marker}[/{lcr_color}]",
                markup=True, classes="stat-val",
                id=self._per_plugin_widget_id("per-plugin-lifecycle-ready", plugin_name),
            ),
            classes="stat-card",
        )

        # ready cell.
        rdy_color = "green" if ready_marker == "✓" else ("red" if ready_marker == "✗" else "yellow")
        rdy_cell = Vertical(
            Static("[dim]ready[/dim]", markup=True, classes="stat-key"),
            Static(
                f"[{rdy_color}]{ready_marker}[/{rdy_color}]",
                markup=True, classes="stat-val",
                id=self._per_plugin_widget_id("per-plugin-ready", plugin_name),
            ),
            classes="stat-card",
        )

        # verbose_notifier Switch cell — only for loaded plugins (need
        # a target instance for the toggle handler to write to). The
        # Switch's `id` is the deterministic per-plugin widget id;
        # on_switch_changed looks up routing metadata via the id (not
        # the `name`), so we register the routing entry directly under
        # the deterministic id and skip `_make_id` for this widget
        # (would otherwise leave a dead registry entry that inflates
        # `_id_counter` without a resolvable widget — review fix).
        if plugin is not None:
            verbose_widget_id = self._per_plugin_widget_id(
                "per-plugin-verbose", plugin_name,
            )
            verbose_cell = Vertical(
                Static("[dim]verbose_notifier[/dim]", markup=True, classes="stat-key"),
                Switch(
                    value=verbose_value,
                    id=verbose_widget_id,
                    classes="per-plugin-verbose-switch",
                ),
                classes="stat-card",
            )
            self._id_registry[verbose_widget_id] = {
                "plugin": plugin_name,
                "endpoint": "",
                "type": "per-plugin-verbose",
            }
        else:
            verbose_cell = Vertical(
                Static("[dim]verbose_notifier[/dim]", markup=True, classes="stat-key"),
                Static("?", classes="stat-val"),
                classes="stat-card",
            )

        # Version cell. `version_str` comes from plugin_config.yml which
        # is free-form — escape against accidental markup syntax in the
        # config (Cycle 2 fresh-eyes LOW finding; matches the existing
        # pattern at `_auto_generate_plugin_view`'s title Static).
        ver_cell = Vertical(
            Static("[dim]Version[/dim]", markup=True, classes="stat-key"),
            Static(escape(version_str), classes="stat-val"),
            classes="stat-card",
        )

        return Horizontal(
            phase_cell, uuid_cell, lcr_cell, rdy_cell, verbose_cell, ver_cell,
            classes="lifecycle-strip",
            id=self._per_plugin_widget_id("lifecycle-strip", plugin_name),
        )

    def _build_per_plugin_events_section(
        self, plugin_name: str, plugin,
    ) -> Vertical:
        """Phase 3b — Events section on the per-plugin tab.

        Static title + DataTable (cursor_type="none", max-height 8 per
        plan Section 4.5 + plan-cycle 1 fix #36 — non-interactive
        regression target) + cross-link button. Empty `plugin.events` →
        `(no events declared)` placeholder.
        """
        title_static = Static(
            "[bold]Events[/bold]", markup=True,
            classes="per-plugin-section-title",
        )

        events_dict = None
        if plugin is not None:
            cand = getattr(plugin, "events", None)
            if isinstance(cand, dict):
                events_dict = cand

        body = [title_static]

        if plugin is None:
            body.append(Static(
                "[dim](plugin not loaded)[/dim]",
                markup=True, classes="per-plugin-empty",
                id=self._per_plugin_widget_id(
                    "per-plugin-events-empty", plugin_name,
                ),
            ))
        elif not events_dict:
            body.append(Static(
                "[dim](no events declared)[/dim]",
                markup=True, classes="per-plugin-empty",
                id=self._per_plugin_widget_id(
                    "per-plugin-events-empty", plugin_name,
                ),
            ))
        else:
            table = DataTable(
                cursor_type="none",  # plan Section 4.5 — non-interactive
                classes="per-plugin-table",
                id=self._per_plugin_widget_id(
                    "per-plugin-events-table", plugin_name,
                ),
            )
            table.add_columns("Event ID", "Topic", "Hosts", "Enabled")
            for evt_id, entry in events_dict.items():
                if not isinstance(entry, dict):
                    continue
                topic = str(entry.get("topic", ""))
                hosts = entry.get("hosts")
                enabled = entry.get("enabled", True)
                # `hosts` can be user-config-controlled (list/str/None);
                # wrap defensively so a stray markup char in a config
                # value doesn't break rendering.
                hosts_text = Text("" if hosts is None else str(hosts))
                enabled_text = Text(
                    "✓" if enabled else "✗",
                    style="green" if enabled else "red",
                )
                table.add_row(
                    str(evt_id),
                    Text(topic),
                    hosts_text,
                    enabled_text,
                )
            body.append(table)

        # Cross-link button — only when plugin is loaded (jump target
        # only makes sense for a plugin the catalogue knows about).
        if plugin is not None:
            jump_id = self._make_id(
                "jump-events", plugin_name, "", "per-plugin-jump-events",
            )
            body.append(Button(
                "Open in Events catalogue",
                id=jump_id,
                classes="per-plugin-jump-button",
                variant="primary",
            ))

        return Vertical(
            *body, classes="per-plugin-section",
            id=self._per_plugin_widget_id(
                "per-plugin-events-section", plugin_name,
            ),
        )

    def _build_per_plugin_subs_section(
        self, plugin_name: str, plugin,
    ) -> Vertical:
        """Phase 3b — Subscriptions section on the per-plugin tab.

        Two sub-tables: declared (from `plugin.subscriptions`) and
        runtime (populated post-mount via
        `_populate_per_plugin_runtime_subs` worker using a single
        `topic_registry.list_local_subs()` snapshot + client-side
        filter `s.plugin_uuid == plugin.plugin_uuid AND s.declared_id
        is None`).

        Cross-link button switches to Events tab → Subs browser inner
        tab + injects the plugin name into the filter Select.

        For UNLOADED / FAILED_LOAD plugins (plugin is None): render
        `(plugin not loaded)` placeholders, no cross-link button.
        """
        body = [
            Static(
                "[bold]Subscriptions[/bold]", markup=True,
                classes="per-plugin-section-title",
            ),
        ]

        if plugin is None:
            body.append(Static(
                "[dim](plugin not loaded)[/dim]",
                markup=True, classes="per-plugin-empty",
                id=self._per_plugin_widget_id(
                    "per-plugin-subs-declared-empty", plugin_name,
                ),
            ))
            body.append(Static(
                "[dim](plugin not loaded)[/dim]",
                markup=True, classes="per-plugin-empty",
                id=self._per_plugin_widget_id(
                    "per-plugin-subs-runtime-empty", plugin_name,
                ),
            ))
            return Vertical(
                *body, classes="per-plugin-section",
                id=self._per_plugin_widget_id(
                    "per-plugin-subs-section", plugin_name,
                ),
            )

        # Declared subs table — sync read from plugin.subscriptions.
        declared_dict = getattr(plugin, "subscriptions", None) or {}
        if not isinstance(declared_dict, dict):
            declared_dict = {}

        body.append(Static(
            "[dim]Declared (YAML):[/dim]", markup=True,
            classes="per-plugin-section-hint",
        ))
        if not declared_dict:
            body.append(Static(
                "[dim](no declared subs)[/dim]",
                markup=True, classes="per-plugin-empty",
                id=self._per_plugin_widget_id(
                    "per-plugin-subs-declared-empty", plugin_name,
                ),
            ))
        else:
            d_table = DataTable(
                cursor_type="none",
                classes="per-plugin-table",
                id=self._per_plugin_widget_id(
                    "per-plugin-subs-declared-table", plugin_name,
                ),
            )
            d_table.add_columns(
                "Declared ID", "Topic", "Target", "Hosts",
                "Authors", "Enabled",
            )
            for dec_id, entry in declared_dict.items():
                if not isinstance(entry, dict):
                    continue
                target_plugin = entry.get("target_plugin") or plugin_name
                target_access = entry.get("target_access_name") or "?"
                hosts = entry.get("hosts")
                authors = entry.get("authors")
                enabled = entry.get("enabled", True)
                enabled_text = Text(
                    "✓" if enabled else "✗",
                    style="green" if enabled else "red",
                )
                d_table.add_row(
                    str(dec_id),
                    Text(str(entry.get("topic", ""))),
                    Text(f"{target_plugin}.{target_access}"),
                    Text("" if hosts is None else str(hosts)),
                    Text("" if authors is None else str(authors)),
                    enabled_text,
                )
            body.append(d_table)

        # Runtime subs table — populated by worker after mount. Default
        # to the empty placeholder VISIBLE; worker swaps display states
        # based on the snapshot filter result. Both widgets mounted so
        # the worker can flip `.display` without re-mounting.
        body.append(Static(
            "[dim]Runtime:[/dim]", markup=True,
            classes="per-plugin-section-hint",
        ))
        runtime_empty = Static(
            "[dim](no runtime subs)[/dim]",
            markup=True, classes="per-plugin-empty",
            id=self._per_plugin_widget_id(
                "per-plugin-subs-runtime-empty", plugin_name,
            ),
        )
        runtime_table = DataTable(
            cursor_type="none",
            classes="per-plugin-table",
            id=self._per_plugin_widget_id(
                "per-plugin-subs-runtime-table", plugin_name,
            ),
        )
        # Defer add_columns until the worker runs — DataTable.add_columns
        # requires `self.app.console` for width measurement, which only
        # exists inside an active Textual app context. Sync-construction
        # callers (the legacy
        # `TestPluginViewGeneration.test_auto_generate_no_endpoints`
        # invokes `_auto_generate_plugin_view` without a running app)
        # would otherwise raise `NoActiveAppError`.
        runtime_table.display = False  # hidden until worker confirms rows
        body.append(runtime_empty)
        body.append(runtime_table)

        # Cross-link button.
        jump_id = self._make_id(
            "jump-subs", plugin_name, "", "per-plugin-jump-subs",
        )
        body.append(Button(
            "Open in Subscriptions browser",
            id=jump_id,
            classes="per-plugin-jump-button",
            variant="primary",
        ))

        return Vertical(
            *body, classes="per-plugin-section",
            id=self._per_plugin_widget_id(
                "per-plugin-subs-section", plugin_name,
            ),
        )

    def _build_per_plugin_logger_section(
        self, plugin_name: str, plugin,
    ) -> Vertical:
        """Phase 3b — Logger-level overrides section on the per-plugin tab.

        Header (title + Refresh button), hint, Current Overrides list
        (rows owned by THIS plugin — Vertical container of per-prefix
        Horizontal rows so each row can host an actual Clear Button;
        Textual 8.2.3's DataTable can't embed widgets in cells), Add
        row (Input + 2 Selects + Apply button).

        For UNLOADED / FAILED_LOAD plugins (plugin is None or no
        plugin_uuid): render placeholder without Add row — per plan
        Section 4.7 edge case + plan-cycle 2 resolution (option A:
        treat both states identically since `pc.plugins.get(name)` is
        None for both per `PluginCore.py:2466-2474`).

        `pc.list_logger_levels()`, `set_logger_level`, `clear_logger_level`
        are sync (per plan Section 4.7 cycle-2 fix referencing
        `PluginCore.py:1737-1763`). Direct-call from the handler is
        correct — wrapping in `_run_on_main` would raise TypeError
        ("a coroutine was expected") because run_coroutine_threadsafe
        expects a coroutine.
        """
        plugin_uuid = (
            str(getattr(plugin, "plugin_uuid", "") or "") if plugin is not None else ""
        )
        renderable_section = (plugin is not None and bool(plugin_uuid))

        if not renderable_section:
            # No Add row for UNLOADED / FAILED_LOAD (per Option A
            # resolution — plugin not actionable).
            title_only = Horizontal(
                Static(
                    "[bold]Logger-level overrides[/bold]", markup=True,
                    classes="per-plugin-section-title",
                ),
                classes="per-plugin-logger-header",
            )
            return Vertical(
                title_only,
                Static(
                    "[dim](plugin not loaded)[/dim]",
                    markup=True, classes="per-plugin-empty",
                    id=self._per_plugin_widget_id(
                        "per-plugin-logger-empty", plugin_name,
                    ),
                ),
                classes="per-plugin-section",
                id=self._per_plugin_widget_id(
                    "per-plugin-logger-section", plugin_name,
                ),
            )

        # Refresh button — re-renders the Current Overrides list only
        # (no bus topic for logger-level changes, per plan Section 4.7).
        refresh_id = self._make_id(
            "logger-refresh", plugin_name, "", "per-plugin-logger-refresh",
        )
        title_row = Horizontal(
            Static(
                "[bold]Logger-level overrides[/bold]", markup=True,
                classes="per-plugin-section-title",
            ),
            Button("Refresh", id=refresh_id, variant="default"),
            classes="per-plugin-logger-header",
        )

        body: list = [title_row]
        body.append(Static(
            "[dim]Per-logger thresholds owned by this plugin. "
            "Plugin source wins over config source.[/dim]",
            markup=True, classes="per-plugin-section-hint",
        ))

        # Current overrides list (Vertical of per-prefix rows).
        owners_filter = (plugin_name, plugin_uuid)
        overrides_list = Vertical(
            id=self._per_plugin_widget_id(
                "per-plugin-logger-list", plugin_name,
            ),
            classes="per-plugin-logger-list",
        )
        body.append(overrides_list)

        # Add row: prefix Input + console Select + file Select + Apply.
        prefix_id = self._per_plugin_widget_id(
            "per-plugin-logger-prefix", plugin_name,
        )
        console_id = self._per_plugin_widget_id(
            "per-plugin-logger-console", plugin_name,
        )
        file_id = self._per_plugin_widget_id(
            "per-plugin-logger-file", plugin_name,
        )
        apply_id = self._make_id(
            "logger-apply", plugin_name, "", "per-plugin-logger-apply",
        )
        body.append(Horizontal(
            Input(placeholder="logger prefix (e.g. asyncio)", id=prefix_id),
            Select(
                _LOGGER_LEVEL_OPTIONS, value="__keep__",
                allow_blank=False, id=console_id,
            ),
            Select(
                _LOGGER_LEVEL_OPTIONS, value="__keep__",
                allow_blank=False, id=file_id,
            ),
            Button("Apply", id=apply_id, variant="primary"),
            classes="per-plugin-logger-add-row",
            id=self._per_plugin_widget_id(
                "per-plugin-logger-add-row", plugin_name,
            ),
        ))

        # Initial population is triggered by `open_plugin_tab` /
        # `_do_switch_view_mode` AFTER the mount loop completes via
        # `_finalize_per_plugin_phase3b_sections`. Builders cannot
        # reliably schedule it themselves because call_after_refresh
        # may fire before mount finishes on the trailing widgets.

        return Vertical(
            *body, classes="per-plugin-section",
            id=self._per_plugin_widget_id(
                "per-plugin-logger-section", plugin_name,
            ),
        )

    def _refresh_per_plugin_logger_list_for(self, plugin_name: str) -> None:
        """Phase 3b — schedule the per-plugin Logger overrides list
        re-render via a plugin-scoped worker group.

        Cycle 3 fresh-eyes MEDIUM fix: a static @work group string
        (`"per-plugin-logger-list"`) with `exclusive=True` would
        cancel ANOTHER plugin's in-flight worker the moment THIS
        plugin's tab triggered a re-render — leaving that other
        plugin's overrides list half-rendered. Use `run_worker(...)`
        with a dynamic per-plugin group so only same-plugin invocations
        cancel each other.

        Callers: initial mount from `_finalize_per_plugin_phase3b_sections`;
        Apply/Clear/Refresh handlers from `on_button_pressed`.
        """
        self.run_worker(
            self._do_refresh_per_plugin_logger_list(plugin_name),
            group=f"per-plugin-logger-list-{plugin_name}",
            exclusive=True,
            thread=False,
        )

    async def _do_refresh_per_plugin_logger_list(
        self, plugin_name: str,
    ) -> None:
        """Worker body — fetch snapshot, await-clear the overrides
        Vertical, mount per-prefix rows or the empty placeholder.

        Scheduled by `_refresh_per_plugin_logger_list_for` with a
        plugin-scoped exclusive group. Same-plugin rapid re-renders
        (Apply / Clear / Refresh) cancel the prior mid-await; cross-
        plugin invocations run in parallel.
        """
        try:
            list_id = self._per_plugin_widget_id(
                "per-plugin-logger-list", plugin_name,
            )
            overrides_list = self.query_one(f"#{list_id}", Vertical)
        except NoMatches:
            return
        plugin = self.plugin_core.plugins.get(plugin_name)
        if plugin is None:
            return
        plugin_uuid = str(getattr(plugin, "plugin_uuid", "") or "")
        if not plugin_uuid:
            return
        try:
            snapshot = self.plugin_core.list_logger_levels()
        except Exception:
            snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}

        # Prune the `_id_registry` entries the prior render produced for
        # this plugin's per-row Clear buttons. Without this, repeated
        # Apply/Clear/Refresh on a long-lived tab accumulates dead
        # registry rows pointing at unmounted widgets — bounded by
        # interaction count, cleaned only on tab close otherwise
        # (Cycle 2 fresh-eyes LOW finding).
        to_drop = [
            wid for wid, e in self._id_registry.items()
            if e.get("plugin") == plugin_name
            and e.get("type") == "per-plugin-logger-clear"
        ]
        for wid in to_drop:
            self._id_registry.pop(wid, None)

        # Await-clear so child.remove() completes before the next mount.
        try:
            await overrides_list.remove_children()
        except Exception:
            return  # widget tree torn down mid-refresh — bail

        owners_filter = (plugin_name, plugin_uuid)
        matching = []
        for prefix, info in snapshot.items():
            if not isinstance(info, dict):
                continue
            owners = info.get("owners") or []
            if owners_filter not in owners:
                continue
            matching.append((prefix, info))

        if not matching:
            await overrides_list.mount(Static(
                "[dim](no overrides owned by this plugin)[/dim]",
                markup=True, classes="per-plugin-empty",
                id=self._per_plugin_widget_id(
                    "per-plugin-logger-empty", plugin_name,
                ),
            ))
            return

        for prefix, info in matching:
            effective = info.get("effective", {}) or {}
            config = info.get("config", {}) or {}
            eff_console = effective.get("console")
            eff_file = effective.get("file")
            cfg_console = config.get("console")
            cfg_file = config.get("file")
            console_cell = "" if eff_console is None else str(eff_console)
            if cfg_console is not None and cfg_console != eff_console:
                console_cell += f" (was: {cfg_console})"
            file_cell = "" if eff_file is None else str(eff_file)
            if cfg_file is not None and cfg_file != eff_file:
                file_cell += f" (was: {cfg_file})"

            # Per-row Clear button — registered with prefix in the
            # `endpoint` slot so the on_button_pressed catch-all can
            # route to the clear handler.
            clear_id = self._make_id(
                "logger-clear", plugin_name, prefix,
                "per-plugin-logger-clear",
            )
            row = Horizontal(
                Static(
                    f"[bold]{escape(prefix)}[/bold]", markup=True,
                    classes="per-plugin-logger-prefix-cell",
                ),
                # `Static` defaults to `markup=True`. Level strings
                # are validated (DEBUG/INFO/etc.) but `cfg_*` values
                # come straight from the LogUtil snapshot dict; an
                # unbracketed value-replace future could surface user
                # text. Disable markup parsing for these cells —
                # consistent with the LOW review fix.
                Static(
                    console_cell, markup=False,
                    classes="per-plugin-logger-console-cell",
                ),
                Static(
                    file_cell, markup=False,
                    classes="per-plugin-logger-file-cell",
                ),
                Button("Clear", id=clear_id, variant="warning"),
                classes="per-plugin-logger-row",
            )
            await overrides_list.mount(row)

    def _populate_per_plugin_runtime_subs(
        self, plugin_name: str, plugin_uuid: str,
    ) -> None:
        """Sync wrapper — schedules the runtime-subs worker with a
        plugin-scoped exclusive group. Cycle 3 fresh-eyes MEDIUM fix —
        see `_refresh_per_plugin_logger_list_for` for the same dynamic-
        group rationale.
        """
        self.run_worker(
            self._do_populate_per_plugin_runtime_subs(plugin_name, plugin_uuid),
            group=f"per-plugin-runtime-subs-{plugin_name}",
            exclusive=True,
            thread=False,
        )

    async def _do_populate_per_plugin_runtime_subs(
        self, plugin_name: str, plugin_uuid: str,
    ) -> None:
        """Worker body — populate the runtime subs table for `plugin_name`
        from a single `pc.topic_registry.list_local_subs()` snapshot.

        Filter: `sub.plugin_uuid == plugin_uuid AND sub.declared_id is
        None` (runtime subs only; YAML-declared subs are shown in the
        declared table). Plan Section 4.6 cycle-1 fix — single snapshot
        call avoids the N-await per-sub lookup AND closes the mid-iter
        pop window (concurrent `_pop_plugin_under_lock` can remove a
        sub between awaits).
        """
        try:
            table_id = self._per_plugin_widget_id(
                "per-plugin-subs-runtime-table", plugin_name,
            )
            empty_id = self._per_plugin_widget_id(
                "per-plugin-subs-runtime-empty", plugin_name,
            )
            table = self.query_one(f"#{table_id}", DataTable)
            empty = self.query_one(f"#{empty_id}", Static)
        except NoMatches:
            return  # tab unmounted between schedule and execute

        # Snapshot via _run_on_main — list_local_subs is async + holds
        # topic_registry._lock on the main loop.
        try:
            subs = await self._run_on_main(
                self.plugin_core.topic_registry.list_local_subs()
            )
        except Exception:
            subs = None
        if subs is None:
            subs = []

        rows = []
        for s in subs:
            if getattr(s, "plugin_uuid", None) != plugin_uuid:
                continue
            if getattr(s, "declared_id", None) is not None:
                continue
            rows.append(s)

        try:
            table.clear()
        except Exception:
            pass
        # Add columns lazily — first run after the widget is mounted
        # inside an active app context. `column_count` is 0 until
        # `add_columns` runs, so this is idempotent on subsequent worker
        # invocations.
        if not table.columns:
            try:
                table.add_columns(
                    "Sub UUID", "Topic", "Target", "Hosts", "Authors",
                )
            except Exception:
                # Defensive — if the active app context is unexpectedly
                # absent, leave the table empty rather than raising.
                return
        for s in rows:
            sub_uuid_short = str(getattr(s, "sub_uuid", "") or "")[:8] + "…"
            topic = str(getattr(s, "topic_pattern", "") or "")
            target_plugin = (
                getattr(s, "target_plugin", None)
                or getattr(s, "plugin_name", "")
            )
            target_access = getattr(s, "target_access_name", "") or ""
            hosts = getattr(s, "hosts", None)
            authors = getattr(s, "authors", None)
            table.add_row(
                Text(sub_uuid_short),
                Text(topic),
                Text(f"{target_plugin}.{target_access}"),
                Text("" if hosts is None else str(hosts)),
                Text("" if authors is None else str(authors)),
            )

        # Toggle display: show table when we have rows, otherwise show
        # the empty placeholder.
        if rows:
            table.display = True
            empty.display = False
        else:
            table.display = False
            empty.display = True

    # ── Phase 3b — Cross-link handlers ────────────────────────────────

    @work(thread=False, exclusive=True, group="cross-link")
    async def _jump_to_events_catalogue_for_plugin(
        self, plugin_name: str,
    ) -> None:
        """Switch to the Events catalogue, inject + select the plugin
        name on the catalogue's filter Select.

        Plan Section 4.6 + cycle-2 fix + plan-cycle 2 review: shared
        `group="cross-link"` with `_jump_to_subs_browser_for_plugin` so
        rapid double-press serialises (latest jump wins, with its own
        option-injection landing on its destination Select before the
        tab activation). Different groups would let concurrent workers
        race and land on the wrong tab with un-applied injection on
        the OTHER Select.
        """
        await self._inject_option_and_select(
            "#events-cat-filter-plugin", plugin_name,
        )
        try:
            self.query_one("#main-tabs", TabbedContent).active = "tab-events"
            self.query_one("#events-tabs", TabbedContent).active = "events-tab-cat"
        except NoMatches:
            return
        # Force the outer flag so the debounce tick processes the
        # cat refresh even if TabActivated dispatch is delayed in test
        # mode (Phase 2b lesson).
        self._outer_is_events = True

    @work(thread=False, exclusive=True, group="cross-link")
    async def _jump_to_subs_browser_for_plugin(
        self, plugin_name: str,
    ) -> None:
        """Switch to the Subs browser, inject + select the plugin name
        on the subs filter Select. Shared `group="cross-link"` with the
        catalogue jump (see `_jump_to_events_catalogue_for_plugin`).
        """
        await self._inject_option_and_select(
            "#events-subs-filter-plugin", plugin_name,
        )
        try:
            self.query_one("#main-tabs", TabbedContent).active = "tab-events"
            self.query_one("#events-tabs", TabbedContent).active = "events-tab-subs"
        except NoMatches:
            return
        self._outer_is_events = True

    async def _inject_option_and_select(
        self, select_selector: str, plugin_name: str,
    ) -> None:
        """Plan Section 4.6 cycle-2 fix — explicit option-injection
        BEFORE tab switch so the value-set always lands on the target
        Select.

        Reads `sel._options` (internal) to filter out the prepended
        allow_blank row (`Select.NULL` sentinel — verified at
        `plugins_test/TUI/textual/src/textual/widgets/_select.py:37`
        + the class-level alias at line 289 `NULL = NULL`). If the
        plugin is absent from the current options list, prepends it
        before calling `set_options(...)` then assigns `value`.

        `prevent(Select.Changed)` suppresses the in-flight change
        event so the destination tab's filter-change observer doesn't
        re-trigger its own refresh worker mid-injection.
        """
        try:
            sel = self.query_one(select_selector, Select)
        except NoMatches:
            return
        # Filter out the NULL sentinel from the existing option list —
        # set_options would double the blank row otherwise.
        current_options = [
            (label, value) for label, value in sel._options
            if value is not Select.NULL
        ]
        if not any(v == plugin_name for _, v in current_options):
            new_options = current_options + [(plugin_name, plugin_name)]
            with self.prevent(Select.Changed):
                try:
                    sel.set_options(new_options)
                except Exception:
                    # Defensive — malformed list shouldn't happen but
                    # bailing here at worst leaves the operator on the
                    # destination tab with no filter applied.
                    return
        with self.prevent(Select.Changed):
            try:
                sel.value = plugin_name
            except Exception:
                # Per plan-cycle 1 test #24: silent no-op acceptable —
                # operator lands on the destination tab even if the
                # filter Select rejected the value.
                pass

    # ── Phase 3b — Logger-level Apply / Clear handlers ────────────────

    def _handle_per_plugin_logger_apply(self, plugin_name: str) -> None:
        """Apply button handler for the per-plugin Logger overrides
        Add row.

        Reads the prefix Input + 2 Selects, maps `__keep__` → None,
        calls `pc.set_logger_level` DIRECTLY (sync — plan Section 4.7
        cycle-2 fix locks this), wrapped in try/except + `self.notify`
        per plan-cycle 2 review fix so a validation raise inside the
        handler doesn't tear down the widget tree silently.
        """
        prefix_id = self._per_plugin_widget_id(
            "per-plugin-logger-prefix", plugin_name,
        )
        console_id = self._per_plugin_widget_id(
            "per-plugin-logger-console", plugin_name,
        )
        file_id = self._per_plugin_widget_id(
            "per-plugin-logger-file", plugin_name,
        )
        try:
            prefix_widget = self.query_one(f"#{prefix_id}", Input)
            console_widget = self.query_one(f"#{console_id}", Select)
            file_widget = self.query_one(f"#{file_id}", Select)
        except NoMatches:
            return
        prefix = (prefix_widget.value or "").strip()
        if not prefix:
            self.notify("Logger prefix required", severity="warning")
            return
        console_val = console_widget.value
        file_val = file_widget.value
        console_arg = None if console_val == "__keep__" else console_val
        file_arg = None if file_val == "__keep__" else file_val
        if console_arg is None and file_arg is None:
            self.notify("Pick at least one level", severity="warning")
            return
        plugin = self.plugin_core.plugins.get(plugin_name)
        if plugin is None:
            return
        plugin_uuid = str(getattr(plugin, "plugin_uuid", "") or "")
        if not plugin_uuid:
            return
        try:
            self.plugin_core.set_logger_level(
                prefix,
                console=console_arg,
                file=file_arg,
                plugin_name=plugin_name,
                plugin_uuid=plugin_uuid,
            )
        except Exception as exc:
            self.notify(
                f"Logger-level apply failed: {exc}", severity="error",
            )
            return
        self._refresh_per_plugin_logger_list_for(plugin_name)

    def _handle_per_plugin_logger_clear(
        self, plugin_name: str, prefix: str,
    ) -> None:
        """Per-row Clear handler. Sync call wrapped in try/except per
        plan-cycle 2 review fix."""
        plugin = self.plugin_core.plugins.get(plugin_name)
        if plugin is None:
            return
        plugin_uuid = str(getattr(plugin, "plugin_uuid", "") or "")
        if not plugin_uuid:
            return
        try:
            self.plugin_core.clear_logger_level(
                prefix, console=True, file=True,
                plugin_name=plugin_name, plugin_uuid=plugin_uuid,
            )
        except Exception as exc:
            self.notify(
                f"Logger-level clear failed: {exc}", severity="error",
            )
            return
        self._refresh_per_plugin_logger_list_for(plugin_name)

    def _handle_per_plugin_logger_refresh(self, plugin_name: str) -> None:
        """Section-header Refresh handler — re-read snapshot, refill
        table. Section 4.7 documented use-case: changes from outside
        the TUI (no bus topic for logger-level changes)."""
        self._refresh_per_plugin_logger_list_for(plugin_name)

    def _handle_per_plugin_copy_uuid(self, plugin_name: str) -> None:
        """Copy the plugin's plugin_uuid to the OS clipboard via the
        Textual App's `copy_to_clipboard` (terminal OSC52 escape;
        plan Section 4.4 + test #20 require this)."""
        plugin = self.plugin_core.plugins.get(plugin_name)
        if plugin is None:
            return
        plugin_uuid = str(getattr(plugin, "plugin_uuid", "") or "")
        if not plugin_uuid:
            return
        try:
            self.copy_to_clipboard(plugin_uuid)
        except Exception:
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

    def action_tab_events(self) -> None:  # Phase 2b
        self._switch_tab("tab-events")

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
