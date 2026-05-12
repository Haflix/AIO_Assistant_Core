"""
Unit tests for the CLI Dashboard plugin.

Tests cover:
- TUILogHandler: buffering, attach/detach, emit routing, thread safety, display_level,
  incremental refresh, record count indicator
- RequestTracker: polling, throughput, latency, error tracking
- DashboardApp: ID registry, config file list, plugin view generation, config dirty tracking
- Headless integration: compose renders all widgets, tab switching, keyboard shortcuts
"""

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

import pytest

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from plugins_test.CLI.log_handler import TUILogHandler, LogRecord
from plugins_test.CLI.request_tracker import RequestTracker, ActiveRequest


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _make_log_record(msg="test message", level=logging.INFO):
    return logging.LogRecord(
        name="test_logger", level=level, pathname="test.py",
        lineno=1, msg=msg, args=(), exc_info=None, func="test_func",
    )

def _make_mock_data_table():
    """Mock a DataTable widget with the methods TUILogHandler uses."""
    w = MagicMock()
    w.app = MagicMock()
    w.row_count = 0
    w._rows = {}  # track rows by key for realistic behavior

    def add_row(*args, key=None):
        w._rows[key] = args
        w.row_count = len(w._rows)

    def remove_row(key):
        w._rows.pop(key, None)
        w.row_count = len(w._rows)

    def clear():
        w._rows.clear()
        w.row_count = 0

    w.add_row = MagicMock(side_effect=add_row)
    w.remove_row = MagicMock(side_effect=remove_row)
    w.clear = MagicMock(side_effect=clear)
    w.move_cursor = MagicMock()
    w.add_columns = MagicMock()
    return w

def _make_mock_plugin_core(plugins=None, yaml_config=None):
    pc = MagicMock()
    pc.plugins = plugins or {}
    pc.yaml_config = yaml_config or {"plugins": [], "general": {}, "networking": {}}
    pc.config_path = "config.yml"
    pc.plugin_package = "plugins"
    pc.hostname = "test-host"
    pc.networking_enabled = False
    pc.networking_port = 2510
    pc.networking_auto_discoverable = False
    pc.networking_direct_discoverable = False
    pc.plugin_lock = asyncio.Lock()
    pc.requests = {}
    pc.get_plugin_info = AsyncMock(return_value={
        "name": "TestPlugin", "version": "1.0", "uuid": "abc123",
        "enabled": True, "remote": False, "description": "test plugin",
        "arguments": None,
    })
    pc.get_plugin_endpoints = AsyncMock(return_value=[])
    return pc

def _make_mock_plugin(
    name="TestPlugin", enabled=True, version="1.0", remote=False,
    description="A test plugin", endpoints=None,
    has_tui_module_info=False, has_tui_menu=False,
    tui_module_info_return=None, tui_menu_return=None,
):
    p = MagicMock()
    p.enabled = enabled
    p.version = version
    p.remote = remote
    p.description = description
    p.plugin_name = name
    p.endpoints = endpoints or {}
    if not has_tui_module_info:
        del p.get_tui_module_info
    else:
        p.get_tui_module_info.return_value = tui_module_info_return
    if not has_tui_menu:
        del p.get_tui_menu
    else:
        p.get_tui_menu.return_value = tui_menu_return
    return p

def _make_dashboard_app(plugin_core=None):
    from plugins_test.CLI.app import DashboardApp
    app = object.__new__(DashboardApp)
    app.plugin_core = plugin_core or _make_mock_plugin_core()
    app.plugin_instance = MagicMock(plugin_name="CLI")
    app.log_handler = TUILogHandler()
    app._start_time = 1000000.0
    app._tracker = RequestTracker()
    app._graph_toggles = {"cpu": True, "memory": True}
    app._cpu_data = []
    app._mem_data = []
    app._current_config_file = None
    app._config_files = {}
    app._config_clean_hash = None
    app._stats_interval = 2.0
    app._plugin_interval = 3.0
    app._request_interval = 1.0
    app._network_interval = 3.0  # Phase 1
    app._stats_timer = None
    app._plugin_timer = None
    app._request_timer = None
    app._log_timer = None
    app._network_timer = None  # Phase 1
    app._id_counter = 0
    app._id_registry = {}
    app._plugin_tab_map = {}
    app._plugin_tab_modes = {}
    app._plugin_filter = ""
    return app

def _make_mock_request(plugin="PluginA", method="do_thing", age=0.5,
                       author="?", error=False, timeout=False,
                       finished_at=None):
    r = MagicMock()
    r.target_plugin = plugin
    r.target_method = method
    r.author = author
    r.created_at = time.time() - age
    r.ready = finished_at is not None
    r.error = error
    r.timeout = timeout
    r.finished_at = finished_at
    return r


# ═══════════════════════════════════════════════════════════════════════
# TUILogHandler Tests
# ═══════════════════════════════════════════════════════════════════════

class TestTUILogHandlerInit:
    def test_default_buffer_size(self):
        assert TUILogHandler()._buffer.maxlen == 500

    def test_default_level_is_debug(self):
        assert TUILogHandler().level == logging.DEBUG

    def test_starts_detached(self):
        h = TUILogHandler()
        assert h._widget is None and h._app is None

    def test_store_starts_empty(self):
        h = TUILogHandler()
        assert len(h._store) == 0

    def test_displayed_seqs_starts_empty(self):
        h = TUILogHandler()
        assert h._displayed_seqs == []


class TestTUILogHandlerBuffering:
    def test_emit_buffers_when_detached(self):
        h = TUILogHandler()
        h.emit(_make_log_record("hello"))
        assert len(h._buffer) == 1
        assert h._buffer[0].message == "hello"

    def test_buffer_respects_max_size(self):
        h = TUILogHandler(max_buffer=3)
        for i in range(5):
            h.emit(_make_log_record(f"msg {i}"))
        assert len(h._buffer) == 3
        assert list(h._buffer)[0].message == "msg 2"

    def test_buffered_records_have_seq(self):
        h = TUILogHandler()
        h.emit(_make_log_record("a"))
        h.emit(_make_log_record("b"))
        seqs = [rec.seq for rec in h._buffer]
        assert seqs[1] > seqs[0]


class TestTUILogHandlerAttachDetach:
    def test_attach_flushes_buffer_to_store(self):
        h = TUILogHandler()
        h.emit(_make_log_record("buf1"))
        h.emit(_make_log_record("buf2"))
        w = _make_mock_data_table()
        h.attach(w)
        assert len(h._buffer) == 0
        assert len(h._store) == 2

    def test_attach_sets_rebuild_flag(self):
        h = TUILogHandler()
        w = _make_mock_data_table()
        h.attach(w)
        assert h._needs_rebuild is True

    def test_detach_re_enables_buffering(self):
        h = TUILogHandler()
        w = _make_mock_data_table()
        h.attach(w)
        h.detach()
        h.emit(_make_log_record("after"))
        assert len(h._buffer) == 1


class TestTUILogHandlerDisplayLevel:
    def test_display_level_filters_records(self):
        h = TUILogHandler()
        w = _make_mock_data_table()
        h.attach(w)
        h.display_level = logging.WARNING
        h.emit(_make_log_record("debug", level=logging.DEBUG))
        h.emit(_make_log_record("warn", level=logging.WARNING))
        h.emit(_make_log_record("err", level=logging.ERROR))
        # All 3 go to store, but only 2 pass the display filter
        assert len(h._store) == 3
        filtered = h.get_filtered_records()
        assert len(filtered) == 2

    def test_display_level_change_sets_rebuild(self):
        h = TUILogHandler()
        h.display_level = logging.ERROR
        assert h._needs_rebuild is True

    def test_search_filter_narrows_results(self):
        h = TUILogHandler()
        w = _make_mock_data_table()
        h.attach(w)
        h.emit(_make_log_record("alpha"))
        h.emit(_make_log_record("beta"))
        h.emit(_make_log_record("alpha again"))
        h.search_filter = "alpha"
        filtered = h.get_filtered_records()
        assert len(filtered) == 2


