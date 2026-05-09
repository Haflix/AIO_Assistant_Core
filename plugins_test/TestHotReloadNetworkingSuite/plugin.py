"""TestHotReloadNetworkingSuite — Commit 2b orchestrator regression tests.

Exercises the hot-reload code paths added by Commit 2b for B-070:

  * `_networking_config_changed(old, new)` — diff returns True only on
    rebuild-relevant fields.
  * `_normalize_networking_for_diff(yaml)` — defaults filled correctly
    so absent-vs-explicit-default doesn't false-positive the diff.
  * `_validate_networking_config(yaml)` — raises on malformed peer
    entries; short-circuits on `enabled: false`.
  * `_rebuild_networking(new_yaml)` — construction failure leaves old
    state intact (HIGH-B fix).

These tests run as pure-function checks against the helpers + a
monkeypatch case for the rebuild abort path. No real TLS peers or
subprocess required — the integration smoke for the full rebuild
flow is in the manual smoke-test plan in
`_private/framework_changes_plan.md` Session 1.

Cases (5):

1. `hotreload.diff.detects_peer_change` — different peers list →
   `_networking_config_changed=True`
2. `hotreload.diff.skips_heartbeat_change` — only heartbeat_interval
   differs → `_networking_config_changed=False`
3. `hotreload.diff.absent_hostname_normalizes_equal` — both yamls
   miss `general.hostname` → normalize fills with
   `socket.gethostname()`, diff returns False
4. `hotreload.validate.rejects_bad_cert_pem` — peer with malformed
   `cert_pem` → `_validate_networking_config` raises RuntimeError
5. `hotreload.rebuild.construction_failure_keeps_old_state` —
   monkeypatch `_build_network_manager` to raise; verify
   `_rebuild_networking` early-returns without mutating state

Spec: `_private/next_session_handoff.md` (Commit 2b Step 8).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copy import deepcopy  # noqa: E402
from typing import Any, Dict, List, Optional  # noqa: E402

from utils import Plugin  # noqa: E402
from decorators import async_log_errors, log_errors  # noqa: E402

from _test_helpers import CaseRecorder  # noqa: E402


SUITE_VERSION = "0.1.0"


def _peer_entry_with_inline_pem(hostname: str, ip: str, port: int, pem: str) -> Dict[str, Any]:
    """Build a peer config dict with an inline cert_pem block.

    Used by diff/normalize cases — the cert PEM is opaque to those
    tests; only validate cases parse it.
    """
    return {
        "hostname": hostname,
        "address": f"{ip}:{port}",
        "cert_pem": pem,
    }


# Minimal placeholder PEM used in diff/normalize cases (NOT validated).
# Validate-cert-pem cases use a deliberately-malformed string.
_PLACEHOLDER_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "PLACEHOLDER\n"
    "-----END CERTIFICATE-----\n"
)


def _baseline_yaml() -> Dict[str, Any]:
    """Return a minimal yaml dict resembling a real config — ``general`` +
    ``networking`` sections, minimal ``plugins`` key. Other suites run
    fine without these specific values; this is a fixture for the
    helpers under test."""
    return {
        "general": {},
        "networking": {
            "enabled": True,
            "port": 2510,
            "peers": [
                _peer_entry_with_inline_pem(
                    "alpha", "10.0.0.1", 2510, _PLACEHOLDER_PEM,
                ),
            ],
            "heartbeat_interval": 10.0,
            "lookup_interval": 60.0,
            "liveness_timeout": 30.0,
        },
        "plugins": [],
    }


class TestHotReloadNetworkingSuite(Plugin):
    """Commit 2b hot-reload orchestrator regression suite."""

    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestHotReloadNetworkingSuite enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestHotReloadNetworkingSuite disabled")

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
        rec = CaseRecorder(
            "TestHotReloadNetworkingSuite", SUITE_VERSION, self._plugin_core
        )

        kw = dict(
            case_ids_filter=case_ids,
            bug_ids_filter=bug_ids,
            category_filter=category,
            host_filter=host,
            skip_slow=skip_slow,
            allow_destructive=allow_destructive,
            remote_available=False,
        )

        await self._case01_diff_detects_peer_change(rec, kw)
        await self._case02_diff_skips_heartbeat_change(rec, kw)
        await self._case03_absent_hostname_normalizes_equal(rec, kw)
        await self._case04_validate_rejects_bad_cert_pem(rec, kw)
        await self._case05_rebuild_construction_failure_keeps_old_state(rec, kw)

        return rec.to_dict()

    # ====================================================================
    # Cases
    # ====================================================================

    async def _case01_diff_detects_peer_change(self, rec, kw):
        """B-070 _networking_config_changed: True when peers differ."""

        async def body(c):
            pc = self._plugin_core
            old = _baseline_yaml()
            new = deepcopy(old)
            new["networking"]["peers"] = [
                _peer_entry_with_inline_pem(
                    "alpha", "10.0.0.1", 2510, _PLACEHOLDER_PEM,
                ),
                _peer_entry_with_inline_pem(
                    "beta", "10.0.0.2", 2510, _PLACEHOLDER_PEM,
                ),
            ]
            c.expect(pc._networking_config_changed(old, new), True)

        await rec.run_case(
            "hotreload.diff.detects_peer_change",
            body,
            hosts=("local",),
            tags=("diff",),
            bug_ids=("B-070",),
            **kw,
        )

    async def _case02_diff_skips_heartbeat_change(self, rec, kw):
        """B-070 _networking_config_changed: False when only
        non-rebuild fields (heartbeat_interval) differ."""

        async def body(c):
            pc = self._plugin_core
            old = _baseline_yaml()
            new = deepcopy(old)
            # heartbeat_interval is NOT in _REBUILD_FIELDS — change
            # should NOT trigger rebuild.
            new["networking"]["heartbeat_interval"] = 20.0
            c.expect(pc._networking_config_changed(old, new), False)

        await rec.run_case(
            "hotreload.diff.skips_heartbeat_change",
            body,
            hosts=("local",),
            tags=("diff",),
            bug_ids=("B-070",),
            **kw,
        )

    async def _case03_absent_hostname_normalizes_equal(self, rec, kw):
        """B-070 _normalize_networking_for_diff: absent
        ``general.hostname`` filled with ``socket.gethostname()`` on
        BOTH sides → diff returns False, mirroring
        ``apply_configvalues``' fallback behavior (utils.py:970-973)."""

        async def body(c):
            pc = self._plugin_core
            old = _baseline_yaml()
            new = deepcopy(old)
            # Both sides explicitly miss general.hostname. After
            # normalize, both get socket.gethostname() filled — equal.
            old.get("general", {}).pop("hostname", None)
            new.get("general", {}).pop("hostname", None)
            c.expect(pc._networking_config_changed(old, new), False)

        await rec.run_case(
            "hotreload.diff.absent_hostname_normalizes_equal",
            body,
            hosts=("local",),
            tags=("normalize",),
            bug_ids=("B-070",),
            **kw,
        )

    async def _case04_validate_rejects_bad_cert_pem(self, rec, kw):
        """B-070 _validate_networking_config: raises on malformed
        ``cert_pem`` (no PEM header)."""

        async def body(c):
            pc = self._plugin_core
            yaml = _baseline_yaml()
            # Override the peer's cert_pem with a string that lacks
            # the PEM header. _parse_one_peer's PEM-header check
            # raises RuntimeError per networking.py:524.
            yaml["networking"]["peers"][0]["cert_pem"] = (
                "not a real pem block"
            )
            c.expect_exception(
                RuntimeError,
                match="missing PEM header",
            )
            pc._validate_networking_config(yaml)

        await rec.run_case(
            "hotreload.validate.rejects_bad_cert_pem",
            body,
            hosts=("local",),
            tags=("validate",),
            bug_ids=("B-070",),
            **kw,
        )

    async def _case05_rebuild_construction_failure_keeps_old_state(
        self, rec, kw
    ):
        """B-070 _rebuild_networking: construction failure (monkeypatch
        ``_build_network_manager`` to raise) leaves ``self.network``
        and ``self.yaml_config`` unchanged. Per cycle 3 HIGH-B + the
        Step 7 docstring's "if construct raises → log error, abort,
        NO state mutation" guarantee.
        """

        async def body(c):
            pc = self._plugin_core
            # Snapshot pre-call state for restoration + assertion.
            old_network = pc.network
            old_yaml = deepcopy(pc.yaml_config)
            old_networking_port = getattr(pc, "networking_port", None)

            # New yaml has networking.enabled=True so step 1 attempts
            # construction; without monkeypatch it would actually try
            # to build a NetworkManager and fail on the placeholder
            # PEM. Monkeypatch to raise a distinctive RuntimeError so
            # we can verify the abort path fires deterministically.
            new_yaml = _baseline_yaml()

            class _SentinelError(RuntimeError):
                pass

            original_build = pc._build_network_manager

            def _raise_for_test(_yaml_config):
                raise _SentinelError("monkeypatched construction failure")

            try:
                pc._build_network_manager = _raise_for_test  # type: ignore[method-assign]
                # _rebuild_networking should catch the exception, log
                # it, and return WITHOUT mutating state.
                await pc._rebuild_networking(new_yaml)
            finally:
                pc._build_network_manager = original_build  # type: ignore[method-assign]

            # State must be unchanged: self.network identity, yaml,
            # and networking_* attrs all match pre-call snapshot.
            c.expect(pc.network, old_network)
            c.expect(pc.yaml_config, old_yaml)
            c.expect(getattr(pc, "networking_port", None), old_networking_port)

        await rec.run_case(
            "hotreload.rebuild.construction_failure_keeps_old_state",
            body,
            hosts=("local",),
            tags=("rebuild", "monkeypatch"),
            bug_ids=("B-070",),
            **kw,
        )
