"""TestLifecycleBrokenVersion — Phase 4 static fixture for B-007.

The plugin_config.yml deliberately omits the `version` field. PluginCore.load_plugin_with_conf only WARNS on missing fields, then the trailing log line at PluginCore.py:679-681 references plugin_config['version'] (subscript) which raises KeyError. The KeyError aborts the entire `for plugin_entry in self.yaml_config.get('plugins', [])` loop in get_plugins, so any plugins listed AFTER this one in config never load.

TestLifecycleSentinel is listed AFTER this plugin in config so the suite can detect B-007 by checking whether Sentinel ended up in core.plugins.
"""

from utils import Plugin
from decorators import async_log_errors, log_errors


class TestLifecycleBrokenVersion(Plugin):
    @log_errors
    def on_load(self, *args, **kwargs):
        pass

    @async_log_errors
    async def on_enable(self):
        pass

    @async_log_errors
    async def on_disable(self):
        pass

    @async_log_errors
    async def ping(self) -> str:
        return "ok"