class TestTUILogHandlerRefreshTable:
    def test_refresh_full_rebuild(self):
        h = TUILogHandler()
        w = _make_mock_data_table()
        h.attach(w)
        h.emit(_make_log_record("msg1"))
        h.emit(_make_log_record("msg2"))
        counts = h.refresh_table()
        assert counts == (2, 2)
        assert w.clear.called
        assert w.add_row.call_count == 2
        assert len(h._displayed_seqs) == 2

    def test_refresh_incremental_append(self):
        h = TUILogHandler()
        w = _make_mock_data_table()
        h.attach(w)
        h.emit(_make_log_record("msg1"))
        h.refresh_table()  # full rebuild (attach sets _needs_rebuild)
        w.clear.reset_mock()
        w.add_row.reset_mock()

        h.emit(_make_log_record("msg2"))
        counts = h.refresh_table()
        assert counts == (2, 2)
        # Should NOT clear — incremental
        assert not w.clear.called
        # Should add just the new row
        assert w.add_row.call_count == 1

    def test_refresh_returns_none_when_not_dirty(self):
        h = TUILogHandler()
        w = _make_mock_data_table()
        h.attach(w)
        h.refresh_table()  # consume dirty flag
        assert h.refresh_table() is None

    def test_refresh_returns_none_when_paused(self):
        h = TUILogHandler()
        w = _make_mock_data_table()
        h.attach(w)
        h.emit(_make_log_record("msg"))
        h.paused = True
        # dirty=True but paused, so no update
        h._dirty = True
        assert h.refresh_table() is None

    def test_filter_change_triggers_rebuild(self):
        h = TUILogHandler()
        w = _make_mock_data_table()
        h.attach(w)
        h.emit(_make_log_record("alpha"))
        h.emit(_make_log_record("beta"))
        h.refresh_table()  # initial rebuild
        w.clear.reset_mock()

        h.search_filter = "alpha"
        counts = h.refresh_table()
        assert counts == (1, 2)
        assert w.clear.called  # rebuild due to filter change

    def test_level_style_applied(self):
        """Verify _style_for_level returns explicit styles for all levels."""
        assert "red" in TUILogHandler._style_for_level(logging.ERROR)
        assert "#cca75a" in TUILogHandler._style_for_level(logging.WARNING)
        assert "#d4d4d4" in TUILogHandler._style_for_level(logging.INFO)
        assert "#666666" in TUILogHandler._style_for_level(logging.DEBUG)

    def test_seq_used_as_row_key(self):
        h = TUILogHandler()
        w = _make_mock_data_table()
        h.attach(w)
        h.emit(_make_log_record("msg"))
        h.refresh_table()
        # Row key should be the record's seq number as string
        call_kwargs = w.add_row.call_args
        assert call_kwargs is not None
        key = call_kwargs[1].get("key") if call_kwargs[1] else None
        assert key is not None and key.isdigit()


class TestTUILogHandlerThreadSafety:
    def test_concurrent_emits(self):
        h = TUILogHandler(max_buffer=1000)
        errors = []
        def batch(start):
            try:
                for i in range(50):
                    h.emit(_make_log_record(f"t{start}-{i}"))
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=batch, args=(t,)) for t in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors and len(h._buffer) == 500


class TestLogRecordSeq:
    def test_seq_monotonically_increasing(self):
        a = LogRecord("00:00:00", "INFO", logging.INFO, "src", "a", "a")
        b = LogRecord("00:00:00", "INFO", logging.INFO, "src", "b", "b")
        assert b.seq > a.seq


# ═══════════════════════════════════════════════════════════════════════
# RequestTracker Tests
# ═══════════════════════════════════════════════════════════════════════

class TestRequestTracker:
    def test_empty_poll(self):
        t = RequestTracker()
        t.poll({})
        assert t.total_requests == 0 and len(t.active) == 0

    def test_new_request_detected(self):
        t = RequestTracker()
        t.poll({"req1": _make_mock_request()})
        assert t.total_requests == 1
        assert len(t.active) == 1
        assert t.active[0].plugin == "PluginA"

    def test_completed_request_tracked(self):
        t = RequestTracker()
        t.poll({"req1": _make_mock_request()})
        t.poll({})  # req1 gone = completed
        assert len(t.active) == 0
        assert len(t.latencies) == 1

    def test_error_counted(self):
        t = RequestTracker()
        t.poll({"req1": _make_mock_request(error=True)})
        assert t.total_errors == 1

    def test_timeout_counted(self):
        t = RequestTracker()
        t.poll({"req1": _make_mock_request(timeout=True)})
        assert t.total_timeouts == 1

    def test_per_plugin_stats(self):
        t = RequestTracker()
        t.poll({
            "r1": _make_mock_request(plugin="A"),
            "r2": _make_mock_request(plugin="B"),
            "r3": _make_mock_request(plugin="A"),
        })
        assert t.per_plugin["A"].total == 2
        assert t.per_plugin["B"].total == 1

    def test_throughput_history_stores_tuples(self):
        t = RequestTracker()
        t.poll({"r1": _make_mock_request()})
        t.poll({})  # r1 completed
        assert len(t.throughput_history) == 2
        completed, elapsed = t.throughput_history[-1]
        assert completed == 1
        assert elapsed >= 0  # can be 0.0 if polls happen in same tick

    def test_reset(self):
        t = RequestTracker()
        t.poll({"r1": _make_mock_request(error=True)})
        t.reset()
        assert t.total_requests == 0 and t.total_errors == 0

    def test_author_tracked(self):
        t = RequestTracker()
        t.poll({"r1": _make_mock_request(author="TestUser")})
        assert t.active[0].author == "TestUser"

    def test_rpm_calculation(self):
        t = RequestTracker()
        t.poll({"r1": _make_mock_request()})
        t.poll({})  # completes
        assert t.requests_per_minute >= 0

    def test_finished_at_gives_accurate_latency(self):
        """When finished_at is set, latency should use it instead of poll time."""
        t = RequestTracker()
        now = time.time()
        req = MagicMock()
        req.target_plugin = "P"
        req.target_method = "m"
        req.author = "?"
        req.created_at = now - 2.0  # created 2s ago
        req.finished_at = now - 1.5  # finished 0.5s after creation
        req.error = False
        req.timeout = False
        t.poll({"r1": req})
        t.poll({})  # r1 disappears — completed
        assert len(t.latencies) == 1
        # Latency should be ~0.5s (finished_at - created_at), not ~2s (now - created_at)
        assert t.latencies[0] < 1.0

    def test_elapsed_uses_finished_at_for_completed_requests(self):
        """Active request elapsed should use finished_at when available."""
        t = RequestTracker()
        now = time.time()
        req = MagicMock()
        req.target_plugin = "P"
        req.target_method = "m"
        req.author = "?"
        req.created_at = now - 5.0  # created 5s ago
        req.finished_at = now - 4.0  # finished after 1s
        req.error = False
        req.timeout = False
        t.poll({"r1": req})
        # Request still in dict but finished — elapsed should be ~1s not ~5s
        assert t.active[0].elapsed < 2.0

    def test_latency_fallback_without_finished_at(self):
        """Without finished_at, latency falls back to poll-time approximation."""
        t = RequestTracker()
        req = _make_mock_request(age=0.5)
        req.finished_at = None  # no finished_at
        t.poll({"r1": req})
        t.poll({})  # completed
        assert len(t.latencies) == 1
        # Falls back to now - first_seen, should be roughly >=0.5s
        assert t.latencies[0] >= 0.0


# ═══════════════════════════════════════════════════════════════════════
# DashboardApp Logic Tests
# ═══════════════════════════════════════════════════════════════════════

class TestIdRegistry:
    def test_unique_ids(self):
        app = _make_dashboard_app()
        id1 = app._make_id("ep", "P", "e1", "call")
        id2 = app._make_id("ep", "P", "e2", "call")
        assert id1 != id2

    def test_stores_mapping(self):
        app = _make_dashboard_app()
        wid = app._make_id("ep", "MyPlugin", "do", "call")
        entry = app._lookup_id(wid)
        assert entry["plugin"] == "MyPlugin" and entry["type"] == "call"

    def test_cleanup_removes_entries(self):
        app = _make_dashboard_app()
        app._make_id("ep", "A", "e1", "call")
        app._make_id("ep", "A", "e2", "call")
        app._make_id("ep", "B", "e1", "call")
        app._cleanup_registry_for_plugin("A")
        assert not any(e["plugin"] == "A" for e in app._id_registry.values())
        assert any(e["plugin"] == "B" for e in app._id_registry.values())

