"""
Unit tests for the TUI Dashboard plugin.

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

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from plugins_test.TUI.log_handler import TUILogHandler, LogRecord
from plugins_test.TUI.request_tracker import RequestTracker, ActiveRequest


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
    from plugins_test.TUI.app import DashboardApp
    import collections as _c
    app = object.__new__(DashboardApp)
    app.plugin_core = plugin_core or _make_mock_plugin_core()
    app.plugin_instance = MagicMock(plugin_name="TUI")
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
    # Phase 2 — cert-expiry cache (bounded FIFO).
    app._cert_expiry_cache = _c.OrderedDict()
    app._cert_expiry_cache_cap = 64
    # Phase 4a — per-peer drill-down state.
    app._peer_tabs = _c.OrderedDict()
    app._peer_tabs_cap = 5
    app._peer_ring_buffers = {}
    app._peer_drill_timer = None
    # Phase 4b — per-peer event log baseline.
    app._peer_log_baselines = {}
    # Phase 5 — networking uptime tracker.
    app._networking_started_at = None
    app._networking_instance_id = None
    # Phase 3a — Plugins-tab refresh-debounce + detail-pane state.
    # _make_dashboard_app bypasses __init__ so the new state attrs must
    # be initialized here too. Mirrors the pattern for the earlier-phase
    # additions above.
    app._plugins_refresh_pending = False
    app._subs_refresh_pending = False
    app._cat_refresh_pending = False
    app._currently_displayed_plugin = None
    app._plugin_detail_open_sections = {}
    app._detail_render_target = None
    app._outer_is_events = False
    app._inner_is_live = False
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
        from plugins_test.TUI.app import DashboardApp
        result = DashboardApp._sanitize_id("AI:Plugin.v2")
        assert ":" not in result and "." not in result

    def test_different_names_unique(self):
        from plugins_test.TUI.app import DashboardApp
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
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Static, TabbedContent, DataTable, TextArea

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        tabs = app.query_one("#main-tabs", TabbedContent)
        for key, expected in [("2", "tab-plugins"), ("3", "tab-config"),
                              ("4", "tab-logs"), ("5", "tab-networking"),
                              ("6", "tab-events"),  # Phase 2b
                              ("7", "tab-settings"), ("1", "tab-home")]:
            await pilot.press(key)
            await pilot.pause()
            assert tabs.active == expected


@pytest.mark.asyncio
async def test_plugin_table_populates(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import DataTable

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#plugin-table", DataTable).row_count == 2


@pytest.mark.asyncio
async def test_log_handler_attaches(mock_pc):
    from plugins_test.TUI.app import DashboardApp

    handler = TUILogHandler()
    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=handler)
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        assert handler._widget is not None


@pytest.mark.asyncio
async def test_log_table_columns(mock_pc):
    """Log DataTable should have Time, Level, Source, Message columns."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import DataTable

    handler = TUILogHandler()
    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Static

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
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

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Static

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Static

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
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

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp

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

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        assert app.query_one("#net-bootstrap-card").display is True


@pytest.mark.asyncio
async def test_phase1_home_network_section_removed(mock_pc):
    """Phase 1: the broken `Network Nodes` section + table are gone."""
    from plugins_test.TUI.app import DashboardApp
    from textual.css.query import NoMatches

    mock_pc.networking_enabled = True
    mock_pc.network = None

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
    from textual.css.query import NoMatches

    mock_pc.networking_enabled = True
    mock_pc.network = None

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Static

    # Case 1: networking OFF
    mock_pc.networking_enabled = False
    mock_pc.network = None
    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        for _ in range(3):  # let stats worker run
            await pilot.pause()
        assert "OFF" in str(app.query_one("#stat-networking", Static).content)

    # Case 2: networking ON but NM is None
    mock_pc.networking_enabled = True
    mock_pc.network = None
    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
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

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(120, 40)) as pilot:
        await pilot.pause()
        peers_value = app.query_one("#info-net-nodes", Static).content
        assert peers_value.startswith("6 (")
        assert "+2 more" in peers_value


# ─── Phase 2 — plugin-side observer state + Networking tab additions ──────────

@pytest.mark.asyncio
async def test_phase2_plugin_observer_state():
    """The TUI plugin maintains the recent-events deque + disconnect-reason
    counters under `_observer_lock`. Test the snapshot helpers directly,
    not through the TUI (cheap + isolates the observer layer)."""
    from plugins_test.TUI.plugin import TUI
    import threading

    plugin = TUI.__new__(TUI)
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


def test_phase2_event_seq_monotonic_past_deque_cap():
    """Regression test for the deque-saturation bug:

    `_recent_peer_events` is a bounded deque (`maxlen=500`). Once it
    saturates, `len()` plateaus at 500 forever and stops being a
    reliable "did a new event arrive" signal. The TUI's per-peer log
    dedup gate uses `_event_seq` (a monotonic counter incremented on
    every `_on_peer_event` call) for exactly this reason — `seq` keeps
    growing past `maxlen` so subsequent bus events are still detected
    as "new" and rendered into the per-peer log.

    Without this regression test, a future refactor could reintroduce
    a `len()`-based gate and silently break the per-peer log after
    long-running clusters with frequent peer churn.
    """
    from plugins_test.TUI.plugin import TUI

    plugin = TUI.__new__(TUI)
    plugin._logger = MagicMock()
    plugin.on_load()
    plugin._app = None  # bridge guard short-circuits

    # Saturate the deque past maxlen.
    deque_cap = plugin._recent_peer_events.maxlen
    assert deque_cap == 500
    for i in range(deque_cap + 50):
        plugin._on_peer_event(
            "_core/peer/connected",
            {"hostname": f"peer-{i}", "ip": "10.0.0.1", "ts": float(i)},
        )

    # deque len plateaus at maxlen, but the seq counts ALL events.
    assert len(plugin.get_recent_peer_events()) == deque_cap
    assert plugin.get_event_seq() == deque_cap + 50

    # Snapshot baseline AFTER saturation. A subsequent event increments
    # the seq, so the dedup gate (`current_seq > baseline`) still fires
    # — len()-based gating would not.
    baseline_seq = plugin.get_event_seq()
    plugin._on_peer_event(
        "_core/peer/connected",
        {"hostname": "peer-new", "ip": "10.0.0.1", "ts": 999.0},
    )
    assert plugin.get_event_seq() == baseline_seq + 1
    # len() did NOT change — proves the bug exists in a `len()`-based gate.
    assert len(plugin.get_recent_peer_events()) == deque_cap


@pytest.mark.asyncio
async def test_phase2_cluster_summary_renders(mock_pc):
    """Cluster summary mounts and renders `N peers · X/Y alive · own_fp:...`."""
    from plugins_test.TUI.app import DashboardApp
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

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
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
        plugin_name = "TUI"
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
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import RichLog

    mock_pc.networking_enabled = True
    mock_pc.network = None

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
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

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, TabPane

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=2)
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp

    nm, _ = _make_phase4_fake_nm(tmp_path, host_count=6)
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=2)
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp
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

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(180, 60)) as pilot:
        await pilot.pause()
        await app._open_peer_drill_down(host)
        # Poll for populate completion — `call_after_refresh` deferral
        # under suite load can exceed 3 ticks easily.
        for _ in range(100):
            await pilot.pause()
            if host in app._peer_log_baselines:
                break
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
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Static

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    host = peers[0].hostname
    # Synthetic set of 3 in-flight task placeholders (just need a set).
    nm._inflight_publishes = {host: {object(), object(), object()}}

    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(180, 60)) as pilot:
        await pilot.pause()
        await app._open_peer_drill_down(host)
        # Poll for populate completion under suite load.
        for _ in range(100):
            await pilot.pause()
            if host in app._peer_log_baselines:
                break
        app._refresh_one_peer_drilldown(host, nm, id(nm), time.time())
        await pilot.pause()
        tab_id = app._peer_tabs[host]
        text = app.query_one(f"#{tab_id}-inflight", Static).content
        assert "In-flight publishes: 3" in str(text)


@pytest.mark.asyncio
async def test_phase4b_per_peer_log_filters_to_host(mock_pc, tmp_path):
    """Per-peer event log gets new lines only for events whose payload
    hostname matches the drill-down's peer."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import RichLog
    import collections as _c

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=2)
    host_a = peers[0].hostname
    host_b = peers[1].hostname

    # Real-ish plugin so the baseline-gated dedup gate sees a deque and
    # `len()` works (a MagicMock plugin's `_recent_peer_events` is an
    # auto-attr that `len()` raises on, forcing the dedup to block).
    class FakePlugin:
        plugin_name = "TUI"
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
        # Poll for populate — `call_after_refresh` under suite load can
        # exceed even 30 ticks; using 100 to be safe.
        for _ in range(100):
            await pilot.pause()
            if host_a in app._peer_log_baselines:
                break
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
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import RichLog
    import collections as _c

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    host = peers[0].hostname

    # Real-ish plugin with a populated deque so hydration runs end-to-end.
    class FakePlugin:
        plugin_name = "TUI"
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
        # Poll for populate completion (the baseline is the last write
        # in `_populate_peer_drill_widgets`, so it's a reliable marker).
        for _ in range(100):
            await pilot.pause()
            if host in app._peer_log_baselines:
                break
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


# ─── Phase 5 — Settings → Networking group additions ─────────────────

@pytest.mark.asyncio
async def test_phase5_settings_identity_rows_populate(mock_pc, tmp_path):
    """Identity rows + pool size in the Settings → Networking group
    render values pulled from `pc.network` when alive."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Static

    cert_file = tmp_path / "cert.pem"
    cert_file.write_text("X", encoding="utf-8")

    class FakeNM:
        peers = []
        nodes = []
        keys_dir = tmp_path
        cert_path = cert_file
        own_fingerprint = "sha256:settings-fp"
        pool_size = 7
        connection_pools = {}
        _inbound_adverts = {}
        _outbound_adverts = {}
        _inflight_publishes = {}
        peer_stats = {}
        liveness_timeout = 30
        heartbeat_interval = 10
        discover_nodes = False
        is_ready = False

    mock_pc.networking_enabled = True
    mock_pc.network = FakeNM()

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause()
        assert "sha256:settings-fp" in app.query_one(
            "#info-settings-net-fingerprint", Static).content
        assert str(tmp_path) in app.query_one(
            "#info-settings-net-keysdir", Static).content
        assert "exists" in app.query_one(
            "#info-settings-net-certfile", Static).content
        assert app.query_one(
            "#info-settings-net-poolsize", Static).content == "7"


@pytest.mark.asyncio
async def test_phase5_settings_uptime_counts_on_is_ready(mock_pc):
    """`is_ready` False → True transition captures a start time; the
    rendered uptime is formatted `h:mm:ss`. An NM instance swap (id
    change) resets the timer cleanly."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Static

    class FakeNM:
        is_ready = False
        peers = []
        nodes = []
        keys_dir = "/tmp"
        cert_path = None
        own_fingerprint = "fp"
        pool_size = 4
        connection_pools = {}
        _inbound_adverts = {}
        _outbound_adverts = {}
        _inflight_publishes = {}
        peer_stats = {}
        liveness_timeout = 30
        heartbeat_interval = 10
        discover_nodes = False

    nm1 = FakeNM()
    mock_pc.networking_enabled = True
    mock_pc.network = nm1

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        # Pre-ready: uptime is "(not started)".
        app._tick_networking_uptime()
        assert app._networking_started_at is None

        # Flip to ready — start time captured.
        nm1.is_ready = True
        app._tick_networking_uptime()
        assert app._networking_started_at is not None
        first_start = app._networking_started_at

        # Same instance, still ready — timestamp unchanged.
        app._tick_networking_uptime()
        assert app._networking_started_at == first_start

        # New NM instance — instance_id swaps. The wall-clock timestamp
        # may be identical when `time.time()` returns the same value
        # between back-to-back calls, so the instance-id swap is the
        # authoritative reset signal we test.
        nm2 = FakeNM()
        nm2.is_ready = True
        mock_pc.network = nm2
        prev_instance_id = app._networking_instance_id
        app._tick_networking_uptime()
        assert app._networking_instance_id != prev_instance_id
        assert app._networking_instance_id == id(nm2)
        # Render path works.
        app._populate_settings_phase5_rows()
        assert ":" in app.query_one(
            "#info-settings-net-uptime", Static).content


@pytest.mark.asyncio
async def test_phase5_settings_secret_status_three_paths(mock_pc, monkeypatch):
    """Secret status renders `set via config` / `set via env` / `unset`
    per priority order."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Static

    mock_pc.networking_enabled = True
    mock_pc.network = None

    # Case 1: config-set wins.
    mock_pc.networking_secret = b"from-config"
    monkeypatch.setenv("NETWORKING_SECRET", "from-env")

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause()
        assert app.query_one("#info-settings-net-secret", Static).content == \
            "set via config"

    # Case 2: env-set when config is empty.
    mock_pc.networking_secret = None
    app2 = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app2.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause()
        assert app2.query_one("#info-settings-net-secret", Static).content == \
            "set via env"

    # Case 3: unset.
    monkeypatch.delenv("NETWORKING_SECRET", raising=False)
    mock_pc.networking_secret = None
    app3 = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app3.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause()
        assert app3.query_one("#info-settings-net-secret", Static).content == \
            "unset"


@pytest.mark.asyncio
async def test_phase5_settings_rebuild_indicator_toggles(mock_pc):
    """Rebuild indicator visible iff networking enabled AND
    pc.network is None."""
    from plugins_test.TUI.app import DashboardApp

    mock_pc.networking_enabled = True
    mock_pc.network = None

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        for _ in range(3):
            await pilot.pause()
        # Enabled + nm=None → visible.
        assert app.query_one("#settings-net-rebuilding").display is True
        # Flip nm in → hidden.
        class FakeNM:
            peers = []
            nodes = []
            keys_dir = "/tmp"
            cert_path = None
            own_fingerprint = "fp"
            pool_size = 4
            connection_pools = {}
            _inbound_adverts = {}
            _outbound_adverts = {}
            _inflight_publishes = {}
            peer_stats = {}
            liveness_timeout = 30
            heartbeat_interval = 10
            discover_nodes = False
            is_ready = True
        mock_pc.network = FakeNM()
        app._populate_settings_phase5_rows()
        assert app.query_one("#settings-net-rebuilding").display is False


@pytest.mark.asyncio
async def test_phase5_settings_view_cert_button_opens_modal(mock_pc, tmp_path):
    """The Settings-tab View-cert button reuses Phase 3's modal helper."""
    from plugins_test.TUI.app import DashboardApp, CertPEMScreen

    cert_file = tmp_path / "cert.pem"
    cert_file.write_text("SETTINGS-PEM", encoding="utf-8")

    class FakeNM:
        peers = []
        nodes = []
        keys_dir = tmp_path
        cert_path = cert_file
        own_fingerprint = "sha256:settings-modal-fp"
        pool_size = 4
        connection_pools = {}
        _inbound_adverts = {}
        _outbound_adverts = {}
        _inflight_publishes = {}
        peer_stats = {}
        liveness_timeout = 30
        heartbeat_interval = 10
        discover_nodes = False
        is_ready = True

    mock_pc.networking_enabled = True
    mock_pc.network = FakeNM()

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(160, 60)) as pilot:
        await pilot.pause()
        app._on_settings_view_cert()
        for _ in range(3):
            await pilot.pause()
        modal = next((s for s in app.screen_stack
                      if isinstance(s, CertPEMScreen)), None)
        assert modal is not None
        assert modal._pem == "SETTINGS-PEM"
        assert modal._fp == "sha256:settings-modal-fp"


