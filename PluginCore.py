import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")  # UTF-8 mode: all open() default to utf-8
if hasattr(sys.stdout, "reconfigure"):  # Reconfigure console streams to UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Windows defaults to ProactorEventLoop, which is incompatible with psycopg3
# async and other libraries. Switch to SelectorEventLoop before any loop is created.
if sys.platform == "win32":
    import asyncio as _asyncio

    _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())

import contextlib
import functools
import importlib
import inspect
import asyncio
import time
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional, Callable, Union, Dict, List
import yaml

# Tracks the sync call chain on each threadpool worker thread.
# Used by execute_sync / _call_endpoint to detect circular sync calls
# that would deadlock the ThreadPoolExecutor.
_sync_call_chain = threading.local()

from exceptions import NetworkRequestException, RequestException
from networking_classes import Node, RemotePlugin
from utils import LogUtil, Request, Plugin, ConfigUtil, GeneratorRequest
from decorators import (
    log_errors,
    handle_errors,
    async_log_errors,
    async_handle_errors,
    async_gen_log_errors,
    async_gen_handle_errors,
    gen_log_errors,
    gen_handle_errors,
)
from networking import NetworkManager, REMOTE_NO_RESULT
from notifier import TopicRegistry, Subscription


def _deep_merge_args(
    base: dict,
    override: dict,
    plugin_name: str,
    logger,
    counters: dict,
    _path: str = "",
) -> dict:
    """Deep-merge override into a copy of base. Lists fully replaced.
    Logs each change at DEBUG (key path only — never values).
    `counters` is mutated with {'added','replaced','type_mismatched'}.

    Single-level shallow copy at each recursion. Keys present only in `base`
    keep their original reference. Plugins must not mutate `self.arguments`
    in place — consistent with the existing contract.

    A dict containing `__replace__: true` is treated as a wholesale-replace
    directive: the rest of that dict (with the marker stripped) becomes the
    value at this position, bypassing deep merge. Use it to clear a subtree
    (`{__replace__: true}` -> `{}`) or replace it (`{__replace__: true, k: v}` -> `{k: v}`).
    """
    if override.get("__replace__") is True:
        replacement = {k: v for k, v in override.items() if k != "__replace__"}
        counters["replaced"] += 1
        logger.debug(
            f"Plugin '{plugin_name}': arg subtree replaced '{_path or '<root>'}'"
        )
        return replacement

    out = dict(base)
    for key, ov in override.items():
        path = f"{_path}.{key}" if _path else key
        if key not in out:
            out[key] = ov
            counters["added"] += 1
            logger.debug(f"Plugin '{plugin_name}': arg added '{path}'")
        elif isinstance(out[key], dict) and isinstance(ov, dict):
            out[key] = _deep_merge_args(
                out[key], ov, plugin_name, logger, counters, path
            )
        elif type(out[key]) == type(ov) and out[key] == ov:
            # No-op: same type AND same value. Type guard prevents `True == 1`
            # (bool vs int) from being treated as no-op — that pair must reach
            # the type-mismatch branch below.
            pass
        else:
            # Base-was-None is NOT a mismatch — overriding a previously-null
            # key is normal. Everything else with a type change warns.
            if out[key] is not None and type(out[key]) != type(ov):
                counters["type_mismatched"] += 1
                logger.warning(
                    f"Plugin '{plugin_name}': arg '{path}' type mismatch "
                    f"({type(out[key]).__name__} -> "
                    f"{type(ov).__name__ if ov is not None else 'NoneType'}); override applied"
                )
            else:
                counters["replaced"] += 1
                logger.debug(f"Plugin '{plugin_name}': arg replaced '{path}'")
            out[key] = ov
    return out


def _normalize_hosts(
    value: Any,
    *,
    param_name: str = "hosts",
    default: Optional[Union[str, List[str]]],
) -> Optional[Union[str, List[str]]]:
    """Normalize a hosts/blocked_hosts value into canonical form.

    Returns canonical value (str, list, or None). Raises ValueError on
    structural errors. Caller is responsible for passing normalized values
    to find_endpoint and to _warn_redundant_host_combos.
    """
    if value is None:
        return default

    if isinstance(value, str):
        if not value.strip():
            raise ValueError(f"{param_name}: empty string not allowed")
        return value

    if isinstance(value, list):
        if not value:
            raise ValueError(f"{param_name}: empty list not allowed")
        for item in value:
            if not isinstance(item, str):
                raise ValueError(
                    f"{param_name}: list entries must be str, "
                    f"got {type(item).__name__}"
                )
            if not item.strip():
                raise ValueError(f"{param_name}: empty string in list not allowed")

        # Dedup first (preserves first-occurrence order).
        seen = set()
        deduped = []
        for item in value:
            if item not in seen:
                seen.add(item)
                deduped.append(item)

        # Single-element list collapses to bare string.
        if len(deduped) == 1:
            return deduped[0]

        # Keyword-in-list guard runs against the deduped list — duplicate
        # keywords like ["remote", "remote"] collapse first and never reach
        # this guard.
        for keyword in ("any", "remote"):
            if keyword in deduped:
                raise ValueError(
                    f"{param_name}: keyword '{keyword}' cannot appear in a "
                    f"list with other elements (it already covers them)"
                )
        return deduped

    raise ValueError(
        f"{param_name}: must be str, list[str], or None, " f"got {type(value).__name__}"
    )


def _warn_redundant_host_combos(hosts, blocked_hosts, logger) -> None:
    """Warn on hosts/blocked_hosts combinations that simplify to a single
    keyword. Run AFTER both values are normalized.
    """
    if hosts != "any":
        return
    block_set = (
        {blocked_hosts} if isinstance(blocked_hosts, str) else set(blocked_hosts or [])
    )
    if "local" in block_set:
        logger.warning(
            "hosts='any' + blocked_hosts contains 'local' — simpler form is "
            "hosts='remote'."
        )
    if "remote" in block_set:
        logger.warning(
            "hosts='any' + blocked_hosts contains 'remote' — simpler form is "
            "hosts='local'."
        )