class TestSanitizeId:
    def test_special_chars_removed(self):
        from plugins_test.CLI.app import DashboardApp
        result = DashboardApp._sanitize_id("AI:Plugin.v2")
        assert ":" not in result and "." not in result

    def test_different_names_unique(self):
        from plugins_test.CLI.app import DashboardApp
        assert DashboardApp._sanitize_id("AI:Plugin") != DashboardApp._sanitize_id("AI-Plugin")

class TestPluginViewGeneration:
    def test_auto_generate_no_endpoints(self):
        app = _make_dashboard_app()
        plugin = _make_mock_plugin(endpoints={})
        widgets = app._auto_generate_plugin_view("TestPlugin", plugin)
        from textual.widgets import Static
        statics = [w for w in widgets if isinstance(w, Static)]
        texts = " ".join(str(s._Static__content) for s in statics)
        assert "No endpoints" in texts

    def test_not_found(self):
        app = _make_dashboard_app()
        app.plugin_core.plugins = {}
        widgets = app._build_plugin_tab_content("Nope")
        assert len(widgets) == 1

    def test_custom_menu(self):
        app = _make_dashboard_app()
        menu = {"label": "Test", "sections": [
            {"title": "Info", "type": "info", "items": ["hello"]},
        ]}
        plugin = _make_mock_plugin(has_tui_menu=True, tui_menu_return=menu)
        app.plugin_core.plugins = {"P": plugin}
        result = app._build_plugin_tab_content("P")
        # Should return rendered menu widgets, not auto-generated
        assert len(result) > 0


class TestConfigDirtyTracking:
    def test_not_dirty_when_no_file_loaded(self):
        app = _make_dashboard_app()
        assert app._config_is_dirty() is False

    def test_dirty_detection(self):
        """Config dirty check compares current editor hash to clean hash."""
        import hashlib
        app = _make_dashboard_app()
        original = "key: value\n"
        app._config_clean_hash = hashlib.md5(original.encode()).hexdigest()
        # Without a real TextArea widget, we can't fully test this,
        # but we verify the hash mechanism works
        modified = "key: changed\n"
        assert hashlib.md5(modified.encode()).hexdigest() != app._config_clean_hash


# ═══════════════════════════════════════════════════════════════════════
# Headless Integration Tests
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_pc():
    pc = MagicMock()
    pc.plugins = {
        "PluginA": MagicMock(
            enabled=True, version="1.0", remote=False,
            description="test A", plugin_name="PluginA",
            endpoints={
                "greet": {
                    "internal_name": "_greet",
                    "description": "Says hi", "remote": False,
                    "accessible_by_other_plugins": True,
                    "arguments": [{"name": "name", "type": "str", "description": "Who to greet"}],
                    "tags": [],
                },
            },
        ),
        "PluginB": MagicMock(
            enabled=False, version="0.5", remote=True,
            description="test B", plugin_name="PluginB", endpoints={},
        ),
    }
    for p in pc.plugins.values():
        del p.get_tui_module_info
        del p.get_tui_menu
    pc.yaml_config = {"plugins": [], "general": {}, "networking": {}}
    pc.config_path = "config.yml"
    pc.plugin_package = "plugins_test"
    pc.hostname = "test-host"
    pc.networking_enabled = False
    pc.networking_port = 2510
    pc.networking_auto_discoverable = False
    pc.networking_direct_discoverable = False
    pc.networking_heartbeat_interval = 10.0
    pc.networking_lookup_interval = 60.0
    pc.networking_liveness_timeout = 30.0
    pc.network = None  # NM only built when networking_enabled=True
    pc.plugin_lock = asyncio.Lock()
    pc.requests = {}
    pc.get_plugin_info = AsyncMock(return_value={
        "name": "PluginA", "version": "1.0", "uuid": "abc",
        "enabled": True, "remote": False, "description": "test A",
    })
    pc.get_plugin_endpoints = AsyncMock(return_value=[])
    return pc


@pytest.mark.asyncio
async def test_compose_all_tabs(mock_pc):
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import Static, TabbedContent, DataTable, TextArea

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        tabs = app.query_one("#main-tabs", TabbedContent)
        assert tabs.active == "tab-home"

        # Home widgets
        for wid in ["stat-hostname", "stat-uptime", "stat-cpu", "stat-memory",
                     "stat-plugins-total", "stat-plugins-enabled",
                     "stat-req-active", "stat-req-total"]:
            app.query_one(f"#{wid}", Static)

        # Tables
        app.query_one("#plugin-table", DataTable)
        app.query_one("#request-table", DataTable)

        # Config
        app.query_one("#config-editor", TextArea)

        # Logs — now a DataTable, not RichLog
        app.query_one("#log-table", DataTable)

        # Log record count indicator
        app.query_one("#log-record-count", Static)

        # Empty state labels
        app.query_one("#request-empty", Static)


@pytest.mark.asyncio
async def test_tab_switching(mock_pc):
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import TabbedContent

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        tabs = app.query_one("#main-tabs", TabbedContent)
        for tid in ["tab-plugins", "tab-config", "tab-logs",
                    "tab-networking", "tab-settings", "tab-home"]:
            tabs.active = tid
            await pilot.pause()
            assert tabs.active == tid


@pytest.mark.asyncio
async def test_keyboard_shortcuts(mock_pc):
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import TabbedContent

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        tabs = app.query_one("#main-tabs", TabbedContent)
        for key, expected in [("2", "tab-plugins"), ("3", "tab-config"),
                              ("4", "tab-logs"), ("5", "tab-networking"),
                              ("6", "tab-settings"), ("1", "tab-home")]:
            await pilot.press(key)
            await pilot.pause()
            assert tabs.active == expected


@pytest.mark.asyncio
async def test_plugin_table_populates(mock_pc):
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import DataTable

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#plugin-table", DataTable).row_count == 2


@pytest.mark.asyncio
async def test_log_handler_attaches(mock_pc):
    from plugins_test.CLI.app import DashboardApp

    handler = TUILogHandler()
    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=handler)
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        assert handler._widget is not None


@pytest.mark.asyncio
async def test_log_table_columns(mock_pc):
    """Log DataTable should have Time, Level, Source, Message columns."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import DataTable

    handler = TUILogHandler()
    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=handler)
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        table = app.query_one("#log-table", DataTable)
        assert len(table.columns) == 4


# ═══════════════════════════════════════════════════════════════════════
# Phase 0 — Settings tab Networking group: peers display + B-069 rows
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_settings_networking_disabled_shows_placeholder(mock_pc):
    """When networking_enabled=False, disabled placeholder is visible and
    data rows are hidden. Mock fixture defaults networking_enabled=False."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import Static

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        placeholder = app.query_one("#settings-net-disabled", Static)
        data = app.query_one("#settings-net-data")
        assert placeholder.display is True
        assert data.display is False


@pytest.mark.asyncio
async def test_settings_networking_enabled_shows_peers(mock_pc):
    """When networking_enabled=True with peers configured, data rows
    visible, B-069 rows populated, label reads 'Peers:' not 'Node IPs:'."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import Static

    # Flip on networking + provide a YAML peers list (network=None still,
    # so _format_peers_display falls back to the YAML reader path).
    mock_pc.networking_enabled = True
    mock_pc.yaml_config = {
        "plugins": [],
        "general": {},
        "networking": {
            "enabled": True,
            "peers": [
                {"hostname": "peer-one", "ip": "10.0.0.1", "port": 2511},
                {"hostname": "peer-two", "ip": "10.0.0.2", "port": 2511},
            ],
        },
    }

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        placeholder = app.query_one("#settings-net-disabled", Static)
        data = app.query_one("#settings-net-data")
        assert placeholder.display is False
        assert data.display is True

        # B-069 interval rows populated.
        assert app.query_one("#info-net-heartbeat", Static).content == "10.0"
        assert app.query_one("#info-net-lookup", Static).content == "60.0"
        assert app.query_one("#info-net-liveness", Static).content == "30.0"

        # Peers value renders count + entries.
        peers_value = app.query_one("#info-net-nodes", Static).content
        assert peers_value.startswith("2 (")
        assert "peer-one @ 10.0.0.1:2511" in peers_value
        assert "peer-two @ 10.0.0.2:2511" in peers_value


@pytest.mark.asyncio
async def test_settings_peers_label_renamed(mock_pc):
    """The Settings Networking-group label reads 'Peers:' not 'Node IPs:'.
    Catches a missed PR4 K-3 cleanup if the rename ever regresses."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import Static

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        # Find the label paired with #info-net-nodes by walking the
        # Networking group's setting-row containers.
        labels = [
            s.content
            for s in app.query("#settings-net-data .setting-label").results(Static)
        ]
        assert "Peers:" in labels
        assert "Node IPs:" not in labels