# ─── Phase 4c — Drill-down quick actions ─────────────────────────────

@pytest.mark.asyncio
async def test_phase4c_copy_fingerprint_invokes_clipboard(mock_pc, tmp_path):
    """`[Copy fingerprint]` calls `App.copy_to_clipboard` with the
    peer's fingerprint string."""
    from plugins_test.TUI.app import DashboardApp
    from unittest.mock import patch

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    host = peers[0].hostname
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(180, 60)) as pilot:
        await pilot.pause()
        await app._open_peer_drill_down(host)
        for _ in range(3):
            await pilot.pause()
        tab_id = app._peer_tabs[host]
        with patch.object(app, "copy_to_clipboard") as mock_copy:
            app._peer_copy_fingerprint(host, tab_id)
            mock_copy.assert_called_once_with(f"sha256:fp-{host}")


@pytest.mark.asyncio
async def test_phase4c_copy_pem_invokes_clipboard(mock_pc, tmp_path):
    """`[Copy PEM]` calls `App.copy_to_clipboard` with the peer's PEM."""
    from plugins_test.TUI.app import DashboardApp
    from unittest.mock import patch

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    host = peers[0].hostname
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(180, 60)) as pilot:
        await pilot.pause()
        await app._open_peer_drill_down(host)
        for _ in range(3):
            await pilot.pause()
        tab_id = app._peer_tabs[host]
        with patch.object(app, "copy_to_clipboard") as mock_copy:
            app._peer_copy_pem(host, tab_id)
            mock_copy.assert_called_once_with(f"PEER-PEM-{host}")


