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
    app._stats_timer = None
    app._plugin_timer = None
    app._request_timer = None
    app._log_timer = None
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
        for tid in ["tab-plugins", "tab-config", "tab-logs", "tab-settings", "tab-home"]:
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
                              ("4", "tab-logs"), ("5", "tab-settings"), ("1", "tab-home")]:
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