# ═══════════════════════════════════════════════════════════════════════
# Phase 1 — Networking tab
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_networking_tab_disabled_shows_banner(mock_pc):
    """When networking_enabled=False, Networking tab shows the disabled
    banner and all data cards are hidden."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import Static

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        banner = app.query_one("#net-disabled-banner", Static)
        assert banner.display is True
        for cid in ("#net-this-node", "#net-discovery", "#net-peers",
                    "#net-bootstrap-card"):
            assert app.query_one(cid).display is False


@pytest.mark.asyncio
async def test_networking_tab_enabled_with_nm_populates_thisnode(mock_pc, tmp_path):
    """Networking on + NM present: This-Node table populated, banner
    hidden, peers table renders peer rows."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import Static, DataTable

    # Build a fake NetworkManager-like object exposing only the attrs
    # the Networking tab reads. The dataclasses are simple enough that
    # MagicMock would also work, but explicit fakes make assertions
    # stable across MagicMock auto-attr quirks.
    cert_file = tmp_path / "cert.pem"
    cert_file.write_text("FAKE-CERT-BODY", encoding="utf-8")

    class FakePeer:
        def __init__(self, hostname, ip, port, fingerprint, system_caller=False):
            self.hostname = hostname
            self.ip = ip
            self.port = port
            self.fingerprint = fingerprint
            self.system_caller = system_caller

    class FakeNode:
        def __init__(self, hostname, ip):
            self.hostname = hostname
            self.IP = ip
            self.last_heartbeat = int(time.time())  # alive now
        def is_alive_sync(self, timeout=30):
            return True

    class FakeNM:
        def __init__(self):
            self.peers = [
                FakePeer("peer-one", "10.0.0.1", 2511,
                         "fingerprint-aaaa-bbbb-cccc"),
                FakePeer("peer-two", "10.0.0.2", 2511,
                         "fingerprint-dddd-eeee-ffff",
                         system_caller=True),
            ]
            self.nodes = [
                FakeNode("peer-one", "10.0.0.1"),
                FakeNode("peer-two", "10.0.0.2"),
            ]
            self.keys_dir = tmp_path
            self.cert_path = cert_file
            self.own_fingerprint = "self-fp-1234567890ab"
            self.pool_size = 4
            self.connection_pools = {}
            self._inbound_adverts = {"peer-one": {"sub-1": object()}}
            self._outbound_adverts = {
                "peer-one": {"sub-2": object(), "sub-3": object()},
            }
            self._inflight_publishes = {"peer-two": {"req-1"}}
            self.peer_stats = {
                "peer-one": {
                    "bytes_sent": 1024, "bytes_recv": 2048,
                    "msgs_sent": 10, "msgs_recv": 12,
                },
            }
            self.liveness_timeout = 30
            self.discover_nodes = True

    mock_pc.networking_enabled = True
    mock_pc.network = FakeNM()

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await pilot.pause()  # peers worker dispatch

        assert app.query_one("#net-disabled-banner", Static).display is False
        assert app.query_one("#net-this-node").display is True
        assert app.query_one("#net-discovery").display is True
        assert app.query_one("#net-peers").display is True

        # Bootstrap card hidden because peers are configured.
        assert app.query_one("#net-bootstrap-card").display is False

        peers_table = app.query_one("#net-peers-table", DataTable)
        assert peers_table.row_count == 2

        # Phase 1 — This-Node + Discovery are now light label/value rows
        # (Static widgets), not DataTables.
        fp_row = app.query_one("#info-net-thisnode-fingerprint", Static)
        assert "self-fp-1234567890ab" in fp_row.content
        hostname_row = app.query_one("#info-net-thisnode-hostname", Static)
        assert hostname_row.content == "test-host"
        # Discovery rows populated with the mock_pc heartbeat_interval.
        assert app.query_one("#info-net-disc-hb", Static).content == "10.0"

        # The cert PEM Collapsibles are gone; cert content is exposed via
        # Phase 3 modal, not the DOM.
        from textual.css.query import NoMatches
        for stale_id in ("#net-cert-pem", "#net-cert-pem-collapsible",
                         "#net-bootstrap-pem", "#net-bootstrap-pem-collapsible"):
            try:
                app.query_one(stale_id)
                assert False, f"{stale_id} should be removed in Phase 1"
            except NoMatches:
                pass


@pytest.mark.asyncio
async def test_networking_tab_bootstrap_helper_visible_when_peers_empty(mock_pc, tmp_path):
    """Bootstrap card visible iff networking on + peers=[] + cert.pem
    exists on disk."""
    from plugins_test.CLI.app import DashboardApp

    cert_file = tmp_path / "cert.pem"
    cert_file.write_text("BOOTSTRAP-CERT", encoding="utf-8")

    class FakeNM:
        peers = []  # no peers configured yet
        nodes = []
        keys_dir = tmp_path
        cert_path = cert_file
        own_fingerprint = "boot-fp-aabbccdd"
        pool_size = 4
        connection_pools = {}
        _inbound_adverts = {}
        _outbound_adverts = {}
        _inflight_publishes = {}
        peer_stats = {}
        liveness_timeout = 30
        discover_nodes = True

    mock_pc.networking_enabled = True
    mock_pc.network = FakeNM()

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        assert app.query_one("#net-bootstrap-card").display is True


@pytest.mark.asyncio
async def test_phase1_home_network_section_removed(mock_pc):
    """Phase 1: the broken `Network Nodes` section + table are gone."""
    from plugins_test.CLI.app import DashboardApp
    from textual.css.query import NoMatches

    mock_pc.networking_enabled = True
    mock_pc.network = None

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        for stale_id in ("#network-section", "#network-table", "#network-empty"):
            try:
                app.query_one(stale_id)
                assert False, f"{stale_id} should be removed in Phase 1"
            except NoMatches:
                pass


@pytest.mark.asyncio
async def test_phase1_reload_button_removed(mock_pc):
    """Phase 1: the Networking-tab Reload button + status Static are gone
    (Config tab already carries an equivalent reload control)."""
    from plugins_test.CLI.app import DashboardApp
    from textual.css.query import NoMatches

    mock_pc.networking_enabled = True
    mock_pc.network = None

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        for stale_id in ("#btn-net-reload", "#net-thisnode-status",
                         "#net-thisnode-table", "#net-discovery-table"):
            try:
                app.query_one(stale_id)
                assert False, f"{stale_id} should be removed in Phase 1"
            except NoMatches:
                pass


@pytest.mark.asyncio
async def test_phase1_net_stat_card_states(mock_pc):
    """Home Net stat card renders the right text for each state.

    States covered:
      - networking disabled → 'OFF'
      - enabled, network=None (pre-start / mid-rebuild) → 'ON (N/A)'
      - enabled, alive nodes → 'ON, X/Y peers alive'
    """
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import Static

    # Case 1: networking OFF
    mock_pc.networking_enabled = False
    mock_pc.network = None
    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        for _ in range(3):  # let stats worker run
            await pilot.pause()
        assert "OFF" in str(app.query_one("#stat-networking", Static).content)

    # Case 2: networking ON but NM is None
    mock_pc.networking_enabled = True
    mock_pc.network = None
    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause()
        text = str(app.query_one("#stat-networking", Static).content)
        assert "N/A" in text

    # Case 3: networking ON with one alive peer out of two
    class FakePeer:
        def __init__(self, hostname):
            self.hostname = hostname
            self.ip = "10.0.0.1"
            self.port = 2511
            self.fingerprint = "fp"
            self.system_caller = False
            self.cert_pem = ""

    class FakeNode:
        def __init__(self, hostname, alive):
            self.hostname = hostname
            self.IP = "10.0.0.1"
            self.enabled = True
            self._alive = alive
            self.last_heartbeat = int(time.time()) if alive else None
        def is_alive_sync(self, timeout=30):
            return self._alive

    class FakeNM:
        peers = [FakePeer("peer-a"), FakePeer("peer-b")]
        nodes = [FakeNode("peer-a", True), FakeNode("peer-b", False)]
        liveness_timeout = 30

    mock_pc.networking_enabled = True
    mock_pc.network = FakeNM()
    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause()
        text = str(app.query_one("#stat-networking", Static).content)
        assert "1/2" in text and "peers alive" in text


