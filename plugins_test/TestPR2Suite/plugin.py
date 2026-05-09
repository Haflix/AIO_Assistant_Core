"""TestPR2Suite — PR2 config-restructure regression tests.

Exercises:
  - Endpoint dict-form load (vs rejected list-form)
  - internal_name and access_name field defaulting and mismatch handling
  - apply_overrides engine: single-field, __replace__, strict unknown-key
    (endpoints), lenient unknown-key (plugin-level), list-value rejection,
    empty overrides, description override
  - Stray top-level arguments: on plugin entry (legacy field warning)
  - Plugin with endpoints: null / absent loads with 0 endpoints

All 15 cases map 1:1 to the test spec in notes.txt section I STEP 6 and
section H TESTING.

Fixture plugins used (all live in plugins_test/):
  TestPR2Fixture        — dict-form endpoints, no internal_name, no access_name field
  TestPR2ListFixture    — legacy list-form endpoints (should be rejected)
  TestPR2NullEpFixture  — endpoints: null
  TestPR2MismatchFixture — access_name field != key
  TestPR2MatchFixture    — access_name field == key

Fixtures are loaded on-demand via load_plugin_with_conf (§5.4 idiom from
TestNotifierSuite) so they don't pollute test_config.yml's auto-start list.
They are registered in test_config.yml with enabled:false for path resolution.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from typing import Any, Dict, List, Optional  # noqa: E402

from utils import Plugin  # noqa: E402
from decorators import async_log_errors, log_errors  # noqa: E402
from PluginCore import apply_overrides  # noqa: E402

from _test_helpers import CaseRecorder  # noqa: E402


SUITE_VERSION = "0.1.1"

# Fixture plugin names (must match test_config.yml entries)
FIXTURE        = "TestPR2Fixture"
LIST_FIXTURE   = "TestPR2ListFixture"
NULL_FIXTURE   = "TestPR2NullEpFixture"
MISMATCH       = "TestPR2MismatchFixture"
MATCH          = "TestPR2MatchFixture"


class TestPR2Suite(Plugin):
    """PR2 config-restructure regression suite."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestPR2Suite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestPR2Suite disabled")

    @async_log_errors
    async def run(
        self,
        category: Optional[str] = None,
        host: Optional[str] = None,
        case_ids: Optional[List[str]] = None,
        bug_ids: Optional[List[str]] = None,
        skip_slow: bool = False,
        allow_destructive: bool = True,
    ) -> Dict[str, Any]:
        rec = CaseRecorder("TestPR2Suite", SUITE_VERSION, self._plugin_core)

        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=False,
        )

        await self._case01_list_form_rejected(rec, kw)
        await self._case02_dict_form_loads_callable(rec, kw)
        await self._case03_no_internal_name_defaults_to_key(rec, kw)
        await self._case04_no_access_name_field_defaults_to_key(rec, kw)
        await self._case05_access_name_eq_key_no_warn(rec, kw)
        await self._case06_access_name_ne_key_warn_key_wins(rec, kw)
        await self._case07_override_single_field_sibling_untouched(rec, kw)
        await self._case08_replace_missing_required_fields_error(rec, kw)
        await self._case09_override_unknown_endpoint_key_error(rec, kw)
        await self._case10_override_unknown_plugin_level_field_warn(rec, kw)
        await self._case11_override_endpoints_list_value_rejected(rec, kw)
        await self._case12_stray_arguments_on_entry_warns(rec, kw)
        await self._case13_null_endpoints_loads_zero(rec, kw)
        await self._case14_empty_overrides_noop(rec, kw)
        await self._case15_description_override_reflected(rec, kw)

        return rec.to_dict()

    # ====================================================================
    # Helpers
    # ====================================================================

    def _find_yaml_entry(self, name: str) -> Optional[Dict[str, Any]]:
        for entry in self._plugin_core.yaml_config.get("plugins", []):
            if entry.get("name") == name:
                return entry
        return None

    async def _ensure_unloaded(self, name: str) -> None:
        """Pop a fixture if it somehow ended up loaded (cleanup helper)."""
        if name in self._plugin_core.plugins:
            try:
                await self._plugin_core.pop_plugin(name)
            except Exception:
                pass

    async def _load_fixture(self, name: str, overrides: Optional[dict] = None) -> bool:
        """Load fixture by name via its registered yaml entry.

        If `overrides` is provided, inject it into the plugin_entry before
        passing to load_plugin_with_conf. Returns True if plugin ended up in
        core.plugins.
        """
        entry = self._find_yaml_entry(name)
        if not entry:
            return False
        # Make a shallow copy so we don't mutate the live yaml_config
        entry_copy = dict(entry)
        entry_copy["enabled"] = True
        if overrides is not None:
            entry_copy["overrides"] = overrides
        await self._plugin_core.load_plugin_with_conf(entry_copy)
        return name in self._plugin_core.plugins

    # ====================================================================
    # Case 01 — list-form endpoints → load fails with dict-form error
    # ====================================================================

    async def _case01_list_form_rejected(self, rec: CaseRecorder, kw: dict) -> None:
        async def body(c):
            await self._ensure_unloaded(LIST_FIXTURE)
            loaded = await self._load_fixture(LIST_FIXTURE)
            # The plugin should NOT be present — error_config calls pop_plugin
            if loaded:
                c.set_marker("plugin_loaded_despite_list_form")
                raise AssertionError(
                    "list-form endpoints should cause load failure "
                    "but plugin ended up in core.plugins"
                )

        await rec.run_case(
            "pr2.config.list_form_endpoints_rejected",
            body,
            tags=("pr2", "endpoints", "validation"),
            **kw,
        )

    # ====================================================================
    # Case 02 — dict-form endpoints → endpoint accessible via execute()
    # ====================================================================

    async def _case02_dict_form_loads_callable(self, rec: CaseRecorder, kw: dict) -> None:
        async def body(c):
            await self._ensure_unloaded(FIXTURE)
            loaded = await self._load_fixture(FIXTURE)
            if not loaded:
                raise AssertionError(
                    f"{FIXTURE} failed to load (not in core.plugins)"
                )
            try:
                await self._plugin_core.enable_plugin(FIXTURE)
                result = await self.execute(FIXTURE, "ping")
                c.expect(result, "pong")
            finally:
                await self._ensure_unloaded(FIXTURE)

        await rec.run_case(
            "pr2.config.dict_form_endpoint_callable",
            body,
            tags=("pr2", "endpoints"),
            **kw,
        )

    # ====================================================================
    # Case 03 — no internal_name field → defaults to key
    # ====================================================================

    async def _case03_no_internal_name_defaults_to_key(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            await self._ensure_unloaded(FIXTURE)
            loaded = await self._load_fixture(FIXTURE)
            if not loaded:
                raise AssertionError(f"{FIXTURE} failed to load")
            try:
                plugin = self._plugin_core.plugins[FIXTURE]
                ep = plugin.endpoints.get("ping")
                if ep is None:
                    raise AssertionError("endpoint 'ping' not found in plugin.endpoints")
                # internal_name absent in config → defaults to key at call time
                # (PluginCore._call_endpoint uses ep.get("internal_name") or function_name)
                if "internal_name" in ep:
                    raise AssertionError(
                        f"expected no internal_name key in stored endpoint dict, "
                        f"got: {ep!r}"
                    )
                # Verify dispatch actually resolves the key as the method name
                await self._plugin_core.enable_plugin(FIXTURE)
                result = await self.execute(FIXTURE, "ping")
                c.expect(result, "pong")
            finally:
                await self._ensure_unloaded(FIXTURE)

        await rec.run_case(
            "pr2.config.no_internal_name_defaults_to_key",
            body,
            tags=("pr2", "endpoints", "internal_name"),
            **kw,
        )

    # ====================================================================
    # Case 04 — no access_name field → defaults to key (load succeeds)
    # ====================================================================

    async def _case04_no_access_name_field_defaults_to_key(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            await self._ensure_unloaded(FIXTURE)
            loaded = await self._load_fixture(FIXTURE)
            if not loaded:
                raise AssertionError(f"{FIXTURE} failed to load")
            try:
                plugin = self._plugin_core.plugins[FIXTURE]
                # The key 'ping' IS the access_name — no access_name field needed
                ep = plugin.endpoints.get("ping")
                if ep is None:
                    raise AssertionError(
                        "endpoint 'ping' not in plugin.endpoints — "
                        "key should serve as access_name"
                    )
                # No access_name field in the stored dict
                if "access_name" in ep:
                    raise AssertionError(
                        f"access_name field unexpectedly present in stored endpoint: {ep!r}"
                    )
            finally:
                await self._ensure_unloaded(FIXTURE)

        await rec.run_case(
            "pr2.config.no_access_name_field_key_is_access_name",
            body,
            tags=("pr2", "endpoints", "access_name"),
            **kw,
        )

    # ====================================================================
    # Case 05 — access_name field == key → plugin loads, endpoint accessible
    # ====================================================================

    async def _case05_access_name_eq_key_no_warn(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            await self._ensure_unloaded(MATCH)
            loaded = await self._load_fixture(MATCH)
            if not loaded:
                raise AssertionError(f"{MATCH} failed to load")
            try:
                plugin = self._plugin_core.plugins[MATCH]
                # Endpoint must be accessible under the key 'ping'
                ep = plugin.endpoints.get("ping")
                if ep is None:
                    raise AssertionError(
                        "endpoint 'ping' not found after loading "
                        f"{MATCH} (access_name field == key)"
                    )
                # Dispatch works
                await self._plugin_core.enable_plugin(MATCH)
                result = await self.execute(MATCH, "ping")
                c.expect(result, "pong")
            finally:
                await self._ensure_unloaded(MATCH)

        await rec.run_case(
            "pr2.config.access_name_eq_key_loads_ok",
            body,
            tags=("pr2", "endpoints", "access_name"),
            **kw,
        )

    # ====================================================================
    # Case 06 — access_name field != key → WARN + endpoint accessible by KEY
    # ====================================================================

    async def _case06_access_name_ne_key_warn_key_wins(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            await self._ensure_unloaded(MISMATCH)
            loaded = await self._load_fixture(MISMATCH)
            if not loaded:
                raise AssertionError(f"{MISMATCH} failed to load")
            try:
                plugin = self._plugin_core.plugins[MISMATCH]
                # Endpoint must be accessible under the KEY 'ping', NOT 'wrong_name'
                ep_by_key = plugin.endpoints.get("ping")
                if ep_by_key is None:
                    raise AssertionError(
                        "endpoint 'ping' (key) not found — key should win over "
                        "access_name field 'wrong_name'"
                    )
                ep_by_field = plugin.endpoints.get("wrong_name")
                if ep_by_field is not None:
                    raise AssertionError(
                        "endpoint registered under access_name field 'wrong_name' — "
                        "should only be accessible by key 'ping'"
                    )
                # Dispatch works using the key
                await self._plugin_core.enable_plugin(MISMATCH)
                result = await self.execute(MISMATCH, "ping")
                c.expect(result, "pong")
            finally:
                await self._ensure_unloaded(MISMATCH)

        await rec.run_case(
            "pr2.config.access_name_ne_key_key_wins",
            body,
            tags=("pr2", "endpoints", "access_name"),
            **kw,
        )

    # ====================================================================
    # Case 07 — override single endpoint field → only that field changes
    # ====================================================================

    async def _case07_override_single_field_sibling_untouched(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            # Pure apply_overrides unit test — no disk I/O needed.
            base_config = {
                "description": "base",
                "version": "1.0.0",
                "remote": False,
                "arguments": None,
                "endpoints": {
                    "ep_a": {
                        "remote": False,
                        "accessible_by_other_plugins": True,
                        "description": "original_a",
                    },
                    "ep_b": {
                        "remote": False,
                        "accessible_by_other_plugins": True,
                        "description": "original_b",
                    },
                },
            }
            overrides = {
                "endpoints": {
                    "ep_a": {
                        "description": "overridden_a",
                    }
                }
            }
            result = apply_overrides(base_config, overrides, "test_plugin", self._logger)

            ep_a = result["endpoints"]["ep_a"]
            ep_b = result["endpoints"]["ep_b"]

            # ep_a.description changed
            c.expect(ep_a["description"], "overridden_a")
            # ep_a.remote and accessible_by_other_plugins untouched
            c.expect(ep_a["remote"], False)
            c.expect(ep_a["accessible_by_other_plugins"], True)
            # ep_b entirely untouched
            c.expect(ep_b["description"], "original_b")
            c.expect(ep_b["remote"], False)

        await rec.run_case(
            "pr2.apply_overrides.single_field_sibling_untouched",
            body,
            tags=("pr2", "overrides"),
            **kw,
        )

    # ====================================================================
    # Case 08 — __replace__: true → wholesale replace;
    #           missing required fields → ERROR (C12) via load_plugin_with_conf
    # ====================================================================

    async def _case08_replace_missing_required_fields_error(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            await self._ensure_unloaded(FIXTURE)
            # Override ep 'ping' with __replace__ that omits 'remote' and
            # 'accessible_by_other_plugins' — should trigger error_config and
            # leave the plugin absent from core.plugins.
            overrides = {
                "endpoints": {
                    "ping": {
                        "__replace__": True,
                        "description": "replaced but missing required fields",
                        # 'remote' and 'accessible_by_other_plugins' intentionally absent
                    }
                }
            }
            loaded = await self._load_fixture(FIXTURE, overrides=overrides)
            if loaded:
                c.set_marker("plugin_loaded_despite_missing_required_fields")
                await self._ensure_unloaded(FIXTURE)
                raise AssertionError(
                    "__replace__ with missing required fields (remote, "
                    "accessible_by_other_plugins) should cause fail-load (C12) "
                    "but plugin ended up in core.plugins"
                )

        await rec.run_case(
            "pr2.apply_overrides.replace_missing_required_fields_error",
            body,
            tags=("pr2", "overrides", "replace"),
            **kw,
        )

    # ====================================================================
    # Case 09 — override unknown endpoint key → ValueError from apply_overrides
    # ====================================================================

    async def _case09_override_unknown_endpoint_key_error(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            base_config = {
                "description": "base",
                "version": "1.0.0",
                "remote": False,
                "arguments": None,
                "endpoints": {
                    "ep_a": {
                        "remote": False,
                        "accessible_by_other_plugins": True,
                    },
                },
            }
            overrides = {
                "endpoints": {
                    "does_not_exist": {
                        "remote": True,
                        "accessible_by_other_plugins": True,
                    }
                }
            }
            # apply_overrides must raise ValueError for strict section (Q2)
            c.expect_exception(ValueError, match=r"unknown")
            apply_overrides(base_config, overrides, "test_plugin", self._logger)

        await rec.run_case(
            "pr2.apply_overrides.unknown_endpoint_key_error",
            body,
            tags=("pr2", "overrides", "validation"),
            **kw,
        )

    # ====================================================================
    # Case 10 — override unknown plugin-level field → WARN + ignored
    # ====================================================================

    async def _case10_override_unknown_plugin_level_field_warn(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            base_config = {
                "description": "base",
                "version": "1.0.0",
                "remote": False,
                "arguments": None,
                "endpoints": {},
            }
            overrides = {
                "nonexistent_plugin_level_key": "some_value",
            }
            # apply_overrides must NOT raise — unknown top-level keys are warn+ignore (Q22)
            result = apply_overrides(base_config, overrides, "test_plugin", self._logger)
            # The unknown key must NOT appear in the merged config
            if "nonexistent_plugin_level_key" in result:
                raise AssertionError(
                    "unknown top-level override key 'nonexistent_plugin_level_key' "
                    "was not ignored — it appeared in the merged config"
                )
            # All base fields preserved
            c.expect(result["description"], "base")
            c.expect(result["version"], "1.0.0")

        await rec.run_case(
            "pr2.apply_overrides.unknown_plugin_level_field_ignored",
            body,
            tags=("pr2", "overrides", "validation"),
            **kw,
        )

    # ====================================================================
    # Case 11 — override endpoints: with list value → ValueError
    # ====================================================================

    async def _case11_override_endpoints_list_value_rejected(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            base_config = {
                "description": "base",
                "version": "1.0.0",
                "remote": False,
                "arguments": None,
                "endpoints": {"ep_a": {"remote": False, "accessible_by_other_plugins": True}},
            }
            overrides = {
                # endpoints override is a list, not a dict — must be rejected
                "endpoints": [
                    {"access_name": "ep_a", "remote": True, "accessible_by_other_plugins": True}
                ],
            }
            c.expect_exception(ValueError, match=r"mapping")
            apply_overrides(base_config, overrides, "test_plugin", self._logger)

        await rec.run_case(
            "pr2.apply_overrides.endpoints_list_override_rejected",
            body,
            tags=("pr2", "overrides", "validation"),
            **kw,
        )

    # ====================================================================
    # Case 12 — stray top-level arguments: on plugin entry → WARN, plugin loads
    # ====================================================================

    async def _case12_stray_arguments_on_entry_warns(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            await self._ensure_unloaded(FIXTURE)
            entry = self._find_yaml_entry(FIXTURE)
            if not entry:
                raise AssertionError(f"{FIXTURE} not found in yaml_config")
            # Inject stray top-level 'arguments:' directly on the plugin entry
            # (the legacy field location — renamed to overrides.arguments in PR2)
            entry_copy = dict(entry)
            entry_copy["enabled"] = True
            entry_copy["arguments"] = {"stray_key": "stray_value"}

            await self._plugin_core.load_plugin_with_conf(entry_copy)
            loaded = FIXTURE in self._plugin_core.plugins
            try:
                if not loaded:
                    raise AssertionError(
                        f"{FIXTURE} failed to load despite stray 'arguments:' on "
                        "plugin entry — should WARN and continue loading"
                    )
            finally:
                await self._ensure_unloaded(FIXTURE)

        await rec.run_case(
            "pr2.config.stray_arguments_on_entry_warns_loads",
            body,
            tags=("pr2", "validation", "legacy"),
            **kw,
        )

    # ====================================================================
    # Case 13 — endpoints: null → loads with 0 endpoints, no error
    # ====================================================================

    async def _case13_null_endpoints_loads_zero(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            await self._ensure_unloaded(NULL_FIXTURE)
            loaded = await self._load_fixture(NULL_FIXTURE)
            try:
                if not loaded:
                    raise AssertionError(
                        f"{NULL_FIXTURE} failed to load — plugin with "
                        "endpoints: null should load with 0 endpoints"
                    )
                plugin = self._plugin_core.plugins[NULL_FIXTURE]
                ep_count = len(plugin.endpoints) if hasattr(plugin, "endpoints") else -1
                c.expect(ep_count, 0)
            finally:
                await self._ensure_unloaded(NULL_FIXTURE)

        await rec.run_case(
            "pr2.config.null_endpoints_loads_zero_endpoints",
            body,
            tags=("pr2", "endpoints"),
            **kw,
        )

    # ====================================================================
    # Case 14 — apply_overrides with empty overrides: {} → no-op
    # ====================================================================

    async def _case14_empty_overrides_noop(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            base_config = {
                "description": "base",
                "version": "1.0.0",
                "remote": False,
                "arguments": {"key": "value"},
                "endpoints": {
                    "ep_a": {"remote": False, "accessible_by_other_plugins": True}
                },
            }
            result = apply_overrides(base_config, {}, "test_plugin", self._logger)
            # Empty overrides: {} must be a no-op — all base values preserved
            c.expect(result["description"], base_config["description"])
            c.expect(result["version"], base_config["version"])
            c.expect(result["remote"], base_config["remote"])
            c.expect(result["arguments"], base_config["arguments"])
            c.expect(result["endpoints"], base_config["endpoints"])

        await rec.run_case(
            "pr2.apply_overrides.empty_overrides_noop",
            body,
            tags=("pr2", "overrides"),
            **kw,
        )

    # ====================================================================
    # Case 15 — override description: → plugin.description reflects override
    # ====================================================================

    async def _case15_description_override_reflected(
        self, rec: CaseRecorder, kw: dict
    ) -> None:
        async def body(c):
            await self._ensure_unloaded(FIXTURE)
            overrides = {"description": "overridden description"}
            loaded = await self._load_fixture(FIXTURE, overrides=overrides)
            if not loaded:
                raise AssertionError(f"{FIXTURE} failed to load with description override")
            try:
                plugin = self._plugin_core.plugins[FIXTURE]
                c.expect(plugin.description, "overridden description")
            finally:
                await self._ensure_unloaded(FIXTURE)

        await rec.run_case(
            "pr2.apply_overrides.description_override_reflected",
            body,
            tags=("pr2", "overrides", "plugin_level"),
            **kw,
        )