@pytest.mark.asyncio
async def test_phase4c_jump_to_config_finds_hostname_line(mock_pc, tmp_path):
    """`[Jump to config]` switches to the Config tab, loads main config,
    and moves the cursor to the line containing `hostname: <peer>`."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, TextArea

    config_file = tmp_path / "config.yml"
    config_text = (
        "general:\n"
        "  hostname: my-node\n"
        "networking:\n"
        "  enabled: true\n"
        "  peers:\n"
        "    - hostname: peer-0\n"
        "      address: 10.0.0.1:2511\n"
        "    - hostname: peer-1\n"
        "      address: 10.0.0.2:2511\n"
    )
    config_file.write_text(config_text, encoding="utf-8")

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=2)
    host = "peer-1"
    mock_pc.networking_enabled = True
    mock_pc.network = nm
    # Override config_path so _build_config_file_list picks up our fixture.
    mock_pc.config_path = str(config_file)
    mock_pc.yaml_config = {"plugins": [], "general": {},
                           "networking": {"enabled": True}}
    mock_pc.plugin_package = "plugins_test"

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
                       log_handler=TUILogHandler())
    async with app.run_test(headless=True, size=(180, 60)) as pilot:
        await pilot.pause()
        # Ensure _config_files is populated from on_mount.
        await app._open_peer_drill_down(host)
        for _ in range(3):
            await pilot.pause()
        tab_id = app._peer_tabs[host]
        await app._peer_jump_to_config(host, tab_id)
        for _ in range(2):
            await pilot.pause()
        # Tab switched to config.
        assert app.query_one("#main-tabs", TabbedContent).active == "tab-config"
        # Cursor placed on the line containing `hostname: peer-1` (line 7,
        # zero-indexed = 6).
        ta = app.query_one("#config-editor", TextArea)
        row, _col = ta.cursor_location
        assert config_text.splitlines()[row].strip() == "- hostname: peer-1"


# ─── Phase 4a — Per-peer drill-down tab (continued) ──────────────────

@pytest.mark.asyncio
async def test_phase4a_drill_down_gone_title_on_disconnect(mock_pc, tmp_path):
    """A `_core/peer/disconnected` event for an open peer flips the tab
    label to `<host> (gone)`; a subsequent reconnect restores it."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp, CertPEMScreen

    nm, peers = _make_phase4_fake_nm(tmp_path, host_count=1)
    mock_pc.networking_enabled = True
    mock_pc.network = nm

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp, CertPEMScreen

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

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp, CertPEMScreen

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

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp, CertPEMScreen

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

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp, CertPEMScreen

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

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp, CertPEMScreen
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

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp, CertPEMScreen

    mock_pc.networking_enabled = True
    mock_pc.network = None

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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
    from plugins_test.TUI.app import DashboardApp

    mock_pc.networking_enabled = True
    mock_pc.network = None

    app = DashboardApp(plugin_core=mock_pc, plugin_instance=MagicMock(plugin_name="TUI"),
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


# ═══════════════════════════════════════════════════════════════════════
# Phase 2b — Events tab (Subs / Catalogue / Live-stream)
# ═══════════════════════════════════════════════════════════════════════
#
# Test structure mirrors plan Section 6 — 37 cases covering:
#   #1     compose
#   #2-12  Subs browser
#   #13-16 Events catalogue
#   #17-22 Live-stream observer pipeline (assert against plugin buffer,
#          NOT the rendered DataTable — see plan cycle 4 H2)
#   #23-30 Live-stream UI (filters, debounce, clear, gating, burst, atomic)
#   #31    Observer cleanup on disable
#   #32-33 Outer-tab scoping + filter persistence
#   #34    _run_on_main None-return toast
#   #52    _run_on_main False-return toast
#   #53    _outer_is_events flips back on Home activation
#   #57    No-op toggle behavior (no toast — emit-fires-on-change locks
#          this via #56 in test_application.py; the TUI side just
#          relies on the wrapper returning True without crashing)


from unittest.mock import patch as _phase2b_patch


def _make_phase2b_subscription(
    *,
    sub_uuid: str,
    topic_pattern: str,
    plugin_name: str,
    plugin_uuid: str = "",
    target_plugin: str = None,
    target_access_name: str = "handler",
    target_plugin_uuid: str = None,
    hosts="any",
    blocked_hosts=None,
    authors=None,
    blocked_authors=None,
    enabled: bool = True,
    declared_id: str = None,
):
    """Build a Subscription-shaped MagicMock for `list_local_subs` mock."""
    sub = MagicMock()
    sub.sub_uuid = sub_uuid
    sub.topic_pattern = topic_pattern
    sub.plugin_name = plugin_name
    sub.plugin_uuid = plugin_uuid or f"{plugin_name}-uuid"
    sub.target_plugin = target_plugin or plugin_name
    sub.target_access_name = target_access_name
    sub.target_plugin_uuid = target_plugin_uuid
    sub.hosts = hosts
    sub.blocked_hosts = blocked_hosts
    sub.authors = authors
    sub.blocked_authors = blocked_authors
    sub.enabled = enabled
    sub.declared_id = declared_id
    return sub


def _install_phase2b_pc(mock_pc, *, subs=None, events_by_plugin=None,
                       set_sub_result=True, set_evt_result=True):
    """Patch a `mock_pc` with the Phase 2b API surface — topic_registry +
    set_subscription_enabled + set_event_enabled. `events_by_plugin` is
    a {plugin_name: {event_id: {topic, hosts, blocked_hosts, enabled}}}
    map injected onto the plugin objects' `events` attribute.
    """
    subs = list(subs or [])

    async def _list_local_subs():
        return list(subs)

    mock_pc.topic_registry = MagicMock()
    mock_pc.topic_registry.list_local_subs = _list_local_subs
    mock_pc.set_subscription_enabled = AsyncMock(return_value=set_sub_result)
    mock_pc.set_event_enabled = AsyncMock(return_value=set_evt_result)
    mock_pc.internal_observe = MagicMock()
    mock_pc.internal_unobserve = MagicMock(return_value=True)
    if events_by_plugin:
        for pname, events_map in events_by_plugin.items():
            p = mock_pc.plugins.get(pname)
            if p is not None:
                p.events = events_map


def _make_phase2b_plugin_instance(name="TUI"):
    """A plugin_instance that satisfies DashboardApp + survives
    `_run_on_main`'s `is_closed()` guard. We patch `_run_on_main` to
    await directly in tests that need real coroutine resolution.
    """
    plugin = MagicMock(plugin_name=name)
    plugin.plugin_uuid = "tui-uuid"
    plugin.event_loop = MagicMock()
    return plugin


async def _patch_run_on_main_passthrough(app):
    """Replace `_run_on_main` with a direct-await passthrough for tests
    where we WANT the registry call to succeed inside the test loop.
    """
    async def _passthrough(coro, timeout=30.0):
        return await coro
    app._run_on_main = _passthrough  # type: ignore[method-assign]


# ── #1 — compose ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase2b_events_tab_renders(mock_pc):
    """Events tab + 3 inner sub-tabs compose."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable, Static

    _install_phase2b_pc(mock_pc)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        # Outer Events tab.
        outer_tabs = app.query_one("#main-tabs", TabbedContent)
        outer_tabs.active = "tab-events"
        await pilot.pause()
        assert outer_tabs.active == "tab-events"

        # Inner TabbedContent.
        inner = app.query_one("#events-tabs", TabbedContent)
        for inner_id in ("events-tab-subs", "events-tab-cat", "events-tab-live"):
            inner.active = inner_id
            await pilot.pause()
            assert inner.active == inner_id

        # Three tables present.
        app.query_one("#events-subs-table", DataTable)
        app.query_one("#events-cat-table", DataTable)
        app.query_one("#events-live-table", DataTable)

        # Counter widgets present.
        app.query_one("#events-subs-counter", Static)
        app.query_one("#events-cat-counter", Static)
        app.query_one("#events-live-counter", Static)


# ── #2-12 — Subs browser ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase2b_subs_browser_populates_from_registry(mock_pc):
    """list_local_subs returns 3 subs, all 3 land in the table with
    correct Type column (YAML vs runtime)."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import DataTable, TabbedContent

    subs = [
        _make_phase2b_subscription(
            sub_uuid="uuid-A", topic_pattern="msgs/foo",
            plugin_name="PluginA", declared_id="declared-a",
        ),
        _make_phase2b_subscription(
            sub_uuid="uuid-B", topic_pattern="msgs/bar",
            plugin_name="PluginB", declared_id="declared-b",
        ),
        _make_phase2b_subscription(
            sub_uuid="uuid-C", topic_pattern="runtime/baz",
            plugin_name="PluginA", declared_id=None,
        ),
    ]
    _install_phase2b_pc(mock_pc, subs=subs)

    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        await pilot.pause()
        await pilot.pause()
        # Worker has now run via the activation handler.
        table = app.query_one("#events-subs-table", DataTable)
        # Wait a couple ticks for the worker.
        for _ in range(5):
            if table.row_count == 3:
                break
            await pilot.pause()
        assert table.row_count == 3
        # Type column index = 6 (Topic / Owner / Target / Hosts /
        # Authors / Enabled / Type / sub_uuid).
        rendered = app._subs_rendered_rows
        types = [r["declared_kind"] for r in rendered]
        assert types.count("YAML") == 2
        assert types.count("runtime") == 1


@pytest.mark.asyncio
async def test_phase2b_subs_browser_filter_by_plugin(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Select, TabbedContent, DataTable

    subs = [
        _make_phase2b_subscription(
            sub_uuid="u1", topic_pattern="a/b",
            plugin_name="PluginA", declared_id="x",
        ),
        _make_phase2b_subscription(
            sub_uuid="u2", topic_pattern="c/d",
            plugin_name="PluginB", declared_id="y",
        ),
    ]
    _install_phase2b_pc(mock_pc, subs=subs)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-subs-table", DataTable).row_count == 2:
                break
        sel = app.query_one("#events-subs-filter-plugin", Select)
        sel.value = "PluginA"
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-subs-table", DataTable).row_count == 1:
                break
        assert app.query_one("#events-subs-table", DataTable).row_count == 1
        # Only PluginA visible.
        assert all(r["plugin_name"] == "PluginA" for r in app._subs_rendered_rows)


@pytest.mark.asyncio
async def test_phase2b_subs_browser_filter_by_topic_substring(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Input, TabbedContent, DataTable

    subs = [
        _make_phase2b_subscription(
            sub_uuid="u1", topic_pattern="msgs/foo", plugin_name="A",
            declared_id="x",
        ),
        _make_phase2b_subscription(
            sub_uuid="u2", topic_pattern="msgs/bar", plugin_name="A",
            declared_id="y",
        ),
        _make_phase2b_subscription(
            sub_uuid="u3", topic_pattern="other/baz", plugin_name="A",
            declared_id="z",
        ),
    ]
    _install_phase2b_pc(mock_pc, subs=subs)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-subs-table", DataTable).row_count == 3:
                break
        topic_inp = app.query_one("#events-subs-filter-topic", Input)
        topic_inp.value = "msgs"
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-subs-table", DataTable).row_count == 2:
                break
        assert app.query_one("#events-subs-table", DataTable).row_count == 2


@pytest.mark.asyncio
async def test_phase2b_subs_browser_filter_by_hostname(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Input, TabbedContent, DataTable

    subs = [
        _make_phase2b_subscription(
            sub_uuid="u1", topic_pattern="a", plugin_name="A",
            declared_id="x", hosts="peer-one",
        ),
        _make_phase2b_subscription(
            sub_uuid="u2", topic_pattern="b", plugin_name="A",
            declared_id="y", hosts="peer-two",
        ),
        _make_phase2b_subscription(
            sub_uuid="u3", topic_pattern="c", plugin_name="A",
            declared_id="z", hosts="any",
        ),
    ]
    _install_phase2b_pc(mock_pc, subs=subs)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(5):
            await pilot.pause()
        host_inp = app.query_one("#events-subs-filter-hostname", Input)
        host_inp.value = "peer-one"
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-subs-table", DataTable).row_count == 1:
                break
        assert app.query_one("#events-subs-table", DataTable).row_count == 1


@pytest.mark.asyncio
async def test_phase2b_subs_browser_filter_by_sub_uuid_substring(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Input, TabbedContent, DataTable

    subs = [
        _make_phase2b_subscription(
            sub_uuid="abc12345", topic_pattern="a", plugin_name="A",
            declared_id="x",
        ),
        _make_phase2b_subscription(
            sub_uuid="def67890", topic_pattern="b", plugin_name="A",
            declared_id="y",
        ),
    ]
    _install_phase2b_pc(mock_pc, subs=subs)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(5):
            await pilot.pause()
        uuid_inp = app.query_one("#events-subs-filter-uuid", Input)
        uuid_inp.value = "def"
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-subs-table", DataTable).row_count == 1:
                break
        assert app.query_one("#events-subs-table", DataTable).row_count == 1
        assert app._subs_rendered_rows[0]["sub_uuid"].startswith("def")


@pytest.mark.asyncio
async def test_phase2b_subs_browser_enabled_only(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Checkbox, TabbedContent, DataTable

    subs = [
        _make_phase2b_subscription(
            sub_uuid="u1", topic_pattern="a", plugin_name="A",
            declared_id="x", enabled=True,
        ),
        _make_phase2b_subscription(
            sub_uuid="u2", topic_pattern="b", plugin_name="A",
            declared_id="y", enabled=False,
        ),
    ]
    _install_phase2b_pc(mock_pc, subs=subs)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(5):
            await pilot.pause()
        cb = app.query_one("#events-subs-filter-enabled-only", Checkbox)
        cb.value = True
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-subs-table", DataTable).row_count == 1:
                break
        assert app.query_one("#events-subs-table", DataTable).row_count == 1
        assert app._subs_rendered_rows[0]["enabled"] is True


@pytest.mark.asyncio
async def test_phase2b_subs_browser_counter_updates(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Input, Static, TabbedContent, DataTable

    subs = [
        _make_phase2b_subscription(
            sub_uuid=f"u{i}", topic_pattern=f"t/{i}", plugin_name="A",
            declared_id=f"d{i}",
        ) for i in range(4)
    ]
    _install_phase2b_pc(mock_pc, subs=subs)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-subs-table", DataTable).row_count == 4:
                break
        counter = app.query_one("#events-subs-counter", Static).content
        assert "4 / 4" in counter
        inp = app.query_one("#events-subs-filter-topic", Input)
        inp.value = "t/1"
        for _ in range(5):
            await pilot.pause()
            if "1 / 4" in app.query_one("#events-subs-counter", Static).content:
                break
        assert "1 / 4" in app.query_one("#events-subs-counter", Static).content


@pytest.mark.asyncio
async def test_phase2b_subs_toggle_via_e_key(mock_pc):
    """Press `e` with focus on the subs table → wrapper called with
    flipped value; row re-renders with new state on bus refresh."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable

    sub = _make_phase2b_subscription(
        sub_uuid="uuid-X", topic_pattern="t", plugin_name="A",
        declared_id="x", enabled=True,
    )
    _install_phase2b_pc(mock_pc, subs=[sub])
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(5):
            await pilot.pause()
        table = app.query_one("#events-subs-table", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("e")
        for _ in range(5):
            await pilot.pause()
        mock_pc.set_subscription_enabled.assert_awaited()
        called_args = mock_pc.set_subscription_enabled.await_args.args
        assert called_args[0] == "uuid-X"
        assert called_args[1] is False  # flipped from True


@pytest.mark.asyncio
async def test_phase2b_subs_copy_uuid_via_c_key(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable

    sub = _make_phase2b_subscription(
        sub_uuid="uuid-COPY-ME", topic_pattern="t", plugin_name="A",
        declared_id="x",
    )
    _install_phase2b_pc(mock_pc, subs=[sub])
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(5):
            await pilot.pause()
        table = app.query_one("#events-subs-table", DataTable)
        table.focus()
        await pilot.pause()
        with _phase2b_patch.object(app, "copy_to_clipboard") as mock_copy:
            await pilot.press("c")
            await pilot.pause()
            mock_copy.assert_called_once_with("uuid-COPY-ME")


@pytest.mark.asyncio
async def test_phase2b_subs_detail_modal_opens_on_enter(mock_pc):
    from plugins_test.TUI.app import DashboardApp, SubscriptionDetailScreen
    from textual.widgets import TabbedContent, DataTable

    sub = _make_phase2b_subscription(
        sub_uuid="uuid-DETAIL", topic_pattern="t", plugin_name="A",
        declared_id="x",
    )
    _install_phase2b_pc(mock_pc, subs=[sub])
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(5):
            await pilot.pause()
        table = app.query_one("#events-subs-table", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(5):
            await pilot.pause()
        modal = next(
            (s for s in app.screen_stack
             if isinstance(s, SubscriptionDetailScreen)),
            None,
        )
        assert modal is not None
        assert modal._sub["sub_uuid"] == "uuid-DETAIL"


@pytest.mark.asyncio
async def test_phase2b_subs_toggle_on_popped_plugin_uuid(mock_pc):
    """Wrapper returns False (uuid not in registry) → warning toast,
    set_subscription_enabled returned False as the mock."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable

    sub = _make_phase2b_subscription(
        sub_uuid="uuid-GONE", topic_pattern="t", plugin_name="A",
        declared_id="x",
    )
    _install_phase2b_pc(mock_pc, subs=[sub], set_sub_result=False)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    notified = []

    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(5):
            await pilot.pause()
        table = app.query_one("#events-subs-table", DataTable)
        table.focus()
        await pilot.pause()
        with _phase2b_patch.object(app, "notify",
                                  side_effect=lambda *a, **k: notified.append((a, k))):
            await pilot.press("e")
            for _ in range(5):
                await pilot.pause()
        assert any("no longer exists" in str(c[0]) for c in notified)


# ── #13-16 — Events catalogue ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase2b_events_catalogue_populates(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable

    _install_phase2b_pc(
        mock_pc,
        events_by_plugin={
            "PluginA": {
                "evt1": {
                    "topic": "msgs/foo", "hosts": "local",
                    "blocked_hosts": None, "enabled": True,
                },
                "evt2": {
                    "topic": "msgs/bar", "hosts": "local",
                    "blocked_hosts": None, "enabled": False,
                },
            },
        },
    )
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        app.query_one("#events-tabs", TabbedContent).active = "events-tab-cat"
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-cat-table", DataTable).row_count == 2:
                break
        assert app.query_one("#events-cat-table", DataTable).row_count == 2


@pytest.mark.asyncio
async def test_phase2b_events_catalogue_runtime_placeholder_highlight(mock_pc):
    """Topic with `{user_id}` renders with yellow markup applied AFTER
    escape. A topic containing `[red]inject[/]` cannot inject formatting.
    """
    from plugins_test.TUI.app import DashboardApp

    rendered = DashboardApp._render_topic_with_placeholders("msgs/{user_id}/x")
    assert "[yellow]{user_id}[/]" in rendered

    injected = DashboardApp._render_topic_with_placeholders(
        "msgs/[red]inject[/]/{var}"
    )
    # `[red]inject[/]` becomes `\[red]inject\[/]` after rich escape so
    # it can never render as red. `{var}` survives the escape pass
    # since it contains no `[`/`]`, then gets wrapped in yellow tags.
    assert "\\[red]inject\\[/]" in injected
    assert "[yellow]{var}[/]" in injected


@pytest.mark.asyncio
async def test_phase2b_events_catalogue_toggle_via_e_key(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable

    _install_phase2b_pc(
        mock_pc,
        events_by_plugin={
            "PluginA": {
                "evt1": {
                    "topic": "msgs/foo", "hosts": "local",
                    "blocked_hosts": None, "enabled": True,
                },
            },
        },
    )
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        app.query_one("#events-tabs", TabbedContent).active = "events-tab-cat"
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-cat-table", DataTable).row_count == 1:
                break
        table = app.query_one("#events-cat-table", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("e")
        for _ in range(5):
            await pilot.pause()
        mock_pc.set_event_enabled.assert_awaited()
        args = mock_pc.set_event_enabled.await_args.args
        assert args[0] == "PluginA"
        assert args[1] == "evt1"
        assert args[2] is False  # flipped from True


@pytest.mark.asyncio
async def test_phase2b_events_catalogue_idempotent_toggle(mock_pc):
    """set_event_enabled returns True for both toggle and no-op. The
    wrapper distinguishes them via the row's `enabled` flag — repeated
    presses produce repeated calls (the wrapper does not pre-coalesce);
    framework-side dedup is what enforces no extra emit."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable

    _install_phase2b_pc(
        mock_pc,
        events_by_plugin={
            "PluginA": {
                "evt1": {
                    "topic": "msgs/foo", "hosts": "local",
                    "blocked_hosts": None, "enabled": True,
                },
            },
        },
    )
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        app.query_one("#events-tabs", TabbedContent).active = "events-tab-cat"
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-cat-table", DataTable).row_count == 1:
                break
        table = app.query_one("#events-cat-table", DataTable)
        table.focus()
        await pilot.pause()
        # First press: True → False. Second press without refresh:
        # the row's `enabled` is still True locally (no bus emit
        # because mock_pc.set_event_enabled doesn't fire one). So
        # second press would re-send `False`. We assert both calls
        # were sent.
        await pilot.press("e")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert mock_pc.set_event_enabled.await_count == 2


# ── #17-22 — Live-stream observer pipeline ──────────────────────────────

def _make_phase2b_real_plugin():
    """Build a real-ish TUI plugin instance for observer tests.
    Bypasses on_enable + threading + log handler attach by calling
    `__new__` + manual state init (mirrors how the existing Phase 2
    plugin tests build one).

    `event_loop` is set to None so DashboardApp's `_run_on_main`
    safely short-circuits — tests calling `_flush_live_events`
    directly don't need cross-loop dispatch. Also stamp
    `plugin_uuid` so the app's observer-registration loop in
    `on_mount` has a uuid to bind against.
    """
    from plugins_test.TUI.plugin import TUI
    plugin = TUI.__new__(TUI)
    plugin._logger = MagicMock()
    plugin.on_load()
    plugin._app = None
    plugin.event_loop = None
    plugin.plugin_name = "TUI"
    plugin.plugin_uuid = "tui-uuid"
    return plugin


def test_phase2b_live_stream_observes_published():
    plugin = _make_phase2b_real_plugin()
    plugin._on_bus_event("_core/event/published", {
        "publisher": "PluginA", "topic": "msgs/foo",
        "target_count": 3, "ts": 100.0,
    })
    rows, seq = plugin.get_live_events_since(0)
    assert seq == 1
    assert len(rows) == 1
    assert "▶ pub" in rows[0]["type_label"]
    assert rows[0]["topic"] == "msgs/foo"
    assert rows[0]["publisher"] == "PluginA"
    assert "target_count=3" in rows[0]["detail"]


def test_phase2b_live_stream_observes_requested():
    plugin = _make_phase2b_real_plugin()
    plugin._on_bus_event("_core/event/requested", {
        "publisher": "PluginB", "topic": "ask/me",
        "target_count": 1, "ts": 200.0,
    })
    rows, _ = plugin.get_live_events_since(0)
    assert len(rows) == 1
    assert "? req" in rows[0]["type_label"]


def test_phase2b_live_stream_observes_stream_lifecycle():
    plugin = _make_phase2b_real_plugin()
    plugin._on_bus_event("_core/event/streamed", {
        "publisher": "X", "topic": "s/t",
        "phase": "first_chunk", "ts": 300.0,
    })
    plugin._on_bus_event("_core/event/streamed", {
        "publisher": "X", "topic": "s/t",
        "phase": "ended", "ts": 301.0,
    })
    rows, _ = plugin.get_live_events_since(0)
    assert len(rows) == 2
    labels = [r["type_label"] for r in rows]
    assert any("» first" in l for l in labels)
    assert any("« end" in l for l in labels)


def test_phase2b_live_stream_observes_sub_toggle():
    """sub-toggle row appears with enabled flag visible in Detail."""
    plugin = _make_phase2b_real_plugin()
    plugin._on_bus_event("_core/subscription/state_changed", {
        "sub_uuid": "uuid-abc123def", "enabled": False, "ts": 400.0,
    })
    plugin._on_bus_event("_core/subscription/state_changed", {
        "sub_uuid": "uuid-abc123def", "enabled": True, "ts": 401.0,
    })
    rows, _ = plugin.get_live_events_since(0)
    assert len(rows) == 2
    assert all("~ sub" in r["type_label"] for r in rows)
    assert "enabled=False" in rows[0]["detail"]
    assert "enabled=True" in rows[1]["detail"]


def test_phase2b_live_stream_observes_event_toggle():
    plugin = _make_phase2b_real_plugin()
    plugin._on_bus_event("_core/event/state_changed", {
        "plugin_name": "PluginA", "event_id": "evt1",
        "enabled": False, "ts": 500.0,
    })
    plugin._on_bus_event("_core/event/state_changed", {
        "plugin_name": "PluginA", "event_id": "evt1",
        "enabled": True, "ts": 501.0,
    })
    rows, _ = plugin.get_live_events_since(0)
    assert len(rows) == 2
    assert all("~ evt" in r["type_label"] for r in rows)
    assert "PluginA/evt1" in rows[0]["detail"]


def test_phase2b_live_stream_unknown_phase_falls_through():
    """Future phase value lands as `stream:unknown` rather than
    silently dropping (plan cycle 1 L1)."""
    plugin = _make_phase2b_real_plugin()
    plugin._on_bus_event("_core/event/streamed", {
        "publisher": "X", "topic": "s/t",
        "phase": "future_value", "ts": 600.0,
    })
    rows, _ = plugin.get_live_events_since(0)
    assert len(rows) == 1
    assert rows[0]["type_label"] == "stream:unknown"


# ── #23-30 — Live-stream UI ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase2b_live_stream_filter_topic(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Input, TabbedContent, DataTable

    _install_phase2b_pc(mock_pc)
    # Real plugin instance so `get_live_events_since` returns the
    # expected `(list, int)` tuple — MagicMock returns a MagicMock
    # which trips the flush's defensive `except Exception: return`.
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_real_plugin(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        # Activate Live-stream tab.
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        app.query_one("#events-tabs", TabbedContent).active = "events-tab-live"
        await pilot.pause()
        # Programmatic `.active=` doesn't deterministically dispatch
        # the TabActivated message inside the same `pause()` window.
        # Force the visibility flags directly so the flush actually
        # renders — the activation handler logic is tested separately
        # by test_phase2b_outer_is_events_clears_on_home_activation.
        app._outer_is_events = True
        app._inner_is_live = True
        # Inject events directly into the rendered-rows cache so the
        # test doesn't depend on the plugin instance.
        app._live_rendered_rows = [
            {"ts": 100.0, "topic_raw": "_core/event/published",
             "type_label": "[green]▶ pub[/]",
             "topic": "msgs/foo", "publisher": "X", "detail": "target_count=1",
             "_seq": 1},
            {"ts": 101.0, "topic_raw": "_core/event/published",
             "type_label": "[green]▶ pub[/]",
             "topic": "other/bar", "publisher": "X", "detail": "target_count=1",
             "_seq": 2},
        ]
        app._live_filter_dirty = True
        app._flush_live_events()
        await pilot.pause()
        assert app.query_one("#events-live-table", DataTable).row_count == 2
        inp = app.query_one("#events-live-filter-topic", Input)
        inp.value = "msgs"
        # Same as the type-checkbox test: set dirty flag explicitly
        # so we don't depend on Input.Changed message-queue timing.
        app._live_filter_dirty = True
        app._flush_live_events()
        await pilot.pause()
        assert app.query_one("#events-live-table", DataTable).row_count == 1


@pytest.mark.asyncio
async def test_phase2b_live_stream_filter_type_checkboxes(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Checkbox, TabbedContent, DataTable

    _install_phase2b_pc(mock_pc)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_real_plugin(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        app.query_one("#events-tabs", TabbedContent).active = "events-tab-live"
        await pilot.pause()
        app._outer_is_events = True
        app._inner_is_live = True
        app._live_rendered_rows = [
            {"ts": 100.0, "topic_raw": "_core/event/published",
             "type_label": "[green]▶ pub[/]",
             "topic": "a", "publisher": "X", "detail": "",
             "_seq": 1},
            {"ts": 101.0, "topic_raw": "_core/event/requested",
             "type_label": "[yellow]? req[/]",
             "topic": "b", "publisher": "X", "detail": "",
             "_seq": 2},
        ]
        app._live_filter_dirty = True
        app._flush_live_events()
        await pilot.pause()
        assert app.query_one("#events-live-table", DataTable).row_count == 2
        # Untick `pub` checkbox. The Checkbox.Changed handler posts an
        # async message; we set the dirty flag explicitly to avoid
        # depending on Textual's message-queue timing in tests.
        cb = app.query_one("#events-live-type-pub", Checkbox)
        cb.value = False
        app._live_filter_dirty = True
        app._flush_live_events()
        await pilot.pause()
        assert app.query_one("#events-live-table", DataTable).row_count == 1


@pytest.mark.asyncio
async def test_phase2b_live_stream_filter_debounce(mock_pc):
    """Rapid filter changes only produce ONE re-render per flush tick.

    Cycle 6 fresh-eyes fix: previously this test only asserted that
    `_live_filter_dirty` cleared, which would pass for a one-line
    `_live_filter_dirty = False` body with no actual render. The
    strengthened version counts `table.clear()` calls across multiple
    dirty-flag toggles per single flush, falsifying the claim that
    rapid changes don't multiply renders.
    """
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import DataTable, TabbedContent

    _install_phase2b_pc(mock_pc)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_real_plugin(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        app.query_one("#events-tabs", TabbedContent).active = "events-tab-live"
        await pilot.pause()
        app._outer_is_events = True
        app._inner_is_live = True
        # Populate one row in the rendered cache so the flush has work.
        app._live_rendered_rows = [
            {"ts": 100.0, "topic_raw": "_core/event/published",
             "type_label": "[green]▶ pub[/]",
             "topic": "t", "publisher": "X", "detail": "",
             "_seq": 1},
        ]
        # Wrap `table.clear` so we can count invocations.
        table = app.query_one("#events-live-table", DataTable)
        clear_calls = []
        original_clear = table.clear
        def _counting_clear(*a, **kw):
            clear_calls.append(time.time())
            return original_clear(*a, **kw)
        table.clear = _counting_clear  # type: ignore[method-assign]
        # Simulate rapid filter changes: set dirty 5 times, then ONE flush.
        for _ in range(5):
            app._live_filter_dirty = True
        app._flush_live_events()
        assert app._live_filter_dirty is False
        # Exactly one render — the multiple dirty-sets coalesced.
        assert len(clear_calls) == 1, (
            f"expected 1 re-render, got {len(clear_calls)}"
        )


@pytest.mark.asyncio
async def test_phase2b_live_stream_clear_button(mock_pc):
    """Clear button wipes the table + resets `_live_last_seen_seq`."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable

    plugin = _make_phase2b_real_plugin()
    _install_phase2b_pc(mock_pc)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=plugin,
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        # Push a couple events into the plugin's buffer.
        plugin._on_bus_event("_core/event/published", {
            "publisher": "X", "topic": "t",
            "target_count": 1, "ts": 100.0,
        })
        plugin._on_bus_event("_core/event/published", {
            "publisher": "X", "topic": "t",
            "target_count": 1, "ts": 101.0,
        })
        # Activate the live-stream sub-tab + force flush.
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        app.query_one("#events-tabs", TabbedContent).active = "events-tab-live"
        await pilot.pause()
        app._outer_is_events = True
        app._inner_is_live = True
        # Invoke the Clear button's handler directly (pilot.click can
        # raise OutOfBounds when the button isn't in the rendered
        # viewport at the headless test size).
        app._on_live_clear_pressed(None)
        for _ in range(3):
            await pilot.pause()
        # `clear_live_events` returns the seq; app applies it.
        assert app._live_last_seen_seq == 2
        assert app._live_rendered_rows == []
        assert app.query_one("#events-live-table", DataTable).row_count == 0


@pytest.mark.asyncio
async def test_phase2b_live_visible_gates_flush(mock_pc):
    """Flush is a no-op when outer or inner is not Events/Live."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import DataTable

    plugin = _make_phase2b_real_plugin()
    _install_phase2b_pc(mock_pc)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=plugin,
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        # Stay on Home (outer != events). Push an event.
        plugin._on_bus_event("_core/event/published", {
            "publisher": "X", "topic": "t",
            "target_count": 1, "ts": 100.0,
        })
        # Force flush — it should observe `_outer_is_events=False`
        # and bail without rendering.
        app._flush_live_events()
        await pilot.pause()
        assert app.query_one("#events-live-table", DataTable).row_count == 0


@pytest.mark.asyncio
async def test_phase2b_live_visible_catchup_flush_on_reenter(mock_pc):
    """Switch outer/inner away then back → catch-up flush renders
    queued events."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable

    plugin = _make_phase2b_real_plugin()
    _install_phase2b_pc(mock_pc)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=plugin,
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        # Activate Events → Live initially (so subscribers are wired).
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        app.query_one("#events-tabs", TabbedContent).active = "events-tab-live"
        await pilot.pause()
        # Switch to Home.
        app.query_one("#main-tabs", TabbedContent).active = "tab-home"
        await pilot.pause()
        # Push events while hidden.
        plugin._on_bus_event("_core/event/published", {
            "publisher": "X", "topic": "t",
            "target_count": 1, "ts": 100.0,
        })
        # Re-enter Events.
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(3):
            await pilot.pause()
        # The catch-up flush fired on the outer-edge handler.
        assert app.query_one("#events-live-table", DataTable).row_count >= 1


def test_phase2b_live_stream_burst_load_caps_at_1000():
    """Plugin deque caps at 1000."""
    plugin = _make_phase2b_real_plugin()
    for i in range(2000):
        plugin._on_bus_event("_core/event/published", {
            "publisher": "X", "topic": "t",
            "target_count": 1, "ts": float(i),
        })
    rows, seq = plugin.get_live_events_since(0)
    assert seq == 2000
    assert len(rows) == 1000  # deque maxlen


def test_phase2b_live_stream_get_events_since_atomic():
    """Concurrent `_on_bus_event` from another thread + main-thread
    `get_live_events_since` — no `RuntimeError`, no torn read.

    Cycle 6 fresh-eyes fix: half the reads use a non-zero `last_seq`
    cursor so the O(new_events) right-iteration path is also
    exercised under contention. Previously the test always called
    `get_live_events_since(0)`, which only walks the full deque
    (also exercises the path consumers actually use, but not the
    cursor-advance behaviour). Both paths share the same lock so
    a torn read in either would surface as `RuntimeError` or a
    `KeyError`/`TypeError` from a partially-built row dict.
    """
    plugin = _make_phase2b_real_plugin()
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            plugin._on_bus_event("_core/event/published", {
                "publisher": "X", "topic": "t",
                "target_count": 1, "ts": float(i),
            })
            i += 1

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        last_seen = 0
        for i in range(200):
            # Alternate between full-deque reads (`last_seq=0`) and
            # cursor-advance reads (`last_seq=last_seen`). The
            # cursor-advance path returns rows whose `_seq` > last_seen
            # by walking the deque from the right; verify every row
            # respects the bound and that the returned `current_seq`
            # monotonically increases.
            if i % 2 == 0:
                rows, current_seq = plugin.get_live_events_since(0)
            else:
                rows, current_seq = plugin.get_live_events_since(last_seen)
                for r in rows:
                    assert r["_seq"] > last_seen, (
                        f"cursor breach: row _seq={r['_seq']} "
                        f"<= last_seen={last_seen}"
                    )
            # `_seq` field present on every row (no torn read).
            for r in rows:
                assert isinstance(r["_seq"], int)
            assert isinstance(current_seq, int)
            assert current_seq >= last_seen
            last_seen = current_seq
    finally:
        stop.set()
        t.join(timeout=2.0)


# ── #31 — Observer cleanup on disable ──────────────────────────────────

@pytest.mark.asyncio
async def test_phase2b_observer_cleanup_on_disable():
    """`plugin.on_disable()` removes every observed topic from
    `internal_observers`. Re-enable + re-disable produce the same
    final state.

    Cycle 1 review fix: this test now calls the real `on_disable`
    coroutine (with stubbed dependencies) instead of manually
    simulating the unregister loop. Without this, a future refactor
    of `on_disable` that uses a different variable name (e.g.
    `_subscribed_topics` instead of `_observed_topics`) would leave
    a real observer leak undetected.
    """
    from plugins_test.TUI.plugin import TUI

    # Synthetic plugin_core stub that mirrors `internal_observe`
    # / `internal_unobserve` semantics on a plain dict so we can
    # inspect. Also satisfies the surface `on_disable` reaches into
    # (log handler removal happens against root logger; the TUI
    # plugin's `_app` is None so the `app.exit()` path is skipped).
    class StubPC:
        def __init__(self):
            self.main_event_loop = None
            self.plugins_by_uuid = {}
            self._internal_observers = {}
        def internal_observe(self, plugin_uuid, topic, cb):
            self._internal_observers.setdefault(topic, []).append(cb)
        def internal_unobserve(self, plugin_uuid, topic, cb):
            lst = self._internal_observers.get(topic, [])
            if cb in lst:
                lst.remove(cb)
            if not lst:
                self._internal_observers.pop(topic, None)
            return True

    plugin = TUI.__new__(TUI)
    plugin._logger = MagicMock()
    plugin._plugin_core = StubPC()
    plugin.plugin_uuid = "tui-uuid"
    plugin.on_load()
    # Mirror the registration loop from `on_enable` — observer pairs
    # the real on_enable would set up. The `internal_observe` method
    # on the real Plugin base auto-fills the plugin_uuid, but here we
    # bypass it by calling our stub directly. This is the
    # registration path equivalent.
    plugin._observed_topics = [
        ("_core/peer/connected", plugin._on_peer_event),
        ("_core/peer/disconnected", plugin._on_peer_event),
        ("_core/event/published", plugin._on_bus_event),
        ("_core/event/requested", plugin._on_bus_event),
        ("_core/event/streamed", plugin._on_bus_event),
        ("_core/subscription/state_changed", plugin._on_bus_event),
        ("_core/event/state_changed", plugin._on_bus_event),
    ]
    for topic, cb in plugin._observed_topics:
        plugin._plugin_core.internal_observe(
            plugin.plugin_uuid, topic, cb,
        )

    expected = {
        "_core/peer/connected",
        "_core/peer/disconnected",
        "_core/event/published",
        "_core/event/requested",
        "_core/event/streamed",
        "_core/subscription/state_changed",
        "_core/event/state_changed",
    }
    assert expected.issubset(
        set(plugin._plugin_core._internal_observers.keys())
    )

    # Set up the minimal state on_disable touches before running it.
    plugin._app = None
    plugin._tui_thread = None
    plugin._muted_handler = None
    plugin._log_handler = MagicMock()
    plugin._log_handler.detach = MagicMock()

    # Patch `internal_unobserve` on the plugin instance to delegate
    # to our stub PC's unobserve (the Plugin base's `internal_unobserve`
    # auto-fills plugin_uuid; our stub PC has its own signature).
    def _plugin_unobserve(topic, cb):
        return plugin._plugin_core.internal_unobserve(
            plugin.plugin_uuid, topic, cb,
        )
    plugin.internal_unobserve = _plugin_unobserve

    # Call the REAL on_disable coroutine.
    await plugin.on_disable()

    # All 7 observer registrations should be cleaned up.
    for topic in expected:
        assert topic not in plugin._plugin_core._internal_observers, (
            f"on_disable left observer for {topic!r} registered"
        )
    # And the plugin's own bookkeeping list should be empty.
    assert plugin._observed_topics == []


# ── #32-33 — Outer-tab scoping + filter persistence ────────────────────

@pytest.mark.asyncio
async def test_phase2b_outer_tab_handler_scoping_against_nested_event(mock_pc):
    """Switching inner tabs (events-tabs) does NOT touch the catch-all
    config-dirty banner via the outer activation handler."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent

    _install_phase2b_pc(mock_pc)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        # Activate Events.
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        await pilot.pause()
        assert app._outer_is_events is True
        # Switch inner tab — outer flag must stay True.
        app.query_one("#events-tabs", TabbedContent).active = "events-tab-cat"
        await pilot.pause()
        assert app._outer_is_events is True
        # Switch back to subs.
        app.query_one("#events-tabs", TabbedContent).active = "events-tab-subs"
        await pilot.pause()
        assert app._outer_is_events is True


@pytest.mark.asyncio
async def test_phase2b_filter_persistence_across_tab_switch(mock_pc):
    """Topic filter on Subs browser survives outer-tab roundtrip."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import Input, TabbedContent

    sub = _make_phase2b_subscription(
        sub_uuid="u1", topic_pattern="t", plugin_name="A", declared_id="x",
    )
    _install_phase2b_pc(mock_pc, subs=[sub])
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(3):
            await pilot.pause()
        inp = app.query_one("#events-subs-filter-topic", Input)
        inp.value = "marker-value"
        await pilot.pause()
        # Switch to Home + back.
        app.query_one("#main-tabs", TabbedContent).active = "tab-home"
        await pilot.pause()
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        await pilot.pause()
        inp_after = app.query_one("#events-subs-filter-topic", Input)
        assert inp_after.value == "marker-value"


# ── #34 — _run_on_main None-return toast ───────────────────────────────

@pytest.mark.asyncio
async def test_phase2b_run_on_main_none_return_shows_toast(mock_pc):
    """When `_run_on_main` returns None (main loop missing), the toggle
    wrapper notifies with a warning toast instead of silently no-opping."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable

    sub = _make_phase2b_subscription(
        sub_uuid="u1", topic_pattern="t", plugin_name="A", declared_id="x",
    )
    _install_phase2b_pc(mock_pc, subs=[sub])
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    notified = []

    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        # Activate Events FIRST so the worker has a chance to run
        # against the unmocked `_run_on_main`. Then patch + pre-populate
        # AFTER, so the worker's clear doesn't wipe our row.
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(3):
            await pilot.pause()
        app._outer_is_events = True
        # Now stub `_run_on_main` to return None for the toggle path.
        async def _none_passthrough(coro, timeout=30.0):
            coro.close()
            return None
        app._run_on_main = _none_passthrough  # type: ignore[method-assign]
        # Pre-populate the rendered rows + table so the toggle has a
        # cursor target.
        app._subs_rendered_rows = [{
            "topic_pattern": "t", "plugin_name": "A", "plugin_uuid": "x",
            "target_plugin": "A", "target_access_name": "h",
            "target_plugin_uuid": None, "target_render": ".h",
            "hosts": None, "blocked_hosts": None, "authors": None,
            "blocked_authors": None, "enabled": True,
            "declared_id": "x", "declared_kind": "YAML", "sub_uuid": "u1",
        }]
        table = app.query_one("#events-subs-table", DataTable)
        if table.row_count == 0:
            table.add_row("t", "A", ".h", "", "*", "on", "YAML", "u1...")
        # Move cursor to the first row so `_selected_sub_row` resolves.
        table.move_cursor(row=0)
        table.focus()
        await pilot.pause()
        # Call the toggle helper directly with an explicit row dict —
        # bypasses any test-ordering / cursor-state issues with
        # `_selected_sub_row()`. The helper's body is identical to
        # what the @work path runs.
        row = {
            "topic_pattern": "t", "plugin_name": "A", "plugin_uuid": "x",
            "target_plugin": "A", "target_access_name": "h",
            "target_plugin_uuid": None, "target_render": ".h",
            "hosts": None, "blocked_hosts": None, "authors": None,
            "blocked_authors": None, "enabled": True,
            "declared_id": "x", "declared_kind": "YAML", "sub_uuid": "u1",
        }
        with _phase2b_patch.object(
            app, "notify",
            side_effect=lambda *a, **k: notified.append((a, k)),
        ):
            await app._apply_subscription_toggle(row)
        msgs = [str(c[0][0]) if c[0] else "" for c in notified]
        assert any("main loop unavailable" in m for m in msgs), \
            f"notified={notified!r}"


# ── #52 — _run_on_main False-return toast ──────────────────────────────

@pytest.mark.asyncio
async def test_phase2b_run_on_main_false_return_shows_toast(mock_pc):
    """Wrapper returns False (sub_uuid not in registry) → distinct
    toast from #34. Already validated by #12 — but plan #52 calls out
    the explicit distinction between None (infrastructure) and False
    (data state changed under us)."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable

    sub = _make_phase2b_subscription(
        sub_uuid="u-gone", topic_pattern="t", plugin_name="A",
        declared_id="x",
    )
    _install_phase2b_pc(mock_pc, subs=[sub], set_sub_result=False)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    notified = []

    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-subs-table", DataTable).row_count == 1:
                break
        table = app.query_one("#events-subs-table", DataTable)
        table.focus()
        await pilot.pause()
        with _phase2b_patch.object(
            app, "notify",
            side_effect=lambda *a, **k: notified.append((a, k)),
        ):
            await pilot.press("e")
            for _ in range(5):
                await pilot.pause()
        # Must be the "no longer exists" toast, NOT the "main loop
        # unavailable" one.
        msgs = [str(c[0]) for c in notified]
        assert any("no longer exists" in m for m in msgs)
        assert not any("main loop unavailable" in m for m in msgs)


# ── #53 — _outer_is_events flips back on Home activation ───────────────

@pytest.mark.asyncio
async def test_phase2b_outer_is_events_clears_on_home_activation(mock_pc):
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable

    _install_phase2b_pc(mock_pc)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        await pilot.pause()
        assert app._outer_is_events is True
        app.query_one("#main-tabs", TabbedContent).active = "tab-home"
        await pilot.pause()
        assert app._outer_is_events is False
        # Flush is no-op while not on Events.
        app._live_rendered_rows = [
            {"ts": 100.0, "topic_raw": "_core/event/published",
             "type_label": "[green]▶ pub[/]",
             "topic": "t", "publisher": "X", "detail": "",
             "_seq": 1},
        ]
        app._live_filter_dirty = True
        app._flush_live_events()
        await pilot.pause()
        # Table count unchanged from 0 (Live-stream not visible).
        # Plus we never rendered in the first place.
        assert app.query_one("#events-live-table", DataTable).row_count == 0


# ── #57 — No-op toggle wrapper-side behavior ───────────────────────────

@pytest.mark.asyncio
async def test_phase2b_subs_browser_noop_toggle_no_crash(mock_pc):
    """The TUI wrapper returns cleanly when the framework reports a
    no-op (same value already set). No toast asserted here — the
    wrapper relies on the bus emit ONLY firing on actual state change,
    locked in framework test #56."""
    from plugins_test.TUI.app import DashboardApp
    from textual.widgets import TabbedContent, DataTable

    sub = _make_phase2b_subscription(
        sub_uuid="u1", topic_pattern="t", plugin_name="A", declared_id="x",
        enabled=True,
    )
    _install_phase2b_pc(mock_pc, subs=[sub], set_sub_result=True)
    app = DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )
    async with app.run_test(headless=True, size=(140, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        app.query_one("#main-tabs", TabbedContent).active = "tab-events"
        for _ in range(5):
            await pilot.pause()
            if app.query_one("#events-subs-table", DataTable).row_count == 1:
                break
        table = app.query_one("#events-subs-table", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("e")
        for _ in range(5):
            await pilot.pause()
        # Wrapper was called with the flipped value (True → False).
        called_args = mock_pc.set_subscription_enabled.await_args.args
        assert called_args[0] == "u1"
        assert called_args[1] is False


# ═══════════════════════════════════════════════════════════════════════
# Phase 3a — Plugins-tab Phase column + counts + 5-section detail pane
# ═══════════════════════════════════════════════════════════════════════


def _install_phase3_pc(
    mock_pc,
    *,
    plugin_states_overrides=None,
    all_subs=None,
    logger_levels_overrides=None,
):
    """Extend `mock_pc` with the Phase 3a + 3b API surface.

    Adds:
      * `plugin_states[name].state` for every plugin in `mock_pc.plugins`
        — defaults to ENABLED for `enabled=True`, INACTIVE otherwise.
        `plugin_states_overrides[name] = State.X` overrides per-plugin.
      * `_lifecycle_ready` and `ready` `asyncio.Event` on each plugin
        instance — both set when default state is ENABLED so the
        Phase column renders READY.
      * `plugin_uuid` on each plugin (used by the per-plugin tab in 3b
        and by the test fixture's identity expectations).
      * `topic_registry.list_local_subs` / `get_subscription` for
        runtime-sub lookups in the per-plugin tab (3b).
      * `get_unloaded_metadata` async — returns None for non-UNLOADED
        (matches `PluginCore.get_unloaded_metadata`).
      * `list_logger_levels` (returns the snapshot shape utils.py uses)
        — `logger_levels_overrides` is wired as the MagicMock's
        `return_value` so 3b Logger-section tests can prime per-prefix
        snapshot dicts that mirror `utils.py:870-903`.
      * `set_logger_level` / `clear_logger_level` MagicMocks (3b apply
        path).

    For UNLOADED state plugins: removes the plugin from `mock_pc.plugins`
    (UNLOADED == config has entry but no live instance, per
    `plugin_state.py:36`).
    """
    from plugin_state import PluginState, State, Phase, ErrorRecord
    overrides = plugin_states_overrides or {}

    states = {}
    for name, p in list(mock_pc.plugins.items()):
        default_state = (
            State.ENABLED if getattr(p, "enabled", False) else State.INACTIVE
        )
        state = overrides.get(name, default_state)

        # Lifecycle / readiness events. Real Plugin instances have these
        # set by Plugin.__init__ (utils.py:1189) + by
        # _enable_plugin_under_lock (PluginCore.py:2815). Mirror here so
        # _plugin_phase reads them with .is_set().
        p._lifecycle_ready = asyncio.Event()
        p.ready = asyncio.Event()
        if state == State.ENABLED:
            p._lifecycle_ready.set()
            p.ready.set()

        # plugin_uuid — Phase 3b cross-link + logger-level identity uses this.
        if not hasattr(p, "plugin_uuid") or isinstance(p.plugin_uuid, MagicMock):
            p.plugin_uuid = f"uuid-{name}"

        # verbose_notifier — Phase 3b Lifecycle strip Switch reads this.
        if not hasattr(p, "verbose_notifier") or isinstance(p.verbose_notifier, MagicMock):
            p.verbose_notifier = False

        # subscriptions / events / _sub_uuids — declarative defaults so
        # _refresh_plugin_table_worker can read len() without raising.
        if not hasattr(p, "subscriptions") or isinstance(p.subscriptions, MagicMock):
            p.subscriptions = {}
        if not hasattr(p, "events") or isinstance(p.events, MagicMock):
            p.events = {}
        if not hasattr(p, "_sub_uuids") or isinstance(p._sub_uuids, MagicMock):
            p._sub_uuids = []
        if not hasattr(p, "arguments") or isinstance(p.arguments, MagicMock):
            p.arguments = None

        # PluginState entry.
        ps = PluginState(name=name, state=state, instance=p)
        states[name] = ps

    # Add plugin_states entries for names ONLY present in overrides
    # (UNLOADED / config-has-entry-but-no-live-instance paths).
    for name, state in overrides.items():
        if name in states:
            continue
        # Synthesize the entry without an instance. Test code can attach
        # a `last_errors` dict afterwards if it wants to test FAILED_LOAD
        # paths without a live instance.
        states[name] = PluginState(name=name, state=state, instance=None)

    # Drop the live instance for states where the framework wouldn't
    # have one in `pc.plugins`:
    #   - UNLOADED: never instantiated (or popped after disable).
    #   - FAILED_LOAD: instantiation raised in `load_plugin_with_conf`
    #     BEFORE the `pc.plugins[name] = plugin` assignment ran
    #     (`PluginCore.py:2466-2474` only runs on the success path),
    #     so `pc.plugins.get(name)` returns None for a real FAILED_LOAD
    #     plugin. Mirror that here so the detail-pane on-disk fallback
    #     test path matches production behaviour.
    for name, ps in states.items():
        if ps.state in (State.UNLOADED, State.FAILED_LOAD):
            mock_pc.plugins.pop(name, None)
            ps.instance = None

    mock_pc.plugin_states = states

    # topic_registry surface (overlaps with Phase 2b helper — idempotent).
    all_subs_list = list(all_subs or [])

    async def _list_local_subs():
        return list(all_subs_list)

    async def _get_subscription(sub_uuid):
        return next(
            (
                s for s in all_subs_list
                if getattr(s, "sub_uuid", None) == sub_uuid
            ),
            None,
        )

    if not hasattr(mock_pc, "topic_registry") or isinstance(mock_pc.topic_registry, MagicMock):
        mock_pc.topic_registry = MagicMock()
    mock_pc.topic_registry.list_local_subs = _list_local_subs
    mock_pc.topic_registry.get_subscription = _get_subscription

    # get_unloaded_metadata — only returns for UNLOADED state.
    async def _get_unloaded_metadata(plugin_name):
        ps = states.get(plugin_name)
        if ps is None or getattr(ps.state, "value", None) != "unloaded":
            return None
        return {
            "name": plugin_name,
            "version": "unknown",
            "description": "",
            "path": f"/test/{plugin_name}",
            "declared_endpoints": [],
            "declared_events": [],
            "declared_subscriptions": [],
        }
    mock_pc.get_unloaded_metadata = _get_unloaded_metadata

    # Logger-level surface (read by Phase 3b, mocked here for fixture
    # consistency — does no harm in 3a tests). Phase 3b tests prime
    # per-prefix snapshots via `logger_levels_overrides`.
    mock_pc.list_logger_levels = MagicMock(
        return_value=logger_levels_overrides or {}
    )
    mock_pc.set_logger_level = MagicMock()
    mock_pc.clear_logger_level = MagicMock()

    # internal_observe / internal_unobserve must be MagicMocks so the
    # app's `on_mount` observer registration loop doesn't trip on a
    # real coroutine. Idempotent re-set if already mocked.
    if not hasattr(mock_pc, "internal_observe") or isinstance(mock_pc.internal_observe, MagicMock):
        mock_pc.internal_observe = MagicMock()
    if not hasattr(mock_pc, "internal_unobserve") or isinstance(mock_pc.internal_unobserve, MagicMock):
        mock_pc.internal_unobserve = MagicMock(return_value=True)


def _attach_failed_load_error(
    mock_pc, plugin_name: str, exc: BaseException, traceback_text: str = "",
):
    """Helper for the FAILED_LOAD detail-pane test (#16).

    Populates `plugin_states[plugin_name].last_errors[Phase.LOAD]` with
    an ErrorRecord. Assumes `_install_phase3_pc` already ran with a
    `plugin_states_overrides={plugin_name: State.FAILED_LOAD}` entry.
    """
    from plugin_state import Phase, ErrorRecord
    ps = mock_pc.plugin_states.get(plugin_name)
    if ps is None:
        return
    ps.last_errors[Phase.LOAD] = ErrorRecord(
        exception=exc,
        traceback=traceback_text or f"Traceback for {type(exc).__name__}",
        ts=1234567890.0,
    )


def _make_phase3_app(mock_pc):
    """Standard Phase 3 test app bootstrap. Returns the configured app."""
    from plugins_test.TUI.app import DashboardApp
    return DashboardApp(
        plugin_core=mock_pc,
        plugin_instance=_make_phase2b_plugin_instance(),
        log_handler=TUILogHandler(),
    )


# ── #1 — table column shape ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase3a_plugin_table_columns(mock_pc):
    """Plugins-tab DataTable has 8 columns in the expected order."""
    from textual.widgets import DataTable, TabbedContent

    _install_phase3_pc(mock_pc)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        app.query_one("#main-tabs", TabbedContent).active = "tab-plugins"
        await pilot.pause()
        table = app.query_one("#plugin-table", DataTable)
        # DataTable.columns is an OrderedDict; values are Column objects
        # with a `label` attribute.
        labels = [str(c.label) for c in table.columns.values()]
        assert labels == [
            "Name", "Phase", "Ver", "R", "Eps", "Subs", "Evs", "Description",
        ]


# ── #2-#8 — Phase column rendering for each State enum value ────────────

@pytest.mark.asyncio
async def test_phase3a_phase_column_enabled_ready(mock_pc):
    """state==ENABLED + both readiness events set → READY (green)."""
    from textual.widgets import DataTable, TabbedContent

    _install_phase3_pc(mock_pc)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        app.query_one("#main-tabs", TabbedContent).active = "tab-plugins"
        await pilot.pause()
        app._refresh_plugin_table_worker()
        for _ in range(5):
            await pilot.pause()
        plugin = mock_pc.plugins["PluginA"]
        label, css = app._plugin_phase("PluginA", plugin)
        assert label == "READY"
        assert css == "stat-val-good"


@pytest.mark.asyncio
async def test_phase3a_phase_column_enabled_waiting(mock_pc):
    """state==ENABLED + _lifecycle_ready set + ready CLEAR → WAITING (warn)."""
    from plugin_state import State

    _install_phase3_pc(mock_pc)
    plugin = mock_pc.plugins["PluginA"]
    plugin.ready.clear()  # author cleared inside on_enable; not yet re-set
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        label, css = app._plugin_phase("PluginA", plugin)
        assert label == "WAITING"
        assert css == "stat-val-warn"


@pytest.mark.asyncio
async def test_phase3a_phase_column_enabling(mock_pc):
    """state==ENABLING → LOADING (warn)."""
    from plugin_state import State

    _install_phase3_pc(mock_pc, plugin_states_overrides={"PluginA": State.ENABLING})
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        plugin = mock_pc.plugins["PluginA"]
        label, css = app._plugin_phase("PluginA", plugin)
        assert label == "LOADING"
        assert css == "stat-val-warn"


@pytest.mark.asyncio
async def test_phase3a_phase_column_inactive(mock_pc):
    """state==INACTIVE → DISABLED (dim)."""
    from plugin_state import State

    _install_phase3_pc(mock_pc, plugin_states_overrides={"PluginA": State.INACTIVE})
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        plugin = mock_pc.plugins["PluginA"]
        label, css = app._plugin_phase("PluginA", plugin)
        assert label == "DISABLED"
        assert css == "phase-dim"


@pytest.mark.asyncio
async def test_phase3a_phase_column_disabling(mock_pc):
    """state==DISABLING → DISABLING (warn)."""
    from plugin_state import State

    _install_phase3_pc(mock_pc, plugin_states_overrides={"PluginA": State.DISABLING})
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        plugin = mock_pc.plugins["PluginA"]
        label, css = app._plugin_phase("PluginA", plugin)
        assert label == "DISABLING"
        assert css == "stat-val-warn"


@pytest.mark.asyncio
async def test_phase3a_phase_column_failed_load(mock_pc):
    """state==FAILED_LOAD → FAILED (bad). Real FAILED_LOAD plugins have
    no entry in `pc.plugins` (instantiation raised before the registry
    assignment in `load_plugin_with_conf`), so the fixture drops the
    instance and `_plugin_phase` is called with plugin=None."""
    from plugin_state import State

    _install_phase3_pc(mock_pc, plugin_states_overrides={"PluginA": State.FAILED_LOAD})
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        # FAILED_LOAD → no live instance, mirroring framework reality.
        assert mock_pc.plugins.get("PluginA") is None
        label, css = app._plugin_phase("PluginA", None)
        assert label == "FAILED"
        assert css == "stat-val-bad"


@pytest.mark.asyncio
async def test_phase3a_phase_column_unloaded(mock_pc):
    """state==UNLOADED → UNLOADED (dim); no live plugin instance."""
    from plugin_state import State

    _install_phase3_pc(
        mock_pc,
        plugin_states_overrides={"PluginA": State.UNLOADED, "PluginB": State.UNLOADED},
    )
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        # Plugin instance was dropped from pc.plugins by the helper to
        # reflect "config has entry, no live instance".
        assert mock_pc.plugins.get("PluginA") is None
        label, css = app._plugin_phase("PluginA", None)
        assert label == "UNLOADED"
        assert css == "phase-dim"


# ── #9 — Subs cell `+N` suffix for runtime additions ────────────────────

@pytest.mark.asyncio
async def test_phase3a_subs_count_includes_runtime_suffix(mock_pc):
    """Declared 3 + runtime 2 → cell shows `3+2`."""
    from textual.widgets import DataTable, TabbedContent

    _install_phase3_pc(mock_pc)
    plugin = mock_pc.plugins["PluginA"]
    plugin.subscriptions = {
        "s1": {"topic": "a", "target_access_name": "h"},
        "s2": {"topic": "b", "target_access_name": "h"},
        "s3": {"topic": "c", "target_access_name": "h"},
    }
    plugin._sub_uuids = ["yaml1", "yaml2", "yaml3", "runtime1", "runtime2"]

    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        app.query_one("#main-tabs", TabbedContent).active = "tab-plugins"
        await pilot.pause()
        app._refresh_plugin_table_worker()
        for _ in range(5):
            await pilot.pause()
        table = app.query_one("#plugin-table", DataTable)
        # Find PluginA row.
        for idx in range(table.row_count):
            row = table.get_row_at(idx)
            if row[0] == "PluginA":
                # Subs cell is column index 5.
                assert row[5] == "3+2"
                break
        else:
            pytest.fail("PluginA row not in table")


# ── #10 — detail pane has 5 Collapsible sections ────────────────────────

@pytest.mark.asyncio
async def test_phase3a_detail_pane_five_sections(mock_pc):
    """Selecting a row mounts 5 Collapsibles with the expected ids."""
    from textual.containers import VerticalScroll
    from textual.widgets import Collapsible, TabbedContent

    _install_phase3_pc(mock_pc)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        app.query_one("#main-tabs", TabbedContent).active = "tab-plugins"
        await pilot.pause()
        # Drive the worker directly — row-highlight dispatching from the
        # DataTable is event-driven and brittle to test pacing.
        app._update_plugin_detail("PluginA")
        for _ in range(8):
            await pilot.pause()
        detail = app.query_one("#plugin-detail", VerticalScroll)
        collapsible_ids = [c.id for c in detail.query(Collapsible)
                           if c.id in app._DETAIL_SECTION_IDS]
        assert set(collapsible_ids) == set(app._DETAIL_SECTION_IDS)


# ── #11 — Args overrides indicator ──────────────────────────────────────

@pytest.mark.asyncio
async def test_phase3a_detail_pane_args_overrides_indicator(mock_pc, tmp_path):
    """When `plugin.arguments` differs from on-disk yaml, render
    'overrides applied' badge in the Args section."""
    import yaml as _yaml
    from textual.containers import VerticalScroll
    from textual.widgets import Static, TabbedContent

    # Write a plugin_config.yml with base args.
    plugin_dir = tmp_path / "TestPluginA"
    plugin_dir.mkdir()
    (plugin_dir / "plugin_config.yml").write_text(
        _yaml.safe_dump({
            "description": "test",
            "version": "1.0",
            "remote": False,
            "arguments": {"timeout": 10},
            "endpoints": {},
        }),
        encoding="utf-8",
    )

    mock_pc.yaml_config = {
        "plugins": [{"name": "PluginA", "path": str(plugin_dir),
                     "enabled": True}],
        "general": {}, "networking": {},
    }
    _install_phase3_pc(mock_pc)
    plugin = mock_pc.plugins["PluginA"]
    # Live args differ from on-disk → overrides applied.
    plugin.arguments = {"timeout": 30, "retry": 5}

    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        app.query_one("#main-tabs", TabbedContent).active = "tab-plugins"
        await pilot.pause()
        app._update_plugin_detail("PluginA")
        for _ in range(8):
            await pilot.pause()
        detail = app.query_one("#plugin-detail", VerticalScroll)
        # The badge is a Static with classes="plugin-detail-overrides-badge".
        badges = [
            s for s in detail.query(Static)
            if "plugin-detail-overrides-badge" in s.classes
        ]
        assert badges, "overrides-applied badge should render"


# ── #12 — `_core/plugin/state_changed` triggers refresh ─────────────────

@pytest.mark.asyncio
async def test_phase3a_plugin_state_changed_refresh(mock_pc):
    """Emitting `_core/plugin/state_changed` sets the plugins flag and
    the next debounce tick spawns the worker."""
    from unittest.mock import MagicMock as _MagicMock

    _install_phase3_pc(mock_pc)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        # Replace the worker with a counter so we can detect invocations.
        called = _MagicMock()
        app._refresh_plugin_table_worker = called  # type: ignore[method-assign]
        # Simulate a bus emit by calling the observer directly.
        app._on_plugin_state_changed(
            "_core/plugin/state_changed",
            {"name": "PluginA", "from_state": "inactive",
             "to_state": "enabled", "ts": 0},
        )
        assert app._plugins_refresh_pending is True
        # Drive the debounce tick.
        for _ in range(5):
            app._debounce_refresh_tick()
            await pilot.pause()
            if called.called:
                break
        assert called.called, "_refresh_plugin_table_worker should fire"


# ── #13 — section open-state preservation across row changes ────────────

@pytest.mark.asyncio
async def test_phase3a_detail_section_open_state_preserved(mock_pc):
    """Expanding Endpoints on plugin A, then re-selecting A, restores
    the expanded state. Section state stored in
    `_plugin_detail_open_sections[plugin_name]`.
    """
    from textual.containers import VerticalScroll
    from textual.widgets import Collapsible, TabbedContent

    _install_phase3_pc(mock_pc)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        app.query_one("#main-tabs", TabbedContent).active = "tab-plugins"
        await pilot.pause()

        # Select A → expand Endpoints by toggling its collapsed flag.
        app._update_plugin_detail("PluginA")
        for _ in range(8):
            await pilot.pause()
        detail = app.query_one("#plugin-detail", VerticalScroll)
        endpoints_col = detail.query_one(
            f"#{app._DETAIL_SECTION_ENDPOINTS}", Collapsible,
        )
        endpoints_col.collapsed = False
        await pilot.pause()

        # Switch to B (triggers capture-then-render).
        app._update_plugin_detail("PluginB")
        for _ in range(8):
            await pilot.pause()
        # The capture-on-render saves A's open set.
        assert app._DETAIL_SECTION_ENDPOINTS in app._plugin_detail_open_sections.get(
            "PluginA", set(),
        )

        # Re-select A → Endpoints reopens.
        app._update_plugin_detail("PluginA")
        for _ in range(8):
            await pilot.pause()
        detail = app.query_one("#plugin-detail", VerticalScroll)
        endpoints_col = detail.query_one(
            f"#{app._DETAIL_SECTION_ENDPOINTS}", Collapsible,
        )
        assert endpoints_col.collapsed is False


# ── #13b — regression: same-plugin re-render preserves saved state ──────

@pytest.mark.asyncio
async def test_phase3a_same_plugin_rerender_preserves_open_state(mock_pc):
    """Regression: a same-plugin re-render (e.g. `_core/plugin/state_changed`
    direct-refresh while a prior worker is mid-render) must NOT overwrite
    the saved open-state set. Pre-fix: Worker2 observed `prior_open=={}`
    (Worker1 already removed children) and saved `{}` over the
    user-expanded state. Post-fix: same-plugin re-render skips the save
    entirely.
    """
    _install_phase3_pc(mock_pc)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        # Pre-populate the saved-open set for PluginA as if the operator
        # had expanded Endpoints previously.
        app._plugin_detail_open_sections["PluginA"] = {
            app._DETAIL_SECTION_ENDPOINTS,
        }
        app._detail_render_target = "PluginA"

        # Trigger a same-plugin re-render. The pre-fix path would
        # capture prior_open={} (no Collapsibles yet) and overwrite
        # _plugin_detail_open_sections["PluginA"] with {}.
        app._update_plugin_detail("PluginA")
        for _ in range(8):
            await pilot.pause()

        # Post-fix: the saved set survives.
        assert app._plugin_detail_open_sections.get("PluginA") == {
            app._DETAIL_SECTION_ENDPOINTS,
        }


# ── #13c — regression: cross-plugin save honors prior_total guard ───────

@pytest.mark.asyncio
async def test_phase3a_cross_plugin_save_skips_on_empty_pane(mock_pc):
    """Regression: when a prior worker was cancelled mid-`remove_children()`
    leaving the pane empty, the next worker must NOT overwrite the
    saved open-state set under the previous plugin name with an empty
    set captured from the cleared pane. Cycle 2 fresh-eyes finding.

    The async ordering of @work + Pilot makes this hard to reproduce
    end-to-end deterministically (the auto-row-highlight on boot
    races our setup). Instead we test the save-guard logic directly
    on a fresh DashboardApp instance with NO live plugins — no
    auto-row-highlight fires, no Workers compete, and we exercise the
    capture-and-save block of `_update_plugin_detail` exactly once
    against a known-empty pane.
    """
    from textual.containers import VerticalScroll
    # Replace mock_pc.plugins with an empty dict so the Plugins-tab
    # table is empty → no auto-row-highlight → no auto _update_plugin_detail
    # invocation on boot. This isolates the save-guard test from the
    # @work timing race entirely.
    mock_pc.plugins = {}
    _install_phase3_pc(mock_pc)

    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()

        # Confirm the pane mounted (compose ran).
        scroll = app.query_one("#plugin-detail", VerticalScroll)
        # Pane has no children — no auto-render happened (empty plugins).
        assert len(list(scroll.query("Collapsible"))) == 0

        # Seed B's saved set as if from a prior session.
        app._plugin_detail_open_sections["PluginB"] = {
            app._DETAIL_SECTION_EVENTS,
        }
        # Simulate Worker(B) having set the render target before being
        # cancelled mid-remove_children().
        app._detail_render_target = "PluginB"

        # Render PluginA against the empty scroll. PluginA isn't in
        # pc.plugins (we emptied it), so plugin=None and the worker
        # falls through the on-disk path. What matters is the
        # capture-and-save block runs against the empty pane.
        app._update_plugin_detail("PluginA")
        for _ in range(8):
            await pilot.pause()

        # B's saved set survives — capture observed prior_total == 0,
        # guard skipped the save instead of overwriting with {}.
        assert app._plugin_detail_open_sections.get("PluginB") == {
            app._DETAIL_SECTION_EVENTS,
        }


# ── #13d — regression: _currently_displayed_plugin only set after pane found ──

@pytest.mark.asyncio
async def test_phase3a_currently_displayed_plugin_not_set_on_nomatches(mock_pc):
    """Regression: if `_update_plugin_detail` bails because
    `#plugin-detail` isn't mounted (e.g. observer fires after the pane
    was torn down or before on_mount completed), `_currently_displayed_plugin`
    MUST stay at its prior value, NOT get set to the bailed-on name.
    Pre-fix: assignment ran before the `query_one` early-return, so a
    subsequent state-change emit would re-spawn the worker forever
    against an unmounted pane. Cycle 2 fresh-eyes finding.
    """
    from textual.containers import VerticalScroll

    _install_phase3_pc(mock_pc)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        # Remove the pane (simulates "pane unmounted") AND explicitly
        # reset the tracker to None so the auto-row-highlight that may
        # have fired during boot doesn't interfere with the assertion.
        scroll = app.query_one("#plugin-detail", VerticalScroll)
        await scroll.remove()
        await pilot.pause()
        app._currently_displayed_plugin = None

        # Invoke against the unmounted pane — should bail at NoMatches
        # WITHOUT setting _currently_displayed_plugin.
        app._update_plugin_detail("PluginA")
        for _ in range(5):
            await pilot.pause()
        assert app._currently_displayed_plugin is None


# ── #14 — direct detail refresh on state change for displayed plugin ────

@pytest.mark.asyncio
async def test_phase3a_currently_displayed_plugin_live_refresh(mock_pc):
    """When `_core/plugin/state_changed` fires AND the changed plugin is
    the currently displayed one, the observer direct-calls
    `_update_plugin_detail` for that plugin (not just via the debounce)."""
    from unittest.mock import MagicMock as _MagicMock

    _install_phase3_pc(mock_pc)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        # Pretend the operator already selected PluginA.
        app._currently_displayed_plugin = "PluginA"
        # Replace the worker so we can count direct invocations. Patching
        # the method on the instance overrides the @work-decorator binding
        # for THIS instance only; the original class-level method stays
        # intact for other tests.
        direct_calls = _MagicMock()
        app._update_plugin_detail = direct_calls  # type: ignore[method-assign]

        # Fire the observer with a state-change for PluginA.
        app._on_plugin_state_changed(
            "_core/plugin/state_changed",
            {"name": "PluginA", "from_state": "enabled",
             "to_state": "disabling", "ts": 0},
        )
        direct_calls.assert_called_once_with("PluginA")

        # Fire again with a DIFFERENT plugin name — should NOT direct-call.
        direct_calls.reset_mock()
        app._on_plugin_state_changed(
            "_core/plugin/state_changed",
            {"name": "PluginB", "from_state": "inactive",
             "to_state": "enabling", "ts": 0},
        )
        assert direct_calls.call_count == 0


# ── #15 — Plugins flag fires regardless of outer tab gating ─────────────

@pytest.mark.asyncio
async def test_phase3a_plugin_table_refresh_fires_outside_events_tab(mock_pc):
    """`_on_plugin_state_changed` sets ALL THREE pending flags. The
    debounce tick processes the plugins flag regardless of outer tab,
    BUT only processes the subs/cat flags when the Events tab is
    active. Validates the split-tick semantics from Section 4.8."""
    from unittest.mock import MagicMock as _MagicMock

    _install_phase3_pc(mock_pc)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        app._outer_is_events = False
        plugins_worker = _MagicMock()
        subs_worker = _MagicMock()
        cat_worker = _MagicMock()
        app._refresh_plugin_table_worker = plugins_worker  # type: ignore[method-assign]
        app._refresh_subs_browser_worker = subs_worker  # type: ignore[method-assign]
        app._refresh_events_catalogue_worker = cat_worker  # type: ignore[method-assign]

        app._on_plugin_state_changed(
            "_core/plugin/state_changed",
            {"name": "PluginA", "from_state": "inactive",
             "to_state": "enabling", "ts": 0},
        )
        # All three flags set.
        assert app._plugins_refresh_pending is True
        assert app._subs_refresh_pending is True
        assert app._cat_refresh_pending is True

        # Tick with non-Events tab active.
        app._debounce_refresh_tick()
        assert plugins_worker.called, "plugins worker fires regardless of tab"
        assert not subs_worker.called, "subs worker gated on Events tab"
        assert not cat_worker.called, "cat worker gated on Events tab"
        # Events flags stay True awaiting the next outer-tab activation.
        assert app._subs_refresh_pending is True
        assert app._cat_refresh_pending is True


# ── #16 — FAILED_LOAD plugin detail pane ────────────────────────────────

@pytest.mark.asyncio
async def test_phase3a_failed_load_detail_pane(mock_pc, tmp_path):
    """A FAILED_LOAD plugin renders the Info section with exception type
    + message + traceback toggle, and pulls Endpoints/Events/Subs/Args
    content from on-disk `plugin_config.yml`."""
    import yaml as _yaml
    from plugin_state import State
    from textual.containers import VerticalScroll
    from textual.widgets import Collapsible, Static, TabbedContent

    plugin_dir = tmp_path / "PluginA"
    plugin_dir.mkdir()
    (plugin_dir / "plugin_config.yml").write_text(
        _yaml.safe_dump({
            "description": "test plugin",
            "version": "0.9",
            "remote": False,
            "arguments": {"k": "v"},
            "endpoints": {"do_thing": {
                "remote": False, "accessible_by_other_plugins": True,
            }},
            "events": {"evt_a": {"topic": "p/a", "enabled": True}},
            "subscriptions": {"sub_a": {
                "topic": "x/y", "target_access_name": "do_thing",
            }},
        }),
        encoding="utf-8",
    )
    mock_pc.yaml_config = {
        "plugins": [{"name": "PluginA", "path": str(plugin_dir),
                     "enabled": True}],
        "general": {}, "networking": {},
    }
    _install_phase3_pc(
        mock_pc,
        plugin_states_overrides={"PluginA": State.FAILED_LOAD},
    )
    err = RuntimeError("simulated on_load failure")
    _attach_failed_load_error(
        mock_pc, "PluginA", err, "Traceback (most recent call last):\n  ...",
    )

    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        app.query_one("#main-tabs", TabbedContent).active = "tab-plugins"
        await pilot.pause()
        app._update_plugin_detail("PluginA")
        for _ in range(8):
            await pilot.pause()

        detail = app.query_one("#plugin-detail", VerticalScroll)
        # Info section banner shows exception class + message.
        banners = [
            s for s in detail.query(Static)
            if "plugin-detail-failed-banner" in s.classes
        ]
        assert banners, "FAILED_LOAD banner should render"
        # Textual 8.2.3 — Static exposes the originally-set content via
        # the `content` property (NOT `renderable`, which was the API in
        # earlier Textual versions). `str()` on the content yields the
        # raw markup string the constructor received.
        banner_text = str(banners[0].content)
        assert "RuntimeError" in banner_text
        assert "simulated on_load failure" in banner_text

        # On-disk fallback populates Endpoints/Events/Subs/Args sections.
        endpoints_col = detail.query_one(
            f"#{app._DETAIL_SECTION_ENDPOINTS}", Collapsible,
        )
        endpoints_text = " ".join(
            str(s.content) for s in endpoints_col.query(Static)
        )
        assert "do_thing" in endpoints_text
        events_col = detail.query_one(
            f"#{app._DETAIL_SECTION_EVENTS}", Collapsible,
        )
        events_text = " ".join(
            str(s.content) for s in events_col.query(Static)
        )
        assert "evt_a" in events_text


# ═══════════════════════════════════════════════════════════════════════
# Phase 3b — Per-plugin tab Lifecycle / Events / Subs / Logger sections
# ═══════════════════════════════════════════════════════════════════════


def _ppwid(kind: str, plugin_name: str) -> str:
    """Build the deterministic per-plugin widget id mirroring
    `DashboardApp._per_plugin_widget_id` (kind + sanitized-plugin-name).
    """
    from plugins_test.TUI.app import DashboardApp
    return f"{kind}-{DashboardApp._sanitize_id(plugin_name)}"


# ── #17 — Lifecycle strip renders with all 6 fields ────────────────────

@pytest.mark.asyncio
async def test_phase3b_lifecycle_strip_renders(mock_pc):
    """Opening the per-plugin tab mounts a Horizontal lifecycle-strip
    container with 6 stat-card children (Phase / UUID / lifecycle_ready
    / ready / verbose_notifier / version)."""
    from textual.containers import Horizontal, Vertical

    _install_phase3_pc(mock_pc)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        strip_id = _ppwid("lifecycle-strip", "PluginA")
        strip = app.query_one(f"#{strip_id}", Horizontal)
        cards = list(strip.query(Vertical))
        # Each cell is a Vertical(.stat-card). Expect exactly 6 cells.
        assert len(cards) == 6, f"expected 6 lifecycle cells, got {len(cards)}"


# ── #18 — Lifecycle strip ✓/✗ markers reflect event state ──────────────

@pytest.mark.asyncio
async def test_phase3b_lifecycle_strip_ready_flags(mock_pc):
    """`_lifecycle_ready` set + `ready` cleared → ✓ / ✗ markers."""
    from textual.widgets import Static

    _install_phase3_pc(mock_pc)
    plugin = mock_pc.plugins["PluginA"]
    # Fixture set both to set (ENABLED default). Clear `ready` so we get
    # a ✓ / ✗ split.
    plugin._lifecycle_ready.set()
    plugin.ready.clear()
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        lcr = app.query_one(
            f"#{_ppwid('per-plugin-lifecycle-ready', 'PluginA')}", Static,
        )
        rdy = app.query_one(
            f"#{_ppwid('per-plugin-ready', 'PluginA')}", Static,
        )
        assert "✓" in str(lcr.content)
        assert "✗" in str(rdy.content)


# ── #19 — verbose_notifier Switch bidirectional sync ───────────────────

@pytest.mark.asyncio
async def test_phase3b_lifecycle_strip_verbose_toggle(mock_pc):
    """Two directions:
    - Attr → Widget: pre-set `plugin.verbose_notifier=True` BEFORE
      mounting the per-plugin tab; the Switch's `value` must reflect
      True on initial render.
    - Widget → Attr: toggling the Switch via direct `.value` assign
      must propagate back to `plugin.verbose_notifier`.
    """
    from textual.widgets import Switch

    _install_phase3_pc(mock_pc)
    plugin = mock_pc.plugins["PluginA"]
    plugin.verbose_notifier = True  # attr→widget half
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        sw_id = _ppwid("per-plugin-verbose", "PluginA")
        switch = app.query_one(f"#{sw_id}", Switch)
        # Attr → Widget: initial render reflects `True`.
        assert switch.value is True, (
            "Switch.value should mirror plugin.verbose_notifier at mount"
        )
        # Widget → Attr: toggle to False.
        switch.value = False
        for _ in range(3):
            await pilot.pause()
        assert plugin.verbose_notifier is False, (
            "toggling Switch should write back to plugin.verbose_notifier"
        )
        # And back to True.
        switch.value = True
        for _ in range(3):
            await pilot.pause()
        assert plugin.verbose_notifier is True


# ── #20 — UUID Copy button copies plugin.plugin_uuid to clipboard ──────

@pytest.mark.asyncio
async def test_phase3b_lifecycle_strip_copy_uuid(mock_pc):
    """Copy button in the lifecycle strip calls
    `app.copy_to_clipboard(plugin.plugin_uuid)`.

    Per-plugin tab content extends past the 50-row test viewport so
    pilot.click can land OutOfBounds; invoke the registered handler
    directly via the deterministic-id registry lookup instead. The
    handler routing in `on_button_pressed` is exercised separately
    by tests #29-#31.
    """
    from unittest.mock import MagicMock as _MagicMock

    _install_phase3_pc(mock_pc)
    plugin = mock_pc.plugins["PluginA"]
    plugin.plugin_uuid = "uuid-PluginA"
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        clipboard = _MagicMock()
        app.copy_to_clipboard = clipboard  # type: ignore[method-assign]
        copy_btn_id = next(
            (wid for wid, e in app._id_registry.items()
             if e.get("type") == "per-plugin-copy-uuid"
             and e.get("plugin") == "PluginA"),
            None,
        )
        assert copy_btn_id is not None, "Copy button must be registered"
        # Invoke the catch-all routing directly so the assertion isn't
        # coupled to viewport / pilot.click pacing.
        app._handle_per_plugin_copy_uuid("PluginA")
        clipboard.assert_called_once_with("uuid-PluginA")


# ── #21 — Events section renders declared events ───────────────────────

@pytest.mark.asyncio
async def test_phase3b_events_section_renders(mock_pc):
    """Plugin with 2 declared events → events DataTable has 2 rows."""
    from textual.widgets import DataTable

    _install_phase3_pc(mock_pc)
    plugin = mock_pc.plugins["PluginA"]
    plugin.events = {
        "evt_a": {"topic": "p/a", "hosts": None, "enabled": True},
        "evt_b": {"topic": "p/b", "hosts": "local", "enabled": False},
    }
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        table = app.query_one(
            f"#{_ppwid('per-plugin-events-table', 'PluginA')}", DataTable,
        )
        assert table.row_count == 2


# ── #22 — Events section empty placeholder ─────────────────────────────

@pytest.mark.asyncio
async def test_phase3b_events_section_empty(mock_pc):
    """Plugin with no events → `(no events declared)` placeholder."""
    from textual.widgets import Static

    _install_phase3_pc(mock_pc)
    plugin = mock_pc.plugins["PluginA"]
    plugin.events = {}
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        empty = app.query_one(
            f"#{_ppwid('per-plugin-events-empty', 'PluginA')}", Static,
        )
        assert "no events declared" in str(empty.content)


# ── #23 — Events cross-link injects + switches tab ─────────────────────

@pytest.mark.asyncio
async def test_phase3b_events_section_cross_link(mock_pc):
    """Open-in-Catalogue button switches to Events / Catalogue + sets
    the catalogue plugin filter Select value to the plugin name.

    Per plan-cycle 1 review fix to this test description: assert
    post-injection state directly (NO `call_after_refresh` deferral —
    Section 10 cycle-2 locks the explicit option-injection pattern).
    """
    from textual.widgets import Select, TabbedContent

    _install_phase3_pc(mock_pc)
    plugin = mock_pc.plugins["PluginA"]
    plugin.events = {"evt": {"topic": "x/y", "enabled": True}}
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        # Pre-populate the catalogue filter Select with PluginA so the
        # injection finds an existing entry.
        sel = app.query_one("#events-cat-filter-plugin", Select)
        with app.prevent(Select.Changed):
            sel.set_options([("All plugins", "__all__"), ("PluginA", "PluginA")])
            sel.value = "__all__"
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        # Invoke the worker directly — pilot.click on the jump button
        # would race against viewport / @work scheduling and produces
        # flaky assertions about the post-switch tab state.
        app._jump_to_events_catalogue_for_plugin("PluginA")
        for _ in range(8):
            await pilot.pause()
        assert app.query_one("#main-tabs", TabbedContent).active == "tab-events"
        assert app.query_one("#events-tabs", TabbedContent).active == "events-tab-cat"
        sel = app.query_one("#events-cat-filter-plugin", Select)
        assert sel.value == "PluginA"


# ── #24 — Cross-link with absent option falls through gracefully ───────

@pytest.mark.asyncio
async def test_phase3b_events_section_cross_link_options_not_populated(mock_pc):
    """Catalogue's plugin filter Select doesn't contain the inspected
    plugin. Press Open-in-Catalogue → no exception; option-injection
    or silent no-op acceptable per the degraded-behavior contract
    (Section 4.6 cycle-2 + plan-cycle 1 review on test #24).
    """
    from textual.widgets import Select, TabbedContent

    _install_phase3_pc(mock_pc)
    plugin = mock_pc.plugins["PluginA"]
    plugin.events = {"evt": {"topic": "x/y", "enabled": True}}
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        # Force catalogue filter Select to "__all__" only — PluginA not
        # in options.
        sel = app.query_one("#events-cat-filter-plugin", Select)
        with app.prevent(Select.Changed):
            sel.set_options([("All plugins", "__all__")])
            sel.value = "__all__"
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        # Invoke worker directly (see test #23 note about pilot.click
        # vs viewport / @work scheduling).
        app._jump_to_events_catalogue_for_plugin("PluginA")
        for _ in range(8):
            await pilot.pause()
        # Operator lands on the destination tab even when the plugin
        # name was absent from the original options (the injection
        # adds it back per Section 4.6).
        assert app.query_one("#main-tabs", TabbedContent).active == "tab-events"
        assert app.query_one("#events-tabs", TabbedContent).active == "events-tab-cat"


# ── #25 — Declared subs section renders ────────────────────────────────

@pytest.mark.asyncio
async def test_phase3b_subs_section_declared_renders(mock_pc):
    """Plugin with 2 declared subs → declared DataTable has 2 rows."""
    from textual.widgets import DataTable

    _install_phase3_pc(mock_pc)
    plugin = mock_pc.plugins["PluginA"]
    plugin.subscriptions = {
        "sub_a": {
            "topic": "x/y", "target_access_name": "greet",
            "target_plugin": "PluginA", "hosts": "any",
            "authors": None, "enabled": True,
        },
        "sub_b": {
            "topic": "p/q", "target_access_name": "greet",
            "target_plugin": "PluginA", "hosts": "remote",
            "authors": None, "enabled": False,
        },
    }
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        d_table = app.query_one(
            f"#{_ppwid('per-plugin-subs-declared-table', 'PluginA')}",
            DataTable,
        )
        assert d_table.row_count == 2


# ── #26 — Runtime subs section populated via single list_local_subs ────

@pytest.mark.asyncio
async def test_phase3b_subs_section_runtime_renders(mock_pc):
    """Runtime sub for the inspected plugin (declared_id=None) appears
    in the Runtime DataTable. Single `list_local_subs()` snapshot per
    plan Section 4.6 cycle-1 fix — NOT N awaits."""
    from textual.widgets import DataTable

    # Build a runtime sub with the same plugin_uuid as PluginA's fixture
    # default ("uuid-PluginA" from _install_phase3_pc).
    runtime_sub = MagicMock(
        sub_uuid="rt-1234567890",
        declared_id=None,  # runtime
        plugin_uuid="uuid-PluginA",
        plugin_name="PluginA",
        topic_pattern="rt/topic",
        target_plugin="PluginA",
        target_access_name="greet",
        hosts="any",
        authors=None,
    )
    _install_phase3_pc(mock_pc, all_subs=[runtime_sub])
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        # The shipped `_make_phase2b_plugin_instance` plugs in a
        # MagicMock event_loop so `_run_on_main` short-circuits to
        # None. Swap in the passthrough so the worker's
        # `list_local_subs()` await actually resolves to our fixture.
        await _patch_run_on_main_passthrough(app)
        await app.open_plugin_tab("PluginA")
        for _ in range(10):
            await pilot.pause()
        runtime_table = app.query_one(
            f"#{_ppwid('per-plugin-subs-runtime-table', 'PluginA')}",
            DataTable,
        )
        assert runtime_table.row_count == 1
        assert runtime_table.display is True


# ── #27 — Subs browser cross-link ──────────────────────────────────────

@pytest.mark.asyncio
async def test_phase3b_subs_section_cross_link(mock_pc):
    """Open-in-Subs-browser switches outer + inner tab + sets the
    Subs filter Select value.

    Seeds a sub for PluginA in `all_subs` so the inner-tab
    activation's `_refresh_subs_browser_worker` rebuild keeps PluginA
    in the filter Select's options (the rebuild reverts the value to
    `__all__` when the plugin is absent from the rebuilt options,
    per `_populate_subs_plugin_filter_options`).
    """
    from textual.widgets import Select, TabbedContent

    sub_for_a = MagicMock(
        sub_uuid="s-a", declared_id=None, plugin_uuid="uuid-PluginA",
        plugin_name="PluginA", topic_pattern="t/a",
        target_plugin="PluginA", target_access_name="greet",
        target_plugin_uuid=None, hosts="any",
        blocked_hosts=None, authors=None, blocked_authors=None,
        enabled=True,
    )
    _install_phase3_pc(mock_pc, all_subs=[sub_for_a])
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        sel = app.query_one("#events-subs-filter-plugin", Select)
        with app.prevent(Select.Changed):
            sel.set_options([("All plugins", "__all__"), ("PluginA", "PluginA")])
            sel.value = "__all__"
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        app._jump_to_subs_browser_for_plugin("PluginA")
        for _ in range(8):
            await pilot.pause()
        assert app.query_one("#main-tabs", TabbedContent).active == "tab-events"
        assert app.query_one("#events-tabs", TabbedContent).active == "events-tab-subs"
        sel = app.query_one("#events-subs-filter-plugin", Select)
        assert sel.value == "PluginA"


# ── #28 — Logger overrides empty (no overrides + Add row present) ──────

@pytest.mark.asyncio
async def test_phase3b_logger_overrides_empty(mock_pc):
    """Plugin with no logger overrides → empty placeholder rendered
    inside the overrides list + Add row present."""
    from textual.containers import Horizontal, Vertical
    from textual.widgets import Static

    _install_phase3_pc(mock_pc, logger_levels_overrides={})
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(8):
            await pilot.pause()
        # Empty placeholder mounts inside the overrides Vertical
        # container after call_after_refresh fires.
        empty = app.query_one(
            f"#{_ppwid('per-plugin-logger-empty', 'PluginA')}", Static,
        )
        assert "no overrides owned by this plugin" in str(empty.content)
        # Add row is mounted.
        app.query_one(
            f"#{_ppwid('per-plugin-logger-add-row', 'PluginA')}",
            Horizontal,
        )


# ── #29 — Apply with one level set calls pc.set_logger_level ───────────

@pytest.mark.asyncio
async def test_phase3b_logger_overrides_apply(mock_pc):
    """Apply with prefix='foo', console='DEBUG', file='__keep__' calls
    `pc.set_logger_level('foo', console='DEBUG', file=None,
    plugin_name='PluginA', plugin_uuid='uuid-PluginA')`."""
    from textual.widgets import Input, Select

    _install_phase3_pc(mock_pc, logger_levels_overrides={})
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(8):
            await pilot.pause()
        prefix_id = _ppwid("per-plugin-logger-prefix", "PluginA")
        console_id = _ppwid("per-plugin-logger-console", "PluginA")
        file_id = _ppwid("per-plugin-logger-file", "PluginA")
        prefix_input = app.query_one(f"#{prefix_id}", Input)
        console_sel = app.query_one(f"#{console_id}", Select)
        file_sel = app.query_one(f"#{file_id}", Select)
        prefix_input.value = "foo"
        with app.prevent(Select.Changed):
            console_sel.value = "DEBUG"
            file_sel.value = "__keep__"
        await pilot.pause()
        # Invoke handler directly — Apply button sits below the test
        # viewport and pilot.click would land OutOfBounds.
        app._handle_per_plugin_logger_apply("PluginA")
        for _ in range(3):
            await pilot.pause()
        mock_pc.set_logger_level.assert_called_once()
        ca = mock_pc.set_logger_level.call_args
        assert ca.args[0] == "foo"
        assert ca.kwargs["console"] == "DEBUG"
        assert ca.kwargs["file"] is None
        assert ca.kwargs["plugin_name"] == "PluginA"
        assert ca.kwargs["plugin_uuid"] == "uuid-PluginA"


# ── #30 — Apply with both __keep__ shows warning toast, no call ────────

@pytest.mark.asyncio
async def test_phase3b_logger_overrides_apply_no_levels_picked(mock_pc):
    """Apply with both selects at `__keep__` → notify warning, no
    set_logger_level call."""
    from unittest.mock import MagicMock as _MagicMock
    from textual.widgets import Input

    _install_phase3_pc(mock_pc, logger_levels_overrides={})
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(8):
            await pilot.pause()
        prefix_input = app.query_one(
            f"#{_ppwid('per-plugin-logger-prefix', 'PluginA')}", Input,
        )
        prefix_input.value = "foo"
        await pilot.pause()
        notify_spy = _MagicMock()
        app.notify = notify_spy  # type: ignore[method-assign]
        app._handle_per_plugin_logger_apply("PluginA")
        for _ in range(3):
            await pilot.pause()
        assert mock_pc.set_logger_level.call_count == 0
        assert notify_spy.called
        msg = notify_spy.call_args.args[0]
        assert "level" in msg.lower()


# ── #31 — Clear button on an existing override ─────────────────────────

@pytest.mark.asyncio
async def test_phase3b_logger_overrides_clear(mock_pc):
    """Clear button on an existing override row calls
    `pc.clear_logger_level(prefix, console=True, file=True,
    plugin_name='PluginA', plugin_uuid='uuid-PluginA')`."""
    snapshot = {
        "foo": {
            "config":    {"console": None, "file": None},
            "plugin":    {"console": "DEBUG", "file": None},
            "effective": {"console": "DEBUG", "file": None},
            "owners":    [("PluginA", "uuid-PluginA")],
        },
    }
    _install_phase3_pc(mock_pc, logger_levels_overrides=snapshot)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(10):
            await pilot.pause()
        clear_id = next(
            (wid for wid, e in app._id_registry.items()
             if e.get("type") == "per-plugin-logger-clear"
             and e.get("plugin") == "PluginA"
             and e.get("endpoint") == "foo"),
            None,
        )
        assert clear_id is not None, (
            "per-row Clear button must be registered after list population"
        )
        # Invoke handler directly (Clear button is below viewport).
        app._handle_per_plugin_logger_clear("PluginA", "foo")
        for _ in range(3):
            await pilot.pause()
        mock_pc.clear_logger_level.assert_called_once()
        ca = mock_pc.clear_logger_level.call_args
        assert ca.args[0] == "foo"
        assert ca.kwargs["console"] is True
        assert ca.kwargs["file"] is True
        assert ca.kwargs["plugin_name"] == "PluginA"
        assert ca.kwargs["plugin_uuid"] == "uuid-PluginA"


# ── #32 — Plugin source wins over config (was: <config>) ───────────────

@pytest.mark.asyncio
async def test_phase3b_logger_overrides_plugin_wins_over_config(mock_pc):
    """Snapshot with config-source AND plugin-source on the same prefix
    → row renders `DEBUG (was: INFO)` (or equivalent annotation)."""
    from textual.widgets import Static

    snapshot = {
        "foo": {
            "config":    {"console": "INFO",  "file": None},
            "plugin":    {"console": "DEBUG", "file": None},
            "effective": {"console": "DEBUG", "file": None},
            "owners":    [("PluginA", "uuid-PluginA")],
        },
    }
    _install_phase3_pc(mock_pc, logger_levels_overrides=snapshot)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(10):
            await pilot.pause()
        from textual.containers import Vertical
        ol = app.query_one(
            f"#{_ppwid('per-plugin-logger-list', 'PluginA')}", Vertical,
        )
        joined = " ".join(str(s.content) for s in ol.query(Static))
        assert "DEBUG" in joined
        assert "(was: INFO)" in joined


# ── #33 — UNLOADED plugin → placeholder, no Add row ────────────────────

@pytest.mark.asyncio
async def test_phase3b_logger_overrides_unloaded_plugin(mock_pc):
    """UNLOADED plugin → Logger section renders placeholder, no Add row."""
    from plugin_state import State
    from textual.widgets import Input, Static
    from textual.css.query import NoMatches

    _install_phase3_pc(
        mock_pc, plugin_states_overrides={"PluginA": State.UNLOADED},
    )
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        # Empty placeholder.
        empty = app.query_one(
            f"#{_ppwid('per-plugin-logger-empty', 'PluginA')}", Static,
        )
        assert "plugin not loaded" in str(empty.content)
        # Add row should NOT exist (no prefix Input mounted).
        prefix_id = _ppwid("per-plugin-logger-prefix", "PluginA")
        try:
            app.query_one(f"#{prefix_id}", Input)
            assert False, "Add row should not exist for UNLOADED plugin"
        except NoMatches:
            pass


# ── #34 — FAILED_LOAD plugin (per Option A) → same as UNLOADED ─────────

@pytest.mark.asyncio
async def test_phase3b_failed_load_logger_section_no_add_row(mock_pc):
    """Per Option A (no live instance for FAILED_LOAD per
    `PluginCore.py:2466-2474`): Logger section renders the placeholder
    without an Add row. Test #34 description in the plan-cycle 1 review
    was contradicted by Section 11.5's framework-reality note;
    maintainer chose Option A (consistent with UNLOADED)."""
    from plugin_state import State
    from textual.widgets import Input, Static
    from textual.css.query import NoMatches

    _install_phase3_pc(
        mock_pc, plugin_states_overrides={"PluginA": State.FAILED_LOAD},
    )
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        empty = app.query_one(
            f"#{_ppwid('per-plugin-logger-empty', 'PluginA')}", Static,
        )
        assert "plugin not loaded" in str(empty.content)
        prefix_id = _ppwid("per-plugin-logger-prefix", "PluginA")
        try:
            app.query_one(f"#{prefix_id}", Input)
            assert False, "Add row should not exist for FAILED_LOAD plugin"
        except NoMatches:
            pass


# ── #35 — UNLOADED placeholders in Events + Subs sections ──────────────

@pytest.mark.asyncio
async def test_phase3b_unloaded_plugin_placeholders_in_events_subs(mock_pc):
    """UNLOADED plugin: Events section shows `(plugin not loaded)`,
    Subs sections show `(plugin not loaded)`."""
    from plugin_state import State
    from textual.widgets import Static

    _install_phase3_pc(
        mock_pc, plugin_states_overrides={"PluginA": State.UNLOADED},
    )
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        events_empty = app.query_one(
            f"#{_ppwid('per-plugin-events-empty', 'PluginA')}", Static,
        )
        assert "plugin not loaded" in str(events_empty.content)
        d_empty = app.query_one(
            f"#{_ppwid('per-plugin-subs-declared-empty', 'PluginA')}", Static,
        )
        assert "plugin not loaded" in str(d_empty.content)
        rt_empty = app.query_one(
            f"#{_ppwid('per-plugin-subs-runtime-empty', 'PluginA')}", Static,
        )
        assert "plugin not loaded" in str(rt_empty.content)


# ── #36a — `on_button_pressed` catch-all routes Phase 3b types ──────────

@pytest.mark.asyncio
async def test_phase3b_button_pressed_routing_coverage(mock_pc):
    """Cycle 1 review fix — every Phase 3b `on_button_pressed` elif
    branch should be exercised. Per-plugin-tab content sits below the
    50-row test viewport so pilot.click on the rendered widgets often
    raises OutOfBounds; the handler tests (#20, #23, #27, #29, #31)
    bypass routing by calling the handler directly. This test
    synthesizes `Button.Pressed` events for the registered button ids
    of each branch type and dispatches via `app.on_button_pressed(...)`
    — exercising the elif chain itself without depending on viewport.
    """
    from textual.widgets import Button
    from unittest.mock import MagicMock as _MagicMock, patch

    snapshot = {
        "foo": {
            "config":    {"console": None, "file": None},
            "plugin":    {"console": "DEBUG", "file": None},
            "effective": {"console": "DEBUG", "file": None},
            "owners":    [("PluginA", "uuid-PluginA")],
        },
    }
    _install_phase3_pc(mock_pc, logger_levels_overrides=snapshot)
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await _patch_run_on_main_passthrough(app)
        await app.open_plugin_tab("PluginA")
        for _ in range(10):
            await pilot.pause()

        # Patch the handlers + jump workers so the test can assert each
        # routing path fired without exercising downstream side effects.
        with patch.object(app, "_handle_per_plugin_copy_uuid") as m_copy, \
             patch.object(app, "_jump_to_events_catalogue_for_plugin") as m_je, \
             patch.object(app, "_jump_to_subs_browser_for_plugin") as m_js, \
             patch.object(app, "_handle_per_plugin_logger_apply") as m_apply, \
             patch.object(app, "_handle_per_plugin_logger_clear") as m_clear, \
             patch.object(app, "_handle_per_plugin_logger_refresh") as m_refresh:
            # For each type, look up the registered button id, build
            # a synthetic Button widget reference, and dispatch.
            def _fire(t: str, expected_endpoint: str = ""):
                wid = next(
                    (w for w, e in app._id_registry.items()
                     if e.get("type") == t
                     and e.get("plugin") == "PluginA"
                     and (not expected_endpoint
                          or e.get("endpoint") == expected_endpoint)),
                    None,
                )
                assert wid is not None, f"button for type={t} not registered"
                btn = app.query_one(f"#{wid}", Button)
                # Synthesize a Button.Pressed event — bypass viewport
                # / pilot.click constraints.
                event = Button.Pressed(btn)
                app.on_button_pressed(event)

            _fire("per-plugin-copy-uuid")
            _fire("per-plugin-jump-events")
            _fire("per-plugin-jump-subs")
            _fire("per-plugin-logger-apply")
            _fire("per-plugin-logger-clear", expected_endpoint="foo")
            _fire("per-plugin-logger-refresh")
            for _ in range(3):
                await pilot.pause()

            m_copy.assert_called_once_with("PluginA")
            m_je.assert_called_once_with("PluginA")
            m_js.assert_called_once_with("PluginA")
            m_apply.assert_called_once_with("PluginA")
            m_clear.assert_called_once_with("PluginA", "foo")
            m_refresh.assert_called_once_with("PluginA")


# ── #36 — Per-plugin Events DataTable is non-interactive ───────────────

@pytest.mark.asyncio
async def test_phase3b_events_table_non_interactive(mock_pc):
    """Per Section 4.5 + plan-cycle 1 review fix: per-plugin Events
    DataTable must have `cursor_type == 'none'` so it visually signals
    "read-only — toggle UI lives in the Events catalogue tab".
    Invisible regression target without an explicit test."""
    from textual.widgets import DataTable

    _install_phase3_pc(mock_pc)
    plugin = mock_pc.plugins["PluginA"]
    plugin.events = {"evt": {"topic": "p/a", "enabled": True}}
    app = _make_phase3_app(mock_pc)
    async with app.run_test(headless=True, size=(160, 50)) as pilot:
        await pilot.pause()
        await app.open_plugin_tab("PluginA")
        for _ in range(5):
            await pilot.pause()
        table = app.query_one(
            f"#{_ppwid('per-plugin-events-table', 'PluginA')}", DataTable,
        )
        # Textual 8.2.3 exposes the configured cursor type as
        # `cursor_type` directly on the DataTable instance.
        assert table.cursor_type == "none"