@pytest.mark.asyncio
async def test_settings_peers_display_overflow_elided(mock_pc):
    """Peers display caps at 4 entries; overflow elided as '+N more'."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import Static

    mock_pc.networking_enabled = True
    mock_pc.yaml_config = {
        "plugins": [],
        "general": {},
        "networking": {
            "enabled": True,
            "peers": [
                {"hostname": f"p{i}", "ip": f"10.0.0.{i}", "port": 2511}
                for i in range(1, 7)  # 6 peers
            ],
        },
    }

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        peers_value = app.query_one("#info-net-nodes", Static).content
        assert peers_value.startswith("6 (")
        assert "+2 more" in peers_value


# ─── Phase 2 — plugin-side observer state + Networking tab additions ──────────

@pytest.mark.asyncio
async def test_phase2_plugin_observer_state():
    """The CLI plugin maintains the recent-events deque + disconnect-reason
    counters under `_observer_lock`. Test the snapshot helpers directly,
    not through the TUI (cheap + isolates the observer layer)."""
    from plugins_test.CLI.plugin import CLI
    import threading

    plugin = CLI.__new__(CLI)
    plugin._logger = MagicMock()
    plugin.on_load()  # initialises state

    # Initial state — empty + zeros.
    assert plugin.get_recent_peer_events() == []
    assert plugin.get_disconnect_reason_counts() == {
        "normal": 0, "connection_error": 0, "rce_attempt": 0, "error": 0,
    }
    # Lock is a real threading.Lock.
    assert isinstance(plugin._observer_lock, type(threading.Lock()))

    # Fire a synthetic disconnect — counter increments + deque appends.
    plugin._app = None  # bridge guard short-circuits cleanly
    plugin._on_peer_event(
        "_core/peer/disconnected",
        {"hostname": "peer-x", "reason": "rce_attempt", "ts": 100.0},
    )
    assert plugin.get_disconnect_reason_counts()["rce_attempt"] == 1
    events = plugin.get_recent_peer_events()
    assert len(events) == 1
    assert events[0][0] == "_core/peer/disconnected"

    # Connect event — appended but does not increment any counter.
    plugin._on_peer_event(
        "_core/peer/connected",
        {"hostname": "peer-x", "ip": "10.0.0.1", "ts": 101.0},
    )
    assert plugin.get_disconnect_reason_counts()["rce_attempt"] == 1
    assert len(plugin.get_recent_peer_events()) == 2

    # Unknown reason — counter NOT incremented (defensive).
    plugin._on_peer_event(
        "_core/peer/disconnected",
        {"hostname": "peer-x", "reason": "bogus_reason", "ts": 102.0},
    )
    counts = plugin.get_disconnect_reason_counts()
    assert sum(counts.values()) == 1  # still only the rce_attempt

    # Clear resets all four to 0.
    plugin.clear_disconnect_reason_counts()
    assert plugin.get_disconnect_reason_counts() == {
        "normal": 0, "connection_error": 0, "rce_attempt": 0, "error": 0,
    }


@pytest.mark.asyncio
async def test_phase2_cluster_summary_renders(mock_pc):
    """Cluster summary mounts and renders `N peers · X/Y alive · own_fp:...`."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import Static

    class FakePeer:
        def __init__(self, hostname):
            self.hostname = hostname
            self.ip = "10.0.0.1"
            self.port = 2511
            self.fingerprint = "fp"
            self.system_caller = False
            self.cert_pem = ""

    class FakeNode:
        def __init__(self, hostname, alive):
            self.hostname = hostname
            self.IP = "10.0.0.1"
            self.enabled = True
            self._alive = alive
            self.last_heartbeat = int(time.time()) if alive else None
        def is_alive_sync(self, timeout=30):
            return self._alive

    class FakeNM:
        peers = [FakePeer("peer-a"), FakePeer("peer-b"), FakePeer("peer-c")]
        nodes = [FakeNode("peer-a", True), FakeNode("peer-b", True),
                 FakeNode("peer-c", False)]
        own_fingerprint = "sha256:abcdef0123456789aabbccdd"
        liveness_timeout = 30
        heartbeat_interval = 10
        _outbound_adverts = {}
        _inbound_adverts = {}
        _inflight_publishes = {}
        peer_stats = {}
        connection_pools = {}
        keys_dir = Path("/tmp")
        cert_path = Path("/tmp/cert.pem")
        pool_size = 4

    mock_pc.networking_enabled = True
    mock_pc.network = FakeNM()

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause()
        text = app.query_one("#net-cluster-summary", Static).content
        assert "3 peers" in text
        assert "2/3 alive" in text
        assert "sha256:abcdef" in text


@pytest.mark.asyncio
async def test_phase2_counter_card_and_clear_button(mock_pc):
    """Counter mini-cards render disconnect-reason snapshots and the
    [Clear counters] button resets them via plugin-side state."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import Static

    # Build a real-ish plugin_instance — not MagicMock — so the observer
    # state + clear method actually work.
    class FakePlugin:
        def __init__(self):
            import threading as _t
            self._observer_lock = _t.Lock()
            self._counts = {"normal": 0, "connection_error": 0,
                            "rce_attempt": 2, "error": 0}
        def get_disconnect_reason_counts(self):
            with self._observer_lock:
                return dict(self._counts)
        def clear_disconnect_reason_counts(self):
            with self._observer_lock:
                for k in self._counts:
                    self._counts[k] = 0
        # Minimum surface DashboardApp accesses during init.
        plugin_name = "CLI"
        event_loop = None

    plugin = FakePlugin()
    mock_pc.networking_enabled = True
    mock_pc.network = None  # peers worker still runs, counters still updated

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=plugin,
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause()
        # Initial render reflects starting counters.
        assert app.query_one("#net-counter-discon-rce", Static).content == "2"
        # Invoke handler directly (the Networking tab + button may be
        # off-screen in headless mode; the goal is wiring, not pixel hit).
        app._on_clear_counters()
        for _ in range(3):
            await pilot.pause()
        assert plugin._counts["rce_attempt"] == 0
        assert app.query_one("#net-counter-discon-rce", Static).content == "0"


@pytest.mark.asyncio
async def test_phase2_event_log_writes_colored_lines(mock_pc):
    """Bus event → RichLog gets a colored line. Verify via
    `on_peer_event_bus` directly (which is the TUI-thread bridge target)."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import RichLog

    mock_pc.networking_enabled = True
    mock_pc.network = None

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        log = app.query_one("#net-event-log", RichLog)

        # Count writes via a wrapper around the public `write` method so
        # the test doesn't peek at private `_deferred_renders` (RichLog
        # defers writes until the widget knows its size — a private detail
        # that may change between Textual versions).
        write_count = 0
        original_write = log.write
        def _counting_write(*args, **kwargs):
            nonlocal write_count
            write_count += 1
            return original_write(*args, **kwargs)
        log.write = _counting_write  # type: ignore[method-assign]

        # Synthetic connected event.
        app.on_peer_event_bus("_core/peer/connected",
                              {"hostname": "peer-x", "ip": "10.0.0.1",
                               "ts": 100.0})
        for _ in range(2):
            await pilot.pause()
        # Synthetic disconnect with rce_attempt reason.
        app.on_peer_event_bus("_core/peer/disconnected",
                              {"hostname": "peer-x",
                               "reason": "rce_attempt", "ts": 101.0})
        for _ in range(2):
            await pilot.pause()
        # Two writes called on the public RichLog.write API.
        assert write_count == 2