class PluginCore:
    """Manages all plugins and facilitates communication between them."""

    def __init__(self, config_path: str):
        self.config_path = config_path

        # Pre-read config for logger bootstrap so the filter system has its
        # thresholds in place from the very first record. quickget_config
        # handles integrity failures; the try/except handles file-missing /
        # YAML parse errors — neither emits a log line at this point because
        # no logger exists yet, but load_config_yaml below will surface a
        # useful error if the YAML is genuinely broken.
        try:
            bootstrap_cfg = (
                ConfigUtil.quickget_config(config_path, fallback_value={}) or {}
            )
        except Exception:
            bootstrap_cfg = {}
        if not isinstance(bootstrap_cfg, dict):
            bootstrap_cfg = {}
        bootstrap_general = bootstrap_cfg.get("general", {})
        if not isinstance(bootstrap_general, dict):
            bootstrap_general = {}
        self._logger = LogUtil.create(
            log_level=bootstrap_general.get("console_log_level", "DEBUG"),
            file_level=bootstrap_general.get("file_log_level", "DEBUG"),
            logger_levels=bootstrap_general.get("logger_levels", {}),
        )

        self.yaml_config = None
        self.load_config_yaml(self.config_path)
        # change_level / change_file_level / apply_logger_levels_config now
        # live inside load_config_yaml so hot-reload picks up changes too.

        self.requests = {}
        self.request_lock = asyncio.Lock()
        self.task_list = []

        self.main_event_loop = None
        self.plugins = {}
        self.plugins_by_uuid = {}
        self.plugin_lock = asyncio.Lock()
        # Dedicated thread pool for sync plugin endpoints — isolated from
        # Python's default executor to prevent deadlock under load.
        # See README "Sync vs Async Plugins: Thread Pool Deadlock Risk".
        self._plugin_executor = ThreadPoolExecutor(
            max_workers=32,
            thread_name_prefix="plugin",
        )
        self._init_tasks = []
        self._running_loop_task = None
        self.network = None
        self.topic_registry = TopicRegistry(self._logger.getChild("notifier"))
        self._config_write_lock = threading.Lock()

    async def wait_until_ready(self):
        """Ensure initialization tasks are started and await their completion."""
        # Ensure event loop and maintenance task
        if self.main_event_loop is None:
            self.main_event_loop = asyncio.get_running_loop()
            if self.yaml_config.get("general", {}).get("asyncio_debug", False):
                self.main_event_loop.set_debug(True)
                self.main_event_loop.slow_callback_duration = 0.5
        if self._running_loop_task is None:
            self._running_loop_task = asyncio.create_task(self.running_loop())

        # If no init tasks yet, create them (backward-compat with older call sites)
        if not self._init_tasks:
            self._init_tasks.append(asyncio.create_task(self.load_plugins()))

            if getattr(self, "networking_enabled", False):
                if self.network is None:
                    self.network = NetworkManager(
                        self,
                        self._logger.getChild("networking"),
                        node_ips=self.yaml_config.get("networking").get("node_ips", []),
                        discover_nodes=self.yaml_config.get("networking").get(
                            "discover_nodes", False
                        ),
                        direct_discoverable=self.networking_direct_discoverable,
                        auto_discoverable=self.networking_auto_discoverable,
                        port=self.networking_port,
                        secret=getattr(self, "networking_secret", None),
                        cert_file=getattr(self, "networking_cert_file", None),
                        key_file=getattr(self, "networking_key_file", None),
                        pool_size=getattr(self, "networking_pool_size", 5),
                    )
                self._init_tasks.append(asyncio.create_task(self.network.start()))

        if self._init_tasks:
            await asyncio.gather(*self._init_tasks)

        # NOTE: Wait for enabled?

    async def start(self):
        """Initialize background tasks, load plugins, and start networking."""
        self.main_event_loop = asyncio.get_running_loop()
        if self._running_loop_task is None:
            self._running_loop_task = asyncio.create_task(self.running_loop())

        self._init_tasks = [asyncio.create_task(self.load_plugins())]

        if getattr(self, "networking_enabled", False):
            self.network = NetworkManager(
                self,
                self._logger.getChild("networking"),
                node_ips=self.yaml_config.get("networking").get("node_ips", []),
                discover_nodes=self.yaml_config.get("networking").get(
                    "discover_nodes", False
                ),
                direct_discoverable=self.networking_direct_discoverable,
                auto_discoverable=self.networking_auto_discoverable,
                port=self.networking_port,
                secret=getattr(self, "networking_secret", None),
                cert_file=getattr(self, "networking_cert_file", None),
                key_file=getattr(self, "networking_key_file", None),
                pool_size=getattr(self, "networking_pool_size", 5),
            )
            self._init_tasks.append(asyncio.create_task(self.network.start()))

        await self.wait_until_ready()

    async def close(self):
        """Gracefully shutdown: drain requests, disable plugins in reverse order, stop networking."""
        # 1. Stop maintenance loop (no more cleanup cycles)
        self._logger.info("Shutdown: stopping maintenance loop...")
        if self._running_loop_task is not None:
            self._running_loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._running_loop_task
            self._running_loop_task = None

        # 2. Wait for all in-flight request tasks to finish (up to 30s)
        pending = [t for t in self.task_list if not t.done()]
        if pending:
            self._logger.info(
                "Shutdown: waiting for %d in-flight request(s)...", len(pending)
            )
            done, still_pending = await asyncio.wait(pending, timeout=30)
            if still_pending:
                self._logger.warning(
                    "Shutdown: %d request(s) still running after 30s, cancelling...",
                    len(still_pending),
                )
                for t in still_pending:
                    t.cancel()
                await asyncio.gather(*still_pending, return_exceptions=True)
        self.task_list = []

        # 3. Disable plugins in REVERSE config order
        #    Reverse order ensures dependents shut down before their dependencies.
        #    e.g. Discord_Bot_Plugin → DataCollection → PostgreSQL
        plugin_names = list(self.plugins.keys())
        plugin_names.reverse()

        for name in plugin_names:
            plugin = self.plugins.get(name)
            if not plugin or not plugin.enabled:
                continue
            self._logger.info("Shutdown: disabling %s...", name)
            try:
                async with self.plugin_lock:
                    if asyncio.iscoroutinefunction(plugin.on_disable):
                        await asyncio.wait_for(plugin.on_disable(), timeout=30)
                    else:
                        await asyncio.wait_for(
                            self.main_event_loop.run_in_executor(
                                self._plugin_executor, plugin.on_disable
                            ),
                            timeout=30,
                        )
                    plugin.enabled = False
                self._logger.info("Shutdown: %s disabled", name)
            except asyncio.TimeoutError:
                self._logger.warning(
                    "Shutdown: %s on_disable timed out after 30s", name
                )
                plugin.enabled = False
            except Exception as e:
                self._logger.error("Shutdown: %s on_disable failed: %s", name, e)
                plugin.enabled = False

        # Sweep any plugin-source per-logger thresholds. Covers never-enabled
        # plugins (the disable loop above skips them via the `enabled` guard)
        # and is idempotent for plugins already cleaned via pop_plugin.
        for sweep_name, sweep_plugin in list(self.plugins.items()):
            sweep_uuid = getattr(sweep_plugin, "plugin_uuid", None)
            if sweep_uuid:
                LogUtil.clear_logger_levels_owned_by(sweep_name, sweep_uuid)

        # 4. Stop networking
        if hasattr(self, "network") and self.network is not None:
            stop = getattr(self.network, "stop", None)
            if callable(stop):
                self._logger.info("Shutdown: stopping networking...")
                with contextlib.suppress(Exception):
                    await stop()

        # 5. Shutdown dedicated plugin executor
        if hasattr(self, "_plugin_executor") and self._plugin_executor:
            self._plugin_executor.shutdown(wait=False)

        self._logger.info("Shutdown complete")

    @log_errors
    def load_config_yaml(self, config_path: str):
        self._logger.info(f"Loading config from config_path: {config_path}")
        self.yaml_config: dict = ConfigUtil.load_config(config_path)
        self._logger.info(self.yaml_config)

        ConfigUtil.check_config_integrity(self.yaml_config, self._logger)

        ConfigUtil.apply_configvalues(self)

        # Apply logging-related config last so hot-reload picks up changes.
        # On first boot LogUtil.create() already used the same values from the
        # bootstrap pre-read; on async_load_config_yaml() this is the only
        # place that re-applies them.
        general = self.yaml_config.get("general", {})
        if not isinstance(general, dict):
            general = {}
        LogUtil.change_level(general.get("console_log_level", "DEBUG"))
        LogUtil.change_file_level(general.get("file_log_level", "DEBUG"))
        LogUtil.apply_logger_levels_config(general.get("logger_levels", {}))

    async def async_load_config_yaml(self, config_path: str):
        """Async wrapper around load_config_yaml for use outside __init__."""
        self.load_config_yaml(config_path)

    # ── Per-logger threshold API (delegates to LogUtil) ─────────────
    # Plugins should call self.set_logger_level / self.clear_logger_level /
    # self.list_logger_levels via the Plugin base helpers — those auto-fill
    # the owner tuple. These wrappers exist so the helpers have something to
    # delegate to and so admin/CLI code can pass owner explicitly if needed.

    def set_logger_level(
        self,
        name: str,
        *,
        console: Optional[str] = None,
        file: Optional[str] = None,
        plugin_name: str,
        plugin_uuid: str,
    ) -> None:
        LogUtil.set_logger_level(
            name, console=console, file=file, owner=(plugin_name, plugin_uuid)
        )

    def clear_logger_level(
        self,
        name: str,
        *,
        console: bool = True,
        file: bool = True,
        plugin_name: Optional[str] = None,
        plugin_uuid: Optional[str] = None,
    ) -> None:
        if plugin_name is None or plugin_uuid is None:
            owner = None
        else:
            owner = (plugin_name, plugin_uuid)
        LogUtil.clear_logger_level(name, console=console, file=file, owner=owner)

    def list_logger_levels(self) -> dict:
        return LogUtil.list_logger_levels()

    # ── Config file editing ──────────────────────────────────────────

    @log_errors
    def list_config_files(self) -> Dict[str, str]:
        """Return {label: absolute_path} for main config and all plugin configs."""
        files = {}
        main_config = os.path.abspath(self.config_path)
        files["config.yml (main)"] = main_config

        for entry in self.yaml_config.get("plugins", []):
            name = entry.get("name", "")
            path = entry.get("path") or os.path.join(self.plugin_package, name)
            cfg = os.path.join(os.path.abspath(path), "plugin_config.yml")
            if os.path.isfile(cfg):
                files[f"{name}/plugin_config.yml"] = cfg

        return files

    @log_errors
    def read_config_file(self, path: str) -> str:
        """Read and return raw content of a known config file.

        Args:
            path: Absolute path to the config file.

        Returns:
            File content as string.

        Raises:
            FileNotFoundError: If path doesn't exist.
            ValueError: If path not in list_config_files().
        """
        abs_path = os.path.abspath(path)
        allowed = set(self.list_config_files().values())
        if abs_path not in allowed:
            raise ValueError(f"Path not in known config files: {path}")

        with open(abs_path, "r", encoding="utf-8") as f:
            return f.read()

    @log_errors
    def save_config_file(self, path: str, content: str, backup: bool = True) -> None:
        """Validate YAML, create .bak backup, and write content.

        Does NOT re-apply main config — call load_config_yaml() explicitly
        if you need settings to take effect immediately.

        Args:
            path: Absolute path to the config file.
            content: New YAML content to write.
            backup: Whether to create a .yml.bak before overwriting.

        Raises:
            ValueError: If path not in known config files or content parses to empty.
            yaml.YAMLError: If content is invalid YAML.
        """
        abs_path = os.path.abspath(path)
        allowed = set(self.list_config_files().values())
        if abs_path not in allowed:
            raise ValueError(f"Path not in known config files: {path}")

        parsed = yaml.safe_load(content)
        if not parsed:
            raise ValueError("Config content is empty or null after parsing")

        with self._config_write_lock:
            if backup and os.path.exists(abs_path):
                with open(abs_path, "r", encoding="utf-8") as f:
                    old_content = f.read()
                bak_path = abs_path + ".bak"
                with open(bak_path, "w", encoding="utf-8") as f:
                    f.write(old_content)

            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)

    def is_main_config(self, path: str) -> bool:
        """Check if path points to the main config.yml."""
        return os.path.abspath(path) == os.path.abspath(self.config_path)

    @async_log_errors
    async def load_plugins(self):
        # Load the plugins
        await self.get_plugins()

        # Enable them
        await self.start_plugins()

        self._logger.info(f"Finished Loading plugins!")

    @async_log_errors
    async def get_plugins(self) -> None:

        for plugin_entry in self.yaml_config.get("plugins", []):

            # Load and initiate the pluginclass
            await self.load_plugin_with_conf(plugin_entry)

    @async_log_errors
    async def start_plugins(self) -> None:
        """Start all plugin loops."""
        tasks = []
        task_plugins = []
        for plugin in self.plugins.values():
            if not plugin.enabled:
                tasks.append(self._enable_plugin(plugin.plugin_name))
                task_plugins.append(plugin)

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for plugin, result in zip(task_plugins, results):
                if isinstance(result, Exception):
                    self._logger.warning(
                        f'Error occured while enabling plugin with name "{plugin.plugin_name}": {type(result).__name__}: {result}'
                    )
                # task = asyncio.create_task(self._enable_plugin(plugin.plugin_name))
                # self.task_list.append(task)

    @async_log_errors
    async def load_plugin_with_conf(self, plugin_entry: list) -> None:

        async def error_config(message):
            self._logger.error(f"Plugin '{name}': {message}")
            await self.pop_plugin(name)

        async def warn_config(message):
            self._logger.warning(f"Plugin '{name}': {message}")

        # Get values
        name = plugin_entry["name"]

        if not plugin_entry.get("enabled"):
            self._logger.debug(
                f'Plugin "{name}" wont be loaded due to it being disabled'
            )
            await self.pop_plugin(name)
            return

        if name in list(self.plugins.keys()):
            self._logger.info(
                f'Plugin "{name}" has an old instance, that will be overwritten'
            )
            await self.pop_plugin(name)

        # Resolve plugin directory
        path = plugin_entry.get("path") or os.path.join(self.plugin_package, name)
        path = os.path.abspath(path)
        if not os.path.exists(path):
            await error_config(f"Plugin directory missing: {name} ({path})")
            return

        # Load plugin config
        try:
            with open(
                os.path.join(path, "plugin_config.yml"), "r", encoding="utf-8"
            ) as f:
                plugin_config = yaml.safe_load(f)
        except Exception as e:
            await error_config(f"Failed loading config for {name}: {e}")
            return

        # Validate plugin config
        for field in ["description", "version", "remote", "arguments", "endpoints"]:
            if field not in plugin_config:
                await warn_config(f"{name} missing {field} in plugin_config.yml")

        # Validate endpoints config
        for endpoint in (
            plugin_config.get("endpoints")
            if isinstance(plugin_config.get("endpoints"), list)
            else []
        ):
            for field in [
                "internal_name",
                "access_name",
                "remote",
                "accessible_by_other_plugins",
            ]:
                if field not in endpoint:
                    await error_config(
                        f"{endpoint} is missing {field} in plugin_config.yml"
                    )
                    return

            for check in [
                ("internal_name", str, True, True),
                ("access_name", str, True, True),
                ("remote", bool, False, False),
                ("accessible_by_other_plugins", bool, False, False),
            ]:  # ({config_option}, {type}, {empty_allowed}, {check_ascii})

                if type(endpoint[check[0]]) != check[1]:
                    await error_config(
                        f"{endpoint}: {check[0]} has wrong type {type(endpoint[check[0]])} in plugin_config.yml as it must be a {check[1]}"
                    )
                    return

                if check[2] and not endpoint[check[0]].strip():
                    await error_config(
                        f"{endpoint}: {check[0]} is empty in plugin_config.yml"
                    )
                    return

                if check[3] and not endpoint[check[0]].isascii():
                    await error_config(
                        f"{endpoint}: {check[0]} contains non ascii chars in plugin_config.yml"
                    )
                    return

        # ── Argument override application ────────────────────────────────
        # Base args from plugin_config.yml. Must be dict-or-null.
        base_args = plugin_config.get("arguments")
        if base_args is not None and not isinstance(base_args, dict):
            await error_config(
                f"top-level 'arguments' in plugin_config.yml must be a mapping (dict) "
                f"or omitted; got {type(base_args).__name__}"
            )
            return

        # Override from main config.yml plugin entry. Must be dict-or-missing.
        # Asymmetry intentional: invalid plugin_config is a hard error (plugin
        # author bug). Invalid main-config override is a warning that ignores
        # the override (deployment misconfig — let other plugins still load).
        # Order matters: type-check BEFORE emptiness short-circuit, so wrong
        # falsy types ([], "", 0, False) still warn instead of being silently dropped.
        override = plugin_entry.get("arguments")
        if override is None:
            merged_args = base_args
        elif not isinstance(override, dict):
            await warn_config(
                f"main config 'arguments' for '{name}' must be a mapping; "
                f"got {type(override).__name__}; ignoring overrides"
            )
            merged_args = base_args
        elif not override:
            merged_args = base_args
        else:
            counters = {"added": 0, "replaced": 0, "type_mismatched": 0}
            merged_args = _deep_merge_args(
                base_args or {},
                override,
                name,
                self._logger,
                counters,
            )
            total = (
                counters["added"] + counters["replaced"] + counters["type_mismatched"]
            )
            if total:
                self._logger.info(
                    f"Plugin '{name}': applied {total} override(s) "
                    f"({counters['added']} added, "
                    f"{counters['replaced']} replaced, "
                    f"{counters['type_mismatched']} type-mismatched)"
                )

        # Dynamic import
        module_path = os.path.join(path, "plugin.py")
        spec = importlib.util.spec_from_file_location(name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # Find first Plugin subclass
        plugin_class = next(
            (
                cls
                for _, cls in inspect.getmembers(module, inspect.isclass)
                if issubclass(cls, Plugin) and cls != Plugin
            ),
            None,
        )
        if plugin_class is None:
            await error_config(f"No Plugin subclass found in {module_path}")
            return

        # Instantiate with merged arguments
        plugin = plugin_class(
            self._logger.getChild(name),
            self,
            arguments=merged_args,
        )

        plugin.plugin_name = name
        plugin.version = plugin_config.get("version") or "0.0.0 - not given"
        plugin.remote = plugin_config.get("remote") or False
        plugin.arguments = merged_args
        endpoints_cfg = plugin_config.get("endpoints") or []
        if not isinstance(endpoints_cfg, list):
            await warn_config(f"endpoints must be a list in plugin_config.yml")
            endpoints_cfg = []
        plugin.endpoints = [e for e in endpoints_cfg if isinstance(e, dict)]
        try:
            plugin._endpoint_by_access = {
                e.get("access_name"): e
                for e in plugin.endpoints
                if e.get("access_name")
            }
        except Exception:
            plugin._endpoint_by_access = {}

        async with self.plugin_lock:
            self.plugins[name] = plugin
            # Maintain uuid index if available
            plugin_uuid = getattr(plugin, "plugin_uuid", None)
            if plugin_uuid:
                self.plugins_by_uuid[plugin_uuid] = plugin

        # Register config-driven topic subscriptions
        for endpoint in plugin.endpoints:
            topic = endpoint.get("topic")
            if topic and isinstance(topic, str) and topic.strip():
                await self.topic_registry.subscribe(
                    topic_pattern=topic.strip(),
                    plugin_name=name,
                    plugin_uuid=plugin.plugin_uuid,
                    endpoint_access_name=endpoint.get("access_name"),
                    config_driven=True,
                )

        self._logger.info(
            f"Successfully loaded plugin: {name} (Version: {plugin_config['version']}, Path: {path})"
        )

    @async_log_errors
    async def pop_plugin(self, plugin_name: str) -> None:
        self._logger.info(f"Popping plugin: {plugin_name}")
        try:
            if plugin_name in list(self.plugins.keys()):
                # Resolve any pending requests targeting this plugin
                async with self.request_lock:
                    for req in self.requests.values():
                        if req.target_plugin == plugin_name and not req._future.done():
                            await req.set_result(
                                f"Plugin {plugin_name} was unloaded while request was pending",
                                error=True,
                            )

                if self.plugins[plugin_name].enabled:
                    await self._disable_plugin(plugin_name)
                async with self.plugin_lock:
                    plugin = self.plugins.pop(plugin_name)
                    # Remove from uuid index
                    plugin_uuid = getattr(plugin, "plugin_uuid", None)
                    if plugin_uuid and plugin_uuid in self.plugins_by_uuid:
                        self.plugins_by_uuid.pop(plugin_uuid, None)
                    # Remove all topic subscriptions for this plugin
                    if plugin_uuid:
                        await self.topic_registry.unsubscribe_plugin(plugin_uuid)
                        LogUtil.clear_logger_levels_owned_by(plugin_name, plugin_uuid)
            else:
                self._logger.warning(f'Plugin with name "{plugin_name}" doesnt exist')
        except Exception as error:
            raise Exception(f'Error while popping plugin "{plugin_name}": {error}')

    @async_log_errors
    async def purge_plugins(self):
        self._logger.info("Purging plugins")
        try:
            for plugin_name in list(self.plugins.keys()):
                await self._disable_plugin(plugin_name)
            async with self.plugin_lock:
                for plugin_name, plugin in self.plugins.items():
                    plugin_uuid = getattr(plugin, "plugin_uuid", None)
                    if plugin_uuid:
                        await self.topic_registry.unsubscribe_plugin(plugin_uuid)
                        LogUtil.clear_logger_levels_owned_by(plugin_name, plugin_uuid)
                self.plugins.clear()
                self.plugins_by_uuid.clear()
            self._logger.info("Purged all plugins")
        except Exception as error:
            raise Exception(f"Error while purging plugins: {error}")

    @async_log_errors
    async def purge_plugins_except(self, excluded_names: List[str]):
        """Purge all plugins except those in the excluded_names list."""
        self._logger.info(f"Purging plugins except: {excluded_names}")
        try:
            plugins_to_purge = [
                name for name in list(self.plugins.keys()) if name not in excluded_names
            ]
            for plugin_name in plugins_to_purge:
                await self._disable_plugin(plugin_name)
            async with self.plugin_lock:
                for plugin_name in plugins_to_purge:
                    plugin = self.plugins.pop(plugin_name, None)
                    if plugin:
                        plugin_uuid = getattr(plugin, "plugin_uuid", None)
                        if plugin_uuid:
                            if plugin_uuid in self.plugins_by_uuid:
                                self.plugins_by_uuid.pop(plugin_uuid, None)
                            await self.topic_registry.unsubscribe_plugin(plugin_uuid)
                            LogUtil.clear_logger_levels_owned_by(
                                plugin_name, plugin_uuid
                            )
            self._logger.info(
                f"Purged {len(plugins_to_purge)} plugins, kept {len(excluded_names)}"
            )
        except Exception as error:
            raise Exception(f"Error while purging plugins: {error}")

    @async_log_errors
    async def get_plugin_info(self, plugin_name: str) -> Optional[Dict[str, Any]]:
        """Get structured information about a plugin."""
        async with self.plugin_lock:
            plugin = self.plugins.get(plugin_name)
            if not plugin:
                return None

            return {
                "name": plugin.plugin_name,
                "version": getattr(plugin, "version", "unknown"),
                "uuid": plugin.plugin_uuid,
                "enabled": plugin.enabled,
                "remote": getattr(plugin, "remote", False),
                "description": getattr(plugin, "description", "No description"),
                "arguments": getattr(plugin, "arguments", None),
            }

    @async_log_errors
    async def get_plugin_endpoints(
        self, plugin_name: str
    ) -> Optional[List[Dict[str, Any]]]:
        """Get all endpoints for a plugin."""
        async with self.plugin_lock:
            plugin = self.plugins.get(plugin_name)
            if not plugin:
                return None

            endpoints = getattr(plugin, "endpoints", [])
            if not isinstance(endpoints, list):
                return []

            return [
                {
                    "access_name": ep.get("access_name", ""),
                    "internal_name": ep.get("internal_name", ""),
                    "remote": ep.get("remote", False),
                    "accessible_by_other_plugins": ep.get(
                        "accessible_by_other_plugins", False
                    ),
                    "description": ep.get("description", ""),
                    "tags": ep.get("tags", []),
                }
                for ep in endpoints
                if isinstance(ep, dict)
            ]

    @async_log_errors
    async def list_plugins_state(self) -> List[Dict[str, Any]]:
        """Return list of all loaded plugins with name, enabled, and description."""
        async with self.plugin_lock:
            return [
                {
                    "name": p.plugin_name,
                    "enabled": p.enabled,
                    "description": getattr(p, "description", "No description"),
                }
                for p in self.plugins.values()
            ]

    @async_log_errors
    async def graceful_shutdown(self):
        """Gracefully shutdown the system by closing PluginCore."""
        self._logger.info("Initiating graceful shutdown...")
        await self.close()
        # Note: Stopping the event loop should be handled by the main application

    @async_handle_errors(None)
    async def _enable_plugin(self, plugin_name: str):
        """Method to enable a plugin."""
        async with self.plugin_lock:
            plugin = self.plugins[plugin_name]
            if not plugin.enabled:
                if asyncio.iscoroutinefunction(plugin.on_enable):
                    await plugin.on_enable()
                else:
                    await self.main_event_loop.run_in_executor(
                        self._plugin_executor, plugin.on_enable
                    )
                plugin.enabled = True

    @async_log_errors
    async def _disable_plugin(self, plugin_name: str):
        """Disable a plugin by calling its on_disable and setting enabled=False."""
        async with self.plugin_lock:
            plugin = self.plugins[plugin_name]
            if plugin.enabled:
                if asyncio.iscoroutinefunction(plugin.on_disable):
                    await plugin.on_disable()
                else:
                    await self.main_event_loop.run_in_executor(
                        self._plugin_executor, plugin.on_disable
                    )
                plugin.enabled = False

    @async_handle_errors(None)
    async def _reload_plugin(self, plugin_name: str):
        """Reload a plugin by disabling, removing, re-loading from config, and re-enabling."""
        # Capture whether it was enabled before reload
        previously_enabled = False
        if plugin_name in self.plugins:
            previously_enabled = self.plugins[plugin_name].enabled

        await self.pop_plugin(plugin_name)

        entry = next(
            (
                p
                for p in self.yaml_config.get("plugins", [])
                if p.get("name") == plugin_name
            ),
            None,
        )
        if not entry:
            raise Exception(f"Plugin '{plugin_name}' not found in config for reload")

        await self.load_plugin_with_conf(entry)
        if previously_enabled:
            await self._enable_plugin(plugin_name)

    @contextlib.asynccontextmanager
    async def request_context_async(self, request: Request):
        """Async context manager to handle requests."""
        try:
            result, error, timed_out = await request.wait_for_result_async()
            if error:
                raise Exception(f"Request {request.id} failed: {request.result}")
            yield result
        finally:
            await request.set_collected()

    @contextlib.contextmanager
    def request_context_sync(self, request: Request):
        """Sync context manager to handle requests."""
        try:
            result = request.get_result_sync()
            if request.error:
                raise Exception(f"Request failed: {request.result}")
            yield result
        finally:
            asyncio.run_coroutine_threadsafe(
                request.set_collected(), self.main_event_loop
            )

    @async_log_errors
    async def create_request(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Request:
        """Create a new request asynchronously."""

        if author_host == None:
            author_host = self.hostname

        request = Request(
            author_host,
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            request_id,
            self.main_event_loop,
        )

        async with self.request_lock:
            self.requests[request.id] = request

        self._logger.debug(
            f"Request {request.id} created by {author} targeting {plugin}.{method}"
        )

        task = asyncio.create_task(self._process_request(request))
        self.task_list.append(task)

        return request

    @log_errors
    def create_request_sync(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Request:
        """Create a new request synchronously."""
        coro = self.create_request(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            author_host,
            request_id,
        )
        future = asyncio.run_coroutine_threadsafe(coro, self.main_event_loop)
        return future.result()

    @async_log_errors
    async def create_gen_request(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> GeneratorRequest:
        """Create a new request asynchronously."""

        if author_host == None:
            author_host = self.hostname

        request = GeneratorRequest(
            author_host,
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            request_id,
            self.main_event_loop,
        )

        async with self.request_lock:
            self.requests[request.id] = request

        self._logger.debug(
            f"GeneratorRequest {request.id} created by {author} targeting {plugin}.{method}"
        )

        task = asyncio.create_task(self._process_request_stream(request))
        self.task_list.append(task)

        return request

    @log_errors
    def create_gen_request_sync(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Request:
        """Create a new request synchronously."""
        coro = self.create_gen_request(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            author_host,
            request_id,
        )
        future = asyncio.run_coroutine_threadsafe(coro, self.main_event_loop)
        return future.result()

    @async_log_errors
    async def find_endpoints_by_tag(self, tag: str) -> Optional[List[Dict[str, Any]]]:
        """
        Finds all endpoints by a tag.

        Args:
            tag: The tag to search for

        Returns:
            List of endpoints
            For local: (Plugin, endpoint, endpoint_description, endpoint_arguments)
            For remote: (RemotePlugin, endpoint, endpoint_description, endpoint_arguments)
        """
        endpoints = []
        for plugin in self.plugins.values():
            plugin: Plugin
            if plugin.enabled:
                for endpoint in plugin.endpoints:
                    if tag in (endpoint.get("tags") or []):
                        endpoints.append(
                            (
                                plugin,
                                endpoint,
                                endpoint.get("description"),
                                endpoint.get("arguments"),
                            )
                        )

        if self.networking_enabled:
            for node in self.network.nodes:
                node: Node
                if node.enabled:
                    result = await self.network.node_get_tagged_endpoints(node.IP, tag)
                    if result:
                        endpoints.extend(result)
        return endpoints

    @async_log_errors
    async def find_endpoint(
        self,
        access_name: str,
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        plugin_uuid: Optional[str] = None,
        requester_id: Optional[str] = None,
        target_plugin: Optional[str] = None,
    ) -> Optional[tuple[Union[Plugin, RemotePlugin], dict, Optional[Node]]]:
        """
        Finds a plugin endpoint locally or on remote nodes with access control.

        Args:
            access_name: The access_name of the endpoint to find
            hosts: Target hosts — "local", "remote", "any", a hostname, or a
                list of hostnames (whitelist). Caller is expected to have
                already passed this through _validate_host_args.
            blocked_hosts: Hosts to exclude — same shape as `hosts`, or None
                for no blocking.
            plugin_uuid: Specific plugin UUID to search for (optional)
            requester_id: UUID of the plugin making the request (for access control)
            target_plugin: Optional plugin name filter

        Returns:
            Tuple of (plugin, endpoint_dict, node) or None if not found
            For local: (Plugin, endpoint_dict, None)
            For remote: (RemotePlugin, endpoint_dict, Node)
        """

        self._logger.debug(
            f"Finding endpoint: access_name='{access_name}', hosts={hosts!r}, "
            f"blocked_hosts={blocked_hosts!r}, plugin_uuid={plugin_uuid}, "
            f"requester_id={requester_id}, target_plugin={target_plugin}"
        )

        # Determine request provenance
        # This is used to determine if a REMOTE NODE is checking OUR endpoints
        # (handled by _handle_has_endpoint on the remote node)
        is_local_system = requester_id == self.hostname
        is_local_plugin = requester_id in self.plugins_by_uuid
        is_remote_request = not (is_local_system or is_local_plugin)

        if is_remote_request:
            self._logger.debug(
                f"Remote request detected from requester_id: {requester_id}"
            )

        def _matches_local():
            if isinstance(hosts, str):
                return hosts in ("local", "any", self.hostname)
            if isinstance(hosts, list):
                return "local" in hosts or self.hostname in hosts
            return False

        def _is_local_blocked():
            if blocked_hosts is None:
                return False
            if isinstance(blocked_hosts, str):
                return blocked_hosts in ("local", "any", self.hostname)
            if isinstance(blocked_hosts, list):
                return "local" in blocked_hosts or self.hostname in blocked_hosts
            return False

        # Check locally first (only if host includes local)
        # Note: Remote accessibility checks for OUR endpoints should only happen
        # when a remote node is querying us (is_remote_request=True).
        # When WE are searching for remote endpoints, we don't check remote accessibility here.
        if _matches_local() and not _is_local_blocked():
            for plugin in self.plugins.values():
                # Skip if plugin doesn't match UUID filter
                if plugin_uuid and plugin.plugin_uuid != plugin_uuid:
                    continue

                if target_plugin and plugin.plugin_name != target_plugin:
                    continue

                # Check if plugin is enabled and has endpoints
                if not (plugin.enabled and hasattr(plugin, "endpoints")):
                    continue

                # Fast-path by access_name
                endpoint = getattr(plugin, "_endpoint_by_access", {}).get(access_name)
                if endpoint:
                    # Check access based on request type
                    if is_remote_request:
                        # A remote node is checking OUR endpoints - check remote accessibility
                        # This is the ONLY place where we check remote accessibility
                        if not getattr(plugin, "remote", False):
                            continue
                        if not endpoint.get("remote", False):
                            continue
                        return plugin, endpoint, None
                    else:
                        # Local request: check accessible_by_other_plugins flag
                        if (
                            not endpoint.get("accessible_by_other_plugins", False)
                            and plugin.plugin_uuid != requester_id
                        ):
                            pass
                        else:
                            return plugin, endpoint, None

        def _matches_remote_node(node_hostname):
            if isinstance(hosts, str):
                return hosts in ("remote", "any") or hosts == node_hostname
            if isinstance(hosts, list):
                return node_hostname in hosts
            return False

        def _is_remote_node_blocked(node_hostname):
            if blocked_hosts is None:
                return False
            if isinstance(blocked_hosts, str):
                return (
                    blocked_hosts in ("remote", "any") or blocked_hosts == node_hostname
                )
            if isinstance(blocked_hosts, list):
                return node_hostname in blocked_hosts
            return False

        def _other_than_local():
            if isinstance(hosts, str):
                return not hosts in ("local", self.hostname)
            if isinstance(hosts, list):
                return (
                    len(hosts) - hosts.count("local") - hosts.count(self.hostname)
                ) > 0
            return True

        # Check remote nodes if networking is enabled
        if getattr(self, "networking_enabled", False) and _other_than_local():

            for node in self.network.nodes:
                if not (node.enabled and await node.is_alive()):
                    continue

                if not _matches_remote_node(node.hostname) or _is_remote_node_blocked(
                    node.hostname
                ):
                    continue

                # Check remote node for endpoint
                result = await self.network.node_has_endpoint(
                    node.IP,
                    access_name,
                    plugin_uuid if plugin_uuid != "remote" else None,
                    requester_id,
                    target_plugin,
                )

                if result and result.get("available", False):
                    # Create RemotePlugin from response data
                    plugin_info = result.get("plugin_info", {})
                    endpoint_info = result.get("endpoint", {})

                    remote_plugin = RemotePlugin(
                        name=plugin_info.get("name", "unknown"),
                        version=plugin_info.get("version", "unknown"),
                        uuid=plugin_info.get("uuid"),
                        enabled=True,
                        remote=True,
                        description=plugin_info.get("description", "Remote plugin"),
                        arguments=[],
                        hostname=result.get("hostname", node.hostname),
                    )

                    return remote_plugin, endpoint_info, node

        return None, None, None

    async def is_requester_allowed(self, requester_id: str) -> bool:
        """
        Checks if a requester plugin exists and is allowed to access endpoints.

        Args:
            requester_id: UUID of the plugin making the request

        Returns:
            True if the requester is allowed, False otherwise
        """
        # Check if requester exists locally
        requester_plugin = self.plugins_by_uuid.get(requester_id)
        if not requester_plugin:
            return False

        # Check if requester is enabled
        if not requester_plugin.enabled:
            return False

        # Add additional access control logic here if needed
        # For example, check if the requester has permission to access remote endpoints

        return True

    @async_handle_errors(None)
    async def _process_request(self, request: Request) -> None:
        """Process a request by invoking the target plugin method."""
        try:
            plugin_name = request.target_plugin
            function_name = request.target_method

            plugin, endpoint, node = await self.find_endpoint(
                request.target_method,
                request.target_hosts,
                request.blocked_hosts,
                request.target_plugin_uuid,
                request.author_id,
                request.target_plugin,
            )

            if not plugin:
                await self._set_request_result(
                    request, f"Endpoint {function_name} not found", True
                )
                return

            host_label = (
                f"(local) {self.hostname}"
                if isinstance(plugin, Plugin)
                else f"{node.IP}#{node.hostname}"
            )
            self._logger.debug(
                f"Found {plugin_name} (ID: {plugin.plugin_uuid}) for Request with ID {request.id} on host {host_label}"
            )

            if isinstance(
                plugin, RemotePlugin
            ):  # NOTE: Fix the timeout thing. Warn if ping is higher than timeout
                result = await self.network.execute_remote(
                    IP=node.IP,
                    plugin=plugin_name,
                    method=function_name,
                    args=request.args,
                    plugin_uuid=request.target_plugin_uuid,
                    author=f"{self.hostname} - {request.author}#{request.author_id}",
                    author_id=request.author_id,
                    timeout=(request.timeout_duration, request.created_at),
                    request_id=request.id,
                )

            else:
                func = getattr(plugin, endpoint.get("internal_name"), None)
                if not callable(func):
                    await self._set_request_result(
                        request,
                        f"Function {endpoint.get('access_name')}({endpoint.get('internal_name')}) not found in plugin {plugin_name}",
                        True,
                    )
                    return

                if inspect.isasyncgenfunction(func) or inspect.isgeneratorfunction(
                    func
                ):
                    await self._set_request_result(
                        request,
                        f"For Request {request.id}: The method you requested is a generator. Use execute_stream for generators",
                        True,
                    )
                    return

                try:
                    chain = getattr(request, "_call_chain", ())
                    result = await self._call_endpoint(func, request.args, chain)
                except Exception as e:
                    await self._set_request_result(request, str(e), True)
                    return

            await self._set_request_result(request, result)

        except Exception as e:
            # Safety net: if anything above failed without resolving the future,
            # resolve it now so the caller doesn't hang forever
            if not request._future.done():
                await self._set_request_result(
                    request, f"Unhandled error processing request: {e}", True
                )

    async def _call_endpoint(
        self, func: Callable, args: Any, call_chain: tuple = ()
    ) -> Any:
        """Call endpoint function handling sync/async and arg shapes."""
        if asyncio.iscoroutinefunction(func):
            if isinstance(args, tuple):
                return await func(*args)
            if isinstance(args, dict):
                return await func(**args)
            if args is None:
                return await func()
            return await func(args)

        # sync function -> threadpool, propagate call chain for cycle detection
        def _tracked(*a, **kw):
            _sync_call_chain.chain = call_chain
            try:
                return func(*a, **kw)
            finally:
                _sync_call_chain.chain = ()

        if isinstance(args, tuple):
            return await self.main_event_loop.run_in_executor(
                self._plugin_executor, _tracked, *args
            )
        if isinstance(args, dict):
            return await self.main_event_loop.run_in_executor(
                self._plugin_executor, functools.partial(_tracked, **args)
            )
        if args is None:
            return await self.main_event_loop.run_in_executor(
                self._plugin_executor, _tracked
            )
        return await self.main_event_loop.run_in_executor(
            self._plugin_executor, _tracked, args
        )

    # @async_handle_errors(None)
    async def _process_request_stream(self, request: GeneratorRequest) -> None:
        """Process a request by invoking the target plugin method."""
        try:
            plugin_name = request.target_plugin
            function_name = request.target_method

            plugin, endpoint, node = await self.find_endpoint(
                request.target_method,
                request.target_hosts,
                request.blocked_hosts,
                request.target_plugin_uuid,
                request.author_id,
                request.target_plugin,
            )

            if not plugin:
                await self._set_gen_request_result(
                    request, f"Endpoint {function_name} not found", True
                )
                return

            host_label = (
                f"(local) {self.hostname}"
                if isinstance(plugin, Plugin)
                else f"{node.IP}#{node.hostname}"
            )
            self._logger.debug(
                f"Found {plugin_name} (ID: {plugin.plugin_uuid}) for Request with ID {request.id} on host {host_label}"
            )

            if isinstance(plugin, RemotePlugin):
                async for result in self.network.execute_remote_stream(
                    IP=node.IP,
                    plugin=plugin_name,
                    method=function_name,
                    args=request.args,
                    plugin_uuid=request.target_plugin_uuid,
                    author=f"{self.hostname} - {request.author}#{request.author_id}",
                    author_id=request.author_id,
                    timeout=(request.timeout_duration, request.created_at),
                    request_id=request.id,
                ):
                    await request.queue.put((result, False, False))

            else:
                # if not plugin.remote and request.author_id:
                #    await self._set_request_result(request, NetworkRequestException(f"Plugin {plugin_name} is not accessable anymore"), True)
                #    return
                func = getattr(plugin, endpoint.get("internal_name"), None)
                if not callable(func):  # or not inspect.isfunction(func):
                    # await request.queue.put((result, False, False))
                    await self._set_gen_request_result(
                        request,
                        f"Function {endpoint.get('access_name')}({endpoint.get('internal_name')}) not found in plugin {plugin_name}",
                        True,
                    )
                    return

                if asyncio.iscoroutinefunction(func):
                    await self._set_gen_request_result(
                        request,
                        f"For Request {request.id}: The method you requested is a non-generator async function. Use execute for non-generators",
                        True,
                    )
                    return

                elif inspect.isasyncgenfunction(func):
                    if isinstance(request.args, tuple):
                        async for result in func(*request.args):
                            await request.queue.put((result, False, False))
                    elif isinstance(request.args, dict):
                        async for result in func(**request.args):
                            await request.queue.put((result, False, False))
                    elif request.args == None:
                        async for result in func():
                            await request.queue.put((result, False, False))
                    else:
                        async for result in func(request.args):
                            await request.queue.put((result, False, False))

                elif inspect.isgeneratorfunction(func):
                    if isinstance(request.args, tuple):
                        generator = func(*request.args)
                    elif isinstance(request.args, dict):
                        generator = func(**request.args)
                    elif request.args == None:
                        generator = func()
                    else:
                        generator = func(request.args)

                    sentinel = object()
                    while True:
                        result = await asyncio.to_thread(next, generator, sentinel)
                        if result is sentinel:
                            break
                        await request.queue.put(
                            (result, False, False)
                        )  # FIXME Add in utils

                else:
                    await self._set_gen_request_result(
                        request,
                        f"For Request {request.id}: The method you requested is a non-generator sync function. Use execute for non-generators",
                        True,
                    )
                    return
                    # result = await self.main_event_loop.run_in_executor(self._plugin_executor, func, request.args)

            await self._set_gen_request_result(request)

        except Exception as e:
            # Safety net: resolve the future so consumers don't hang forever
            if not request._future.done():
                await self._set_gen_request_result(
                    request, f"Unhandled error processing stream request: {e}", True
                )

    @async_handle_errors(None)
    async def _set_request_result(
        self, request: Request, result: Any, error: bool = False
    ) -> None:
        """Set the result of a request."""
        if isinstance(result, asyncio.Future):
            result = await result
        await request.set_result(result, error)

    @async_handle_errors(None)
    async def _set_gen_request_result(
        self, request: GeneratorRequest, result: Any = None, error: bool = False
    ) -> None:
        """Set the result of a request."""

        await request.set_result(result, error)

    async def running_loop(self):
        """Maintenance loop that cleans up tasks and requests."""
        while True:
            self.task_list = [t for t in self.task_list if not t.done()]
            await self.cleanup_requests()
            await asyncio.sleep(10)

    @async_log_errors
    async def cleanup_requests(self):
        """Remove collected requests older than 10 seconds.

        Uses created_at as the fallback when finished_at is unset — a few exit
        paths in Request.wait_for_result_async finalize state without setting
        finished_at (timeout/exception branches), and the previous filter
        (`finished_at is None` → keep forever) was leaking those forever.
        Falling back to created_at means even un-finalized-but-collected
        requests get reaped 10 s after creation.
        """
        _timer = time.time() - 10
        async with self.request_lock:
            self.requests = {
                rid: req
                for rid, req in self.requests.items()
                if not req.collected or (req.finished_at or req.created_at) > _timer
            }

    def _validate_host_args(self, hosts, blocked_hosts):
        """Normalize hosts/blocked_hosts and warn on redundant combos.

        Returns (hosts, blocked_hosts) ready to pass to find_endpoint.
        Raises ValueError on structural input errors. Idempotent — safe to
        call on already-normalized values.
        """
        hosts = _normalize_hosts(hosts, param_name="hosts", default="local")
        blocked_hosts = _normalize_hosts(
            blocked_hosts, param_name="blocked_hosts", default=None,
        )
        _warn_redundant_host_combos(hosts, blocked_hosts, self._logger)
        return hosts, blocked_hosts

    # One-liner methods for plugin communication
    # @async_handle_errors(default_return=None)
    # async def execute(self, target: str, args: Any = None, author: str = "system", timeout: Optional[float] = None) -> Any:
    @async_log_errors
    async def execute(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Any:
        """
        One-liner to execute a plugin method and get its result with built-in error handling.
        This combines request creation, processing, and result retrieval in one method.

        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds

        Returns:
            The result from the plugin method or None if any error occurs
        """
        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)

        if author == "system":
            author = self.hostname
            author_id = self.hostname

        request = await self.create_request(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            author_host,
            request_id,
        )
        try:
            result, error, _ = await request.wait_for_result_async()
            if error:
                self._logger.warning(
                    f"Error executing {plugin}.{method} (Req-ID: {request.id}): {result}. You can check the logs for this Req-ID."
                )
                raise RequestException(result)
            return result
        finally:
            # Mark for cleanup. Runs on normal return, RequestException, AND
            # CancelledError — without this, a cancelled caller would leave the
            # Request lingering in self.requests forever.
            await request.set_collected()

    @log_errors
    def execute_sync(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Any:
        """
        Synchronous one-liner to execute a plugin method with built-in error handling.

        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds

        Returns:
            The result from the plugin method or None if any error occurs
        """

        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)

        if author == "system":
            author = self.hostname
            author_id = self.hostname

        # Detect circular sync calls that would deadlock the threadpool
        chain = getattr(_sync_call_chain, "chain", ())
        target = f"{plugin}.{method}"
        if target in chain:
            raise RequestException(
                f"Circular sync call: {' -> '.join(chain)} -> {target}"
            )

        future = asyncio.run_coroutine_threadsafe(
            self._execute_sync_tracked(
                chain + (target,),
                plugin,
                method,
                args,
                plugin_uuid,
                hosts,
                blocked_hosts,
                author,
                author_id,
                timeout,
                author_host,
                request_id,
            ),
            self.main_event_loop,
        )
        return future.result()

    async def _execute_sync_tracked(
        self,
        call_chain,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Any:
        """Like execute(), but attaches the sync call chain to the request."""
        request = await self.create_request(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            author_host,
            request_id,
        )
        request._call_chain = call_chain
        try:
            result, error, _ = await request.wait_for_result_async()
            if error:
                self._logger.warning(
                    f"Error executing {plugin}.{method} (Req-ID: {request.id}): {result}"
                )
                raise RequestException(result)
            return result
        finally:
            await request.set_collected()

    @async_gen_log_errors
    async def execute_stream(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Any:
        """
        One-liner to execute a plugin method and get its result with built-in error handling.
        This combines request creation, processing, and result retrieval in one method.

        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds

        Returns:
            The result from the plugin method or None if any error occurs
        """

        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)

        if author == "system":
            author = self.hostname
            author_id = self.hostname

        request = await self.create_gen_request(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            author_host,
            request_id,
        )
        try:
            async for result, error, _ in request.get_queue_stream():
                if error:
                    self._logger.warning(
                        f"Error executing {plugin}.{method} (GenReq-ID: {request.id}): {result}. You can check the logs for this Req-ID."
                    )
                    raise RequestException(result)
                yield result
        finally:
            # Mark for cleanup. Runs on normal completion, RequestException,
            # AND CancelledError (e.g. when caller breaks out of `async for`
            # early) — without this, cancelled stream consumers would leave
            # the GeneratorRequest lingering forever.
            await request.set_collected()

    @gen_log_errors
    def execute_stream_sync(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        hosts: Union[
            str, list, None
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        author_host: str = None,
        request_id: str = None,
    ) -> Any:
        """
        Synchronous one-liner to execute a plugin method with built-in error handling.

        Args:
            target: The plugin and method in format "PluginName.method_name"
            args: Arguments to pass to the method
            author: The name of the caller (defaults to "system")
            timeout: Optional timeout in seconds

        Returns:
            The result from the plugin method or None if any error occurs
        """

        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)

        if author == "system":
            author = self.hostname
            author_id = self.hostname

        # Detect circular sync calls that would deadlock the threadpool
        chain = getattr(_sync_call_chain, "chain", ())
        target = f"{plugin}.{method}"
        if target in chain:
            raise RequestException(
                f"Circular sync call: {' -> '.join(chain)} -> {target}"
            )

        request = self.create_gen_request_sync(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
            author_host,
            request_id,
        )

        try:
            for result, error, _ in request.get_queue_stream_sync():
                if error:
                    self._logger.warning(
                        f"Error executing {plugin}.{method} (GenReq-ID: {request.id}): {result}. You can check the logs for this Req-ID."
                    )
                    raise RequestException(result)
                yield result
        finally:
            asyncio.run_coroutine_threadsafe(
                request.set_collected(), self.main_event_loop
            )

    # ── Notifier system ───────────────────────────────────────────────

    async def _resolve_subscription(self, sub: Subscription) -> tuple:
        """
        Resolve a Subscription to (plugin_name, access_name) for use with execute().

        For config-driven subs, the endpoint access_name is already known.
        For code-driven subs, a temporary endpoint is not needed — we call the
        handler directly via _call_endpoint.
        """
        return sub.plugin_name, sub.endpoint_access_name, sub.handler

    @async_log_errors
    async def notify(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        author: str = "system",
        author_id: str = "system",
    ) -> int:
        """
        Fire-and-forget publish to a topic. All matching subscribers are called
        concurrently; errors are logged but do not propagate.

        Returns the number of subscribers that were called.

        .. deprecated::
            The notifier subsystem is being redesigned. List-form
            hosts/blocked_hosts is rejected on this legacy path; full list
            support arrives with the rework. See notes.txt.
        """
        warnings.warn(
            "PluginCore.notify() uses the legacy notifier subsystem which is "
            "being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)
        if isinstance(hosts, list) or isinstance(blocked_hosts, list):
            raise ValueError(
                "Legacy notifier methods do not yet support list-form hosts/"
                "blocked_hosts. Use a string keyword ('any'/'local'/'remote') "
                "or a single hostname. Full list support arrives with the "
                "notifier rework."
            )

        subs = await self.topic_registry.find_all(topic)
        if not subs:
            self._logger.debug(f"Notify '{topic}': no subscribers")
            return 0

        called = 0

        async def _call_sub(sub: Subscription):
            nonlocal called
            try:
                plugin_name, access_name, handler = await self._resolve_subscription(
                    sub
                )

                if handler is not None:
                    # Code-driven: call handler directly
                    await self._call_endpoint(handler, args)
                elif access_name:
                    # Config-driven: route through execute()
                    await self.execute(
                        plugin_name,
                        access_name,
                        args,
                        plugin_uuid=sub.plugin_uuid,
                        hosts="local",
                        author=author,
                        author_id=author_id,
                    )
                called += 1
            except Exception as e:
                self._logger.warning(
                    f"Notify '{topic}': subscriber {sub} raised {type(e).__name__}: {e}"
                )

        # Local subscribers
        local_subs = [s for s in subs if s.plugin_uuid in self.plugins_by_uuid]
        local_targeted = hosts in ("any", "local", self.hostname)
        local_blocked = (
            blocked_hosts in ("any", "local", self.hostname)
            if blocked_hosts
            else False
        )
        if local_targeted and not local_blocked and local_subs:
            await asyncio.gather(*[_call_sub(s) for s in local_subs])

        # Remote notify (broadcast to all nodes)
        remote_needed = hosts in ("any", "remote") or (
            hosts not in ("local", self.hostname)
        )
        if remote_needed and getattr(self, "networking_enabled", False):
            for node in self.network.nodes:
                if not (node.enabled and await node.is_alive()):
                    continue
                if hosts not in ("any", "remote") and node.hostname != hosts:
                    continue
                if blocked_hosts is not None and (
                    blocked_hosts in ("any", "remote")
                    or blocked_hosts == node.hostname
                ):
                    continue
                try:
                    await self.network.notify_remote(
                        node.IP,
                        topic,
                        args,
                        author,
                        author_id,
                    )
                    called += 1  # Count remote dispatch as one call
                except Exception as e:
                    self._logger.warning(
                        f"Notify '{topic}': remote node {node.hostname} failed: {e}"
                    )

        return called

    @log_errors
    def notify_sync(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        author: str = "system",
        author_id: str = "system",
    ) -> int:
        """Synchronous variant of notify().

        .. deprecated::
            See notes.txt — the notifier subsystem is being redesigned.
        """
        warnings.warn(
            "PluginCore.notify_sync() uses the legacy notifier subsystem "
            "which is being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        future = asyncio.run_coroutine_threadsafe(
            self.notify(topic, args, hosts, blocked_hosts, author, author_id),
            self.main_event_loop,
        )
        return future.result()

    @async_log_errors
    async def request_topic(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        author: str = "system",
        author_id: str = "system",
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Request-by-topic: find the first matching handler and return its result.
        Same discovery logic as execute() with hosts="any" (local first).

        .. deprecated::
            See notes.txt — the notifier subsystem is being redesigned.
        """
        warnings.warn(
            "PluginCore.request_topic() uses the legacy notifier subsystem "
            "which is being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)
        if isinstance(hosts, list) or isinstance(blocked_hosts, list):
            raise ValueError(
                "Legacy notifier methods do not yet support list-form hosts/"
                "blocked_hosts. Use a string keyword ('any'/'local'/'remote') "
                "or a single hostname. Full list support arrives with the "
                "notifier rework."
            )

        # Try local subscription first (only if hosts includes local and not blocked)
        sub = None
        local_targeted = hosts in ("any", "local", self.hostname)
        local_blocked = (
            blocked_hosts in ("any", "local", self.hostname)
            if blocked_hosts
            else False
        )
        if local_targeted and not local_blocked:
            sub = await self.topic_registry.find_first(topic)

        if sub is not None:
            plugin_name, access_name, handler = await self._resolve_subscription(sub)

            if handler is not None:
                return await self._call_endpoint(handler, args)

            return await self.execute(
                plugin_name,
                access_name,
                args,
                plugin_uuid=sub.plugin_uuid,
                hosts="local",
                author=author,
                author_id=author_id,
                timeout=timeout,
            )

        # No local match (or hosts excludes local) — check remote
        if getattr(self, "networking_enabled", False) and hosts not in (
            "local",
            self.hostname,
        ):
            for node in self.network.nodes:
                if not (node.enabled and await node.is_alive()):
                    continue
                if hosts not in ("any", "remote") and node.hostname != hosts:
                    continue
                if blocked_hosts is not None and (
                    blocked_hosts in ("any", "remote")
                    or blocked_hosts == node.hostname
                ):
                    continue
                try:
                    result = await self.network.request_topic_remote(
                        node.IP,
                        topic,
                        args,
                        author,
                        author_id,
                        timeout,
                    )
                    if result is not REMOTE_NO_RESULT:
                        return result
                except Exception as e:
                    self._logger.warning(
                        f"request_topic '{topic}': remote {node.hostname} failed: {e}"
                    )

        raise RequestException(f"No handler found for topic '{topic}'")

    @log_errors
    def request_topic_sync(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        author: str = "system",
        author_id: str = "system",
        timeout: Optional[float] = None,
    ) -> Any:
        """Synchronous variant of request_topic().

        .. deprecated::
            See notes.txt — the notifier subsystem is being redesigned.
        """
        warnings.warn(
            "PluginCore.request_topic_sync() uses the legacy notifier subsystem "
            "which is being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        chain = getattr(_sync_call_chain, "chain", ())
        target = f"topic:{topic}"
        if target in chain:
            raise RequestException(
                f"Circular sync call: {' -> '.join(chain)} -> {target}"
            )

        future = asyncio.run_coroutine_threadsafe(
            self.request_topic(
                topic, args, hosts, blocked_hosts, author, author_id, timeout
            ),
            self.main_event_loop,
        )
        return future.result()

    @async_gen_log_errors
    async def request_topic_stream(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        author: str = "system",
        author_id: str = "system",
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Request-by-topic with streaming: find the first matching handler
        and yield its results.

        .. deprecated::
            See notes.txt — the notifier subsystem is being redesigned.
        """
        warnings.warn(
            "PluginCore.request_topic_stream() uses the legacy notifier "
            "subsystem which is being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)
        if isinstance(hosts, list) or isinstance(blocked_hosts, list):
            raise ValueError(
                "Legacy notifier methods do not yet support list-form hosts/"
                "blocked_hosts. Use a string keyword ('any'/'local'/'remote') "
                "or a single hostname. Full list support arrives with the "
                "notifier rework."
            )

        sub = None
        local_targeted = hosts in ("any", "local", self.hostname)
        local_blocked = (
            blocked_hosts in ("any", "local", self.hostname)
            if blocked_hosts
            else False
        )
        if local_targeted and not local_blocked:
            sub = await self.topic_registry.find_first(topic)

        if sub is None:
            if getattr(self, "networking_enabled", False) and hosts not in (
                "local",
                self.hostname,
            ):
                for node in self.network.nodes:
                    if not (node.enabled and await node.is_alive()):
                        continue
                    if hosts not in ("any", "remote") and node.hostname != hosts:
                        continue
                    if blocked_hosts is not None and (
                        blocked_hosts in ("any", "remote")
                        or blocked_hosts == node.hostname
                    ):
                        continue
                    try:
                        async for chunk in self.network.request_topic_stream_remote(
                            node.IP,
                            topic,
                            args,
                            author,
                            author_id,
                            timeout,
                        ):
                            yield chunk
                        return
                    except Exception as e:
                        self._logger.warning(
                            f"request_topic_stream '{topic}': remote {node.hostname} failed: {e}"
                        )
            raise RequestException(f"No handler found for topic '{topic}'")

        plugin_name, access_name, handler = await self._resolve_subscription(sub)

        if handler is not None:
            # Code-driven handler — must be an async generator
            if inspect.isasyncgenfunction(handler):
                if isinstance(args, tuple):
                    async for chunk in handler(*args):
                        yield chunk
                elif isinstance(args, dict):
                    async for chunk in handler(**args):
                        yield chunk
                elif args is None:
                    async for chunk in handler():
                        yield chunk
                else:
                    async for chunk in handler(args):
                        yield chunk
            elif inspect.isgeneratorfunction(handler):
                if isinstance(args, tuple):
                    gen = handler(*args)
                elif isinstance(args, dict):
                    gen = handler(**args)
                elif args is None:
                    gen = handler()
                else:
                    gen = handler(args)
                sentinel = object()
                while True:
                    chunk = await asyncio.to_thread(next, gen, sentinel)
                    if chunk is sentinel:
                        break
                    yield chunk
            else:
                raise RequestException(
                    f"Handler for topic '{topic}' is not a generator function"
                )
        else:
            async for chunk in self.execute_stream(
                plugin_name,
                access_name,
                args,
                plugin_uuid=sub.plugin_uuid,
                hosts="local",
                author=author,
                author_id=author_id,
                timeout=timeout,
            ):
                yield chunk

    @gen_log_errors
    def request_topic_stream_sync(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        author: str = "system",
        author_id: str = "system",
        timeout: Optional[float] = None,
    ) -> Any:
        """Synchronous streaming variant of request_topic().

        .. deprecated::
            See notes.txt — the notifier subsystem is being redesigned.
        """
        warnings.warn(
            "PluginCore.request_topic_stream_sync() uses the legacy notifier "
            "subsystem which is being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        hosts, blocked_hosts = self._validate_host_args(hosts, blocked_hosts)
        if isinstance(hosts, list) or isinstance(blocked_hosts, list):
            raise ValueError(
                "Legacy notifier methods do not yet support list-form hosts/"
                "blocked_hosts. Use a string keyword ('any'/'local'/'remote') "
                "or a single hostname. Full list support arrives with the "
                "notifier rework."
            )

        sub = asyncio.run_coroutine_threadsafe(
            self.topic_registry.find_first(topic),
            self.main_event_loop,
        ).result()

        if sub is None:
            raise RequestException(f"No handler found for topic '{topic}'")

        plugin_name, access_name, handler = asyncio.run_coroutine_threadsafe(
            self._resolve_subscription(sub),
            self.main_event_loop,
        ).result()

        if handler is not None:
            if inspect.isgeneratorfunction(handler):
                if isinstance(args, tuple):
                    yield from handler(*args)
                elif isinstance(args, dict):
                    yield from handler(**args)
                elif args is None:
                    yield from handler()
                else:
                    yield from handler(args)
            else:
                raise RequestException(
                    f"Handler for topic '{topic}' is not a sync generator"
                )
        else:
            for chunk in self.execute_stream_sync(
                plugin_name,
                access_name,
                args,
                plugin_uuid=sub.plugin_uuid,
                hosts="local",
                author=author,
                author_id=author_id,
                timeout=timeout,
            ):
                yield chunk

    async def subscribe(
        self,
        topic: str,
        plugin_name: str,
        plugin_uuid: str,
        endpoint_access_name: Optional[str] = None,
        handler: Optional[Callable] = None,
        config_driven: bool = False,
    ) -> str:
        """Register a topic subscription. Returns subscription ID.

        .. deprecated::
            The notifier subsystem is being redesigned. ``handler=`` will be
            removed and subscriptions will require an endpoint access_name
            target. See notes.txt.
        """
        warnings.warn(
            "PluginCore.subscribe() uses the legacy notifier subsystem which "
            "is being redesigned (handler= will be removed). See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.topic_registry.subscribe(
            topic_pattern=topic,
            plugin_name=plugin_name,
            plugin_uuid=plugin_uuid,
            endpoint_access_name=endpoint_access_name,
            handler=handler,
            config_driven=config_driven,
        )

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a topic subscription by ID.

        .. deprecated::
            See notes.txt — the notifier subsystem is being redesigned.
        """
        warnings.warn(
            "PluginCore.unsubscribe() uses the legacy notifier subsystem "
            "which is being redesigned. See notes.txt.",
            DeprecationWarning,
            stacklevel=2,
        )
        return await self.topic_registry.unsubscribe(subscription_id)