@pytest.mark.asyncio
async def test_phase2_cert_expiry_row_renders(mock_pc, tmp_path):
    """Cert-expiry row renders 'in N days (...)' for a synthetic cert,
    with color class matching the days-remaining band."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import Static
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from datetime import datetime, timezone, timedelta

    # Build a self-signed cert that expires in 45 days (green band).
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "test-host")]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=45))
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "cert.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    class FakeNM:
        peers = []
        nodes = []
        keys_dir = tmp_path
        cert_path = cert_file
        own_fingerprint = "fp-aabbcc"
        pool_size = 4
        connection_pools = {}
        _inbound_adverts = {}
        _outbound_adverts = {}
        _inflight_publishes = {}
        peer_stats = {}
        liveness_timeout = 30
        heartbeat_interval = 10
        discover_nodes = False

    mock_pc.networking_enabled = True
    mock_pc.network = FakeNM()

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause()
        widget = app.query_one("#net-cert-expiry", Static)
        # 45 days in the future → green class.
        assert widget.has_class("cert-expiry-good")
        assert "days (own)" in str(widget.content)


# ─── Phase 4a — Per-peer drill-down tab ──────────────────────────────

def _make_phase4_fake_nm(tmp_path, *, host_count: int = 2):
    """Build a stand-in for `pc.network` sufficient for Phase 4a tests.

    Returns (FakeNM_instance, peers_list).
    """
    cert_file = tmp_path / "cert.pem"
    cert_file.write_text("OWN-CERT", encoding="utf-8")

    class FakePeer:
        def __init__(self, hostname):
            self.hostname = hostname
            self.ip = f"10.0.0.{hash(hostname) % 200 + 1}"
            self.port = 2511
            self.fingerprint = f"sha256:fp-{hostname}"
            self.cert_pem = f"PEER-PEM-{hostname}"
            self.system_caller = False

    class FakeNode:
        def __init__(self, hostname):
            self.hostname = hostname
            self.IP = f"10.0.0.{hash(hostname) % 200 + 1}"
            self.enabled = True
            self.last_heartbeat = int(time.time())
        def is_alive_sync(self, timeout=30):
            return True

    peer_list = [FakePeer(f"peer-{i}") for i in range(host_count)]
    node_list = [FakeNode(p.hostname) for p in peer_list]

    class FakeNM:
        peers = peer_list
        nodes = node_list
        keys_dir = tmp_path
        cert_path = cert_file
        own_fingerprint = "sha256:own-fp"
        pool_size = 4
        connection_pools = {}
        _inbound_adverts = {}
        _outbound_adverts = {}
        _inflight_publishes = {}
        peer_stats = {p.hostname: {
            "bytes_sent": 100, "bytes_recv": 200,
            "msgs_sent": 5, "msgs_recv": 8,
        } for p in peer_list}
        liveness_timeout = 30
        heartbeat_interval = 10
        discover_nodes = False

    return FakeNM(), peer_list


@pytest.mark.asyncio
async def test_phase4a_drill_down_opens_on_row_select(mock_pc, tmp_path):
    """Selecting a peers-table row spawns a drill-down TabPane keyed by
    hostname. Re-selecting the same row focuses the existing pane
    instead of stacking duplicates."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import TabbedContent, TabPane

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=2)
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause()
        host = peers[0].hostname
        # Open via the same code path the RowSelected handler uses.
        await app._open_peer_drill_down(host)
        for _ in range(3):
            await pilot.pause()
        assert host in app._peer_tabs
        tab_id = app._peer_tabs[host]
        # TabPane mounted.
        assert app.query_one(f"#{tab_id}", TabPane) is not None
        # Re-select — should be a no-op (no second pane).
        await app._open_peer_drill_down(host)
        await pilot.pause()
        assert len(app._peer_tabs) == 1


@pytest.mark.asyncio
async def test_phase4a_drill_down_cap_evicts_oldest(mock_pc, tmp_path):
    """The 6th distinct peer drill-down evicts the oldest open tab."""
    from plugins_test.CLI.app import DashboardApp

    nm, _ = _make_phase4_fake_nm(tmp_path, host_count=6)
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause()
        hosts = [p.hostname for p in nm.peers]
        # Open 5 — all fit in cap.
        for h in hosts[:5]:
            await app._open_peer_drill_down(h)
        await pilot.pause()
        assert len(app._peer_tabs) == 5
        # 6th evicts the FIRST opened.
        await app._open_peer_drill_down(hosts[5])
        await pilot.pause()
        assert len(app._peer_tabs) == 5
        assert hosts[0] not in app._peer_tabs
        assert hosts[5] in app._peer_tabs
        # Ring buffer for the evicted host is cleaned up.
        assert hosts[0] not in app._peer_ring_buffers


@pytest.mark.asyncio
async def test_phase4a_drill_down_sparkline_data_grows(mock_pc, tmp_path):
    """Each refresh tick appends a delta to the 4 throughput sparklines.
    Verify the deque length grows under repeated calls and stays
    capped at 60."""
    from plugins_test.CLI.app import DashboardApp

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        host = peers[0].hostname
        await app._open_peer_drill_down(host)
        for _ in range(3):
            await pilot.pause()

        rb = app._peer_ring_buffers[host]
        # Reset the ring buffer to a known starting state so the test is
        # deterministic regardless of whether `call_after_refresh` already
        # fired an initial tick under the test harness's pacing.
        for k in ("bytes_sent_delta", "bytes_recv_delta",
                  "msgs_sent_delta", "msgs_recv_delta"):
            rb[k].clear()
        rb["last_sample"] = None
        rb["last_sample_nm_id"] = None

        # First tick — last_sample is None → seeds zero-delta sample.
        app._refresh_one_peer_drilldown(host, nm, id(nm), time.time())
        assert len(rb["bytes_sent_delta"]) == 1
        assert rb["bytes_sent_delta"][0] == 0.0

        # Bump cumulative counters and tick again — delta appended.
        nm.peer_stats[host]["bytes_sent"] += 50
        app._refresh_one_peer_drilldown(host, nm, id(nm), time.time())
        assert len(rb["bytes_sent_delta"]) == 2
        assert rb["bytes_sent_delta"][-1] == 50.0


@pytest.mark.asyncio
async def test_phase4a_drill_down_closes_when_networking_disabled(mock_pc, tmp_path):
    """When networking flips off mid-session, all open drill-down tabs
    are closed by the shared refresh worker."""
    from plugins_test.CLI.app import DashboardApp

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=2)
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        await app._open_peer_drill_down(peers[0].hostname)
        await app._open_peer_drill_down(peers[1].hostname)
        for _ in range(3):
            await pilot.pause()
        assert len(app._peer_tabs) == 2

        # Flip networking off — refresh worker closes all peer tabs.
        mock_pc.networking_enabled = False
        await app._refresh_peer_drilldowns()
        for _ in range(3):
            await pilot.pause()
        assert app._peer_tabs == {}
        # Shared timer also stopped.
        assert app._peer_drill_timer is None


@pytest.mark.asyncio
async def test_phase4b_subs_tables_populate_from_advert_state(mock_pc, tmp_path):
    """Inbound + outbound subs DataTables render rows from the
    networking-side advert dicts."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import DataTable

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    host = peers[0].hostname

    # Plant synthetic AdvertSub-like objects in the inbound + outbound
    # advert dicts.
    class FakeSub:
        def __init__(self, topic_pattern, state="pending", retry=0,
                     sent_at=None, acked_at=None, hosts=None, authors=None):
            self.topic_pattern = topic_pattern
            self.state = state
            self.retry_count = retry
            self.sent_at = sent_at
            self.acked_at = acked_at
            self.hosts = hosts
            self.authors = authors

    inbound_uuid = "in-1234567890abcdef"
    outbound_uuid = "out-abcdef1234"
    nm._inbound_adverts = {host: {inbound_uuid: FakeSub(
        topic_pattern="llm/response", hosts="any", authors=None,
    )}}
    now = time.time()
    nm._outbound_adverts = {host: {outbound_uuid: FakeSub(
        topic_pattern="reminder/fire",
        state="acked",
        sent_at=now - 2.0,
        acked_at=now - 1.5,
        retry=0,
    )}}

    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(180, 60)) as pilot:
        await pilot.pause()
        await app._open_peer_drill_down(host)
        for _ in range(3):
            await pilot.pause()
        # Force a refresh tick so subs are pulled from the synthetic NM.
        app._refresh_one_peer_drilldown(host, nm, id(nm), time.time())
        await pilot.pause()
        tab_id = app._peer_tabs[host]
        in_tbl = app.query_one(f"#{tab_id}-subs-in", DataTable)
        out_tbl = app.query_one(f"#{tab_id}-subs-out", DataTable)
        assert in_tbl.row_count == 1
        assert out_tbl.row_count == 1


@pytest.mark.asyncio
async def test_phase4b_inflight_count_renders(mock_pc, tmp_path):
    """In-flight publishes count Static reflects the size of
    `nm._inflight_publishes[host]`."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import Static

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    host = peers[0].hostname
    # Synthetic set of 3 in-flight task placeholders (just need a set).
    nm._inflight_publishes = {host: {object(), object(), object()}}

    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(180, 60)) as pilot:
        await pilot.pause()
        await app._open_peer_drill_down(host)
        for _ in range(3):
            await pilot.pause()
        app._refresh_one_peer_drilldown(host, nm, id(nm), time.time())
        await pilot.pause()
        tab_id = app._peer_tabs[host]
        text = app.query_one(f"#{tab_id}-inflight", Static).content
        assert "In-flight publishes: 3" in str(text)


@pytest.mark.asyncio
async def test_phase4b_per_peer_log_filters_to_host(mock_pc, tmp_path):
    """Per-peer event log gets new lines only for events whose payload
    hostname matches the drill-down's peer."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import RichLog
    import collections as _c

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=2)
    host_a = peers[0].hostname
    host_b = peers[1].hostname

    # Real-ish plugin so the baseline-gated dedup gate sees a deque and
    # `len()` works (a MagicMock plugin's `_recent_peer_events` is an
    # auto-attr that `len()` raises on, forcing the dedup to block).
    class FakePlugin:
        plugin_name = "CLI"
        event_loop = None
        def __init__(self):
            import threading as _t
            self._observer_lock = _t.Lock()
            self._recent_peer_events: _c.deque = _c.deque(maxlen=500)
        def get_recent_peer_events(self):
            return list(self._recent_peer_events)
        def get_disconnect_reason_counts(self):
            return {"normal": 0, "connection_error": 0,
                    "rce_attempt": 0, "error": 0}

    plugin = FakePlugin()
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=plugin,
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(180, 60)) as pilot:
        await pilot.pause()
        await app._open_peer_drill_down(host_a)
        for _ in range(3):
            await pilot.pause()
        tab_id = app._peer_tabs[host_a]

        # Count writes on the per-peer log via wrapper (avoid private
        # `_deferred_renders` peek; see Phase 2 event-log test).
        log = app.query_one(f"#{tab_id}-eventlog", RichLog)
        log_writes = 0
        original_write = log.write
        def _counting_write(*args, **kwargs):
            nonlocal log_writes
            log_writes += 1
            return original_write(*args, **kwargs)
        log.write = _counting_write  # type: ignore[method-assign]

        # Mimic the observer: append to plugin deque BEFORE the bus call
        # (real `_on_peer_event` appends then bridges). The dedup gate
        # compares len(deque) > baseline.
        # Event for OUR host → log appended.
        evt_a = {"hostname": host_a, "reason": "rce_attempt",
                 "ts": time.time()}
        plugin._recent_peer_events.append(("_core/peer/disconnected", evt_a))
        app.on_peer_event_bus("_core/peer/disconnected", evt_a)
        # Event for the OTHER host → log NOT touched.
        evt_b = {"hostname": host_b, "ip": "10.0.0.2",
                 "ts": time.time()}
        plugin._recent_peer_events.append(("_core/peer/connected", evt_b))
        app.on_peer_event_bus("_core/peer/connected", evt_b)
        for _ in range(2):
            await pilot.pause()
        assert log_writes == 1


@pytest.mark.asyncio
async def test_phase4b_baseline_dedup_skips_hydrated_events(mock_pc, tmp_path):
    """Events captured in the plugin deque BEFORE the drill-down mount
    are hydrated by `_hydrate_peer_log`; their subsequent arrival via
    `on_peer_event_bus` MUST NOT re-render them. The baseline-gated
    dedup blocks the duplicate."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import RichLog
    import collections as _c

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    host = peers[0].hostname

    # Real-ish plugin with a populated deque so hydration runs end-to-end.
    class FakePlugin:
        plugin_name = "CLI"
        event_loop = None
        def __init__(self):
            import threading as _t
            self._observer_lock = _t.Lock()
            self._recent_peer_events: _c.deque = _c.deque(maxlen=500)
        def get_recent_peer_events(self):
            return list(self._recent_peer_events)
        def get_disconnect_reason_counts(self):
            return {"normal": 0, "connection_error": 0,
                    "rce_attempt": 0, "error": 0}

    plugin = FakePlugin()
    # Plant a pre-mount disconnect event in the deque — will be hydrated.
    pre_event = ("_core/peer/disconnected",
                 {"hostname": host, "reason": "rce_attempt",
                  "ts": time.time()})
    plugin._recent_peer_events.append(pre_event)

    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=plugin,
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(180, 60)) as pilot:
        await pilot.pause()
        await app._open_peer_drill_down(host)
        for _ in range(4):
            await pilot.pause()
        # Hydration: the pre-mount event was rendered once. Baseline == 1.
        assert app._peer_log_baselines[host] == 1

        # Simulate the bus dispatch for the same pre-mount event.
        # `on_peer_event_bus` must skip this — len(deque) (1) is NOT
        # greater than baseline (1).
        tab_id = app._peer_tabs[host]
        log = app.query_one(f"#{tab_id}-eventlog", RichLog)
        log_writes = 0
        original_write = log.write
        def _counting_write(*args, **kwargs):
            nonlocal log_writes
            log_writes += 1
            return original_write(*args, **kwargs)
        log.write = _counting_write  # type: ignore[method-assign]

        app.on_peer_event_bus(*pre_event)
        for _ in range(2):
            await pilot.pause()
        assert log_writes == 0  # baseline blocked the dupe

        # Now a NEW event — observer appends + bus dispatches.
        new_event = ("_core/peer/connected",
                     {"hostname": host, "ip": "10.0.0.1",
                      "ts": time.time()})
        plugin._recent_peer_events.append(new_event[1])  # mimic observer append
        app.on_peer_event_bus(*new_event)
        for _ in range(2):
            await pilot.pause()
        assert log_writes == 1  # post-baseline event rendered


# ─── Phase 4a — Per-peer drill-down tab (continued) ──────────────────

@pytest.mark.asyncio
async def test_phase4a_drill_down_gone_title_on_disconnect(mock_pc, tmp_path):
    """A `_core/peer/disconnected` event for an open peer flips the tab
    label to `<host> (gone)`; a subsequent reconnect restores it."""
    from plugins_test.CLI.app import DashboardApp
    from textual.widgets import TabbedContent

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        host = peers[0].hostname
        await app._open_peer_drill_down(host)
        for _ in range(3):
            await pilot.pause()
        tabs = app.query_one("#main-tabs", TabbedContent)
        tab_id = app._peer_tabs[host]
        # Baseline: label is just the hostname.
        assert str(tabs.get_tab(tab_id).label) == host
        # Disconnect → title gains the `(gone)` suffix.
        app.on_peer_event_bus("_core/peer/disconnected",
                              {"hostname": host, "reason": "normal",
                               "ts": time.time()})
        for _ in range(2):
            await pilot.pause()
        assert "(gone)" in str(tabs.get_tab(tab_id).label)
        # Reconnect → label restored.
        app.on_peer_event_bus("_core/peer/connected",
                              {"hostname": host, "ip": "10.0.0.1",
                               "ts": time.time()})
        for _ in range(2):
            await pilot.pause()
        assert str(tabs.get_tab(tab_id).label) == host


@pytest.mark.asyncio
async def test_phase4a_drill_down_view_cert_opens_peer_modal(mock_pc, tmp_path):
    """The drill-down `View cert` button opens a CertPEMScreen carrying
    the PEER's cert PEM + fingerprint (not the own cert)."""
    from plugins_test.CLI.app import DashboardApp, CertPEMScreen

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        host = peers[0].hostname
        await app._open_peer_drill_down(host)
        for _ in range(3):
            await pilot.pause()
        app._open_peer_cert_modal(host)
        for _ in range(3):
            await pilot.pause()
        modal = next((s for s in app.screen_stack
                      if isinstance(s, CertPEMScreen)), None)
        assert modal is not None
        assert modal._pem == f"PEER-PEM-{host}"
        assert modal._fp == f"sha256:fp-{host}"


# ─── Phase 3 — Cert PEM modal ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase3_view_cert_button_opens_modal(mock_pc, tmp_path):
    """Pressing the This-Node `View cert` button pushes a CertPEMScreen
    pre-populated with the own cert PEM + fingerprint."""
    from plugins_test.CLI.app import DashboardApp, CertPEMScreen

    cert_file = tmp_path / "cert.pem"
    cert_file.write_text("MOCK-OWN-PEM", encoding="utf-8")

    class FakeNM:
        peers = []
        nodes = []
        keys_dir = tmp_path
        cert_path = cert_file
        own_fingerprint = "sha256:own-fp-aabb"
        pool_size = 4
        connection_pools = {}
        _inbound_adverts = {}
        _outbound_adverts = {}
        _inflight_publishes = {}
        peer_stats = {}
        liveness_timeout = 30
        heartbeat_interval = 10
        discover_nodes = False

    mock_pc.networking_enabled = True
    mock_pc.network = FakeNM()

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        # No modal on top of the stack yet.
        assert not any(isinstance(s, CertPEMScreen) for s in app.screen_stack)
        # Trigger the handler directly (button hit-test may be off-screen
        # in headless mode; goal is wiring + modal payload, not pixel hit).
        app._on_view_thisnode_cert()
        for _ in range(3):
            await pilot.pause()
        modal = next((s for s in app.screen_stack
                      if isinstance(s, CertPEMScreen)), None)
        assert modal is not None
        # Modal carries own PEM + own fingerprint.
        assert modal._pem == "MOCK-OWN-PEM"
        assert modal._fp == "sha256:own-fp-aabb"
        # Esc dismisses (via Screen.action_dismiss inherited binding).
        await modal.action_dismiss()
        for _ in range(3):
            await pilot.pause()
        assert not any(isinstance(s, CertPEMScreen) for s in app.screen_stack)


@pytest.mark.asyncio
async def test_phase3_close_button_dismisses(mock_pc, tmp_path):
    """The modal's [Close] button calls `dismiss()` and pops the modal."""
    from plugins_test.CLI.app import DashboardApp, CertPEMScreen

    cert_file = tmp_path / "cert.pem"
    cert_file.write_text("CLOSE-TEST-PEM", encoding="utf-8")

    class FakeNM:
        peers = []
        nodes = []
        keys_dir = tmp_path
        cert_path = cert_file
        own_fingerprint = "sha256:close-fp"
        pool_size = 4
        connection_pools = {}
        _inbound_adverts = {}
        _outbound_adverts = {}
        _inflight_publishes = {}
        peer_stats = {}
        liveness_timeout = 30
        heartbeat_interval = 10
        discover_nodes = False

    mock_pc.networking_enabled = True
    mock_pc.network = FakeNM()

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        app._on_view_thisnode_cert()
        for _ in range(3):
            await pilot.pause()
        modal = next((s for s in app.screen_stack
                      if isinstance(s, CertPEMScreen)), None)
        assert modal is not None
        modal._on_close()
        for _ in range(3):
            await pilot.pause()
        assert not any(isinstance(s, CertPEMScreen) for s in app.screen_stack)


@pytest.mark.asyncio
async def test_phase3_double_push_guard(mock_pc, tmp_path):
    """Rapid double-press of View cert opens only ONE modal — the
    second call is a no-op while a modal is already on the stack."""
    from plugins_test.CLI.app import DashboardApp, CertPEMScreen

    cert_file = tmp_path / "cert.pem"
    cert_file.write_text("DUP-PEM", encoding="utf-8")

    class FakeNM:
        peers = []
        nodes = []
        keys_dir = tmp_path
        cert_path = cert_file
        own_fingerprint = "sha256:dup-fp"
        pool_size = 4
        connection_pools = {}
        _inbound_adverts = {}
        _outbound_adverts = {}
        _inflight_publishes = {}
        peer_stats = {}
        liveness_timeout = 30
        heartbeat_interval = 10
        discover_nodes = False

    mock_pc.networking_enabled = True
    mock_pc.network = FakeNM()

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        app._on_view_thisnode_cert()
        app._on_view_thisnode_cert()  # double-press
        for _ in range(3):
            await pilot.pause()
        modal_count = sum(1 for s in app.screen_stack
                          if isinstance(s, CertPEMScreen))
        assert modal_count == 1


@pytest.mark.asyncio
async def test_phase3_bootstrap_view_cert_button_opens_modal(mock_pc, tmp_path):
    """The Bootstrap card's `View bootstrap PEM` button reuses the same
    modal, populated with the same own cert PEM + fingerprint."""
    from plugins_test.CLI.app import DashboardApp, CertPEMScreen

    cert_file = tmp_path / "cert.pem"
    cert_file.write_text("BOOTSTRAP-OWN-PEM", encoding="utf-8")

    class FakeNM:
        peers = []
        nodes = []
        keys_dir = tmp_path
        cert_path = cert_file
        own_fingerprint = "sha256:boot-fp"
        pool_size = 4
        connection_pools = {}
        _inbound_adverts = {}
        _outbound_adverts = {}
        _inflight_publishes = {}
        peer_stats = {}
        liveness_timeout = 30
        heartbeat_interval = 10
        discover_nodes = False

    mock_pc.networking_enabled = True
    mock_pc.network = FakeNM()

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        app._on_view_bootstrap_cert()
        for _ in range(3):
            await pilot.pause()
        modal = next((s for s in app.screen_stack
                      if isinstance(s, CertPEMScreen)), None)
        assert modal is not None
        assert modal._pem == "BOOTSTRAP-OWN-PEM"
        assert modal._title == "Bootstrap — local certificate"


@pytest.mark.asyncio
async def test_phase3_modal_copy_button_invokes_clipboard(mock_pc, tmp_path):
    """Pressing [Copy PEM] in the modal calls App.copy_to_clipboard
    with the PEM body."""
    from plugins_test.CLI.app import DashboardApp, CertPEMScreen
    from unittest.mock import patch

    cert_file = tmp_path / "cert.pem"
    cert_file.write_text("COPY-TEST-PEM", encoding="utf-8")

    class FakeNM:
        peers = []
        nodes = []
        keys_dir = tmp_path
        cert_path = cert_file
        own_fingerprint = "sha256:copy-fp"
        pool_size = 4
        connection_pools = {}
        _inbound_adverts = {}
        _outbound_adverts = {}
        _inflight_publishes = {}
        peer_stats = {}
        liveness_timeout = 30
        heartbeat_interval = 10
        discover_nodes = False

    mock_pc.networking_enabled = True
    mock_pc.network = FakeNM()

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        app._on_view_thisnode_cert()
        for _ in range(3):
            await pilot.pause()
        modal = next((s for s in app.screen_stack
                      if isinstance(s, CertPEMScreen)), None)
        assert modal is not None
        with patch.object(app, "copy_to_clipboard") as mock_copy:
            modal._on_copy()
            mock_copy.assert_called_once_with("COPY-TEST-PEM")


@pytest.mark.asyncio
async def test_phase3_view_cert_when_nm_is_none(mock_pc):
    """If `pc.network` is None (pre-NM / mid-rebuild), the modal opens
    with placeholder text instead of crashing on missing `cert_path`."""
    from plugins_test.CLI.app import DashboardApp, CertPEMScreen

    mock_pc.networking_enabled = True
    mock_pc.network = None

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        app._on_view_thisnode_cert()
        for _ in range(3):
            await pilot.pause()
        modal = next((s for s in app.screen_stack
                      if isinstance(s, CertPEMScreen)), None)
        assert modal is not None
        assert "NM not built" in modal._fp
        assert "NetworkManager not built" in modal._pem


@pytest.mark.asyncio
async def test_phase2_disable_hides_all_new_cards(mock_pc):
    """Mid-session networking flip from ON → OFF hides every new card."""
    from plugins_test.CLI.app import DashboardApp

    mock_pc.networking_enabled = True
    mock_pc.network = None

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="CLI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        # Confirm cards visible when enabled.
        assert app.query_one("#net-cluster-summary").display is True
        assert app.query_one("#net-counters").display is True
        assert app.query_one("#net-event-log").display is True

        # Flip to disabled and re-run populate.
        mock_pc.networking_enabled = False
        app._populate_networking_static()
        await pilot.pause()
        assert app.query_one("#net-cluster-summary").display is False
        assert app.query_one("#net-counters").display is False
        assert app.query_one("#net-event-log").display is False
        assert app.query_one("#net-disabled-banner").display is True
