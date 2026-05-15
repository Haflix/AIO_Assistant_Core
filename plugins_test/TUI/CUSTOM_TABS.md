# Custom Plugin Tabs in the TUI Dashboard

Plugins can register custom TUI panels that appear as dedicated tabs in the Dashboard.
Two approaches are available, depending on UI complexity.

## Priority Chain

When building a plugin tab, the Dashboard checks in order:

1. `get_tui_module_info()` — Dashboard imports TUI module, creates widget
2. `get_tui_menu()` — Dashboard renders declarative dict
3. Auto-generated view from endpoints (fallback)

## View-Mode Toggle

When a plugin provides a custom view (module_info or menu), the tab automatically
shows "Custom View" / "Generated View" toggle buttons. Users can switch between
the custom TUI and the auto-generated endpoint view. No plugin code needed.

## Option 1: `get_tui_module_info()` — Dashboard-Loaded Widget (Recommended)

The plugin returns a dict pointing to its TUI package. The Dashboard imports it
via importlib, registering the package properly so internal relative imports work.

This approach exists because Plexus loads plugins as standalone modules without
`__package__`, which breaks relative imports inside plugin subpackages. The Dashboard
works around this by registering the TUI module in `sys.modules` as `_tui_{PluginName}`.

### Plugin side (plugin.py — no Textual import):

```python
class MyPlugin(Plugin):

    def get_tui_module_info(self):
        """Return path and class name for the Dashboard to import."""
        import os
        return {
            "path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "tui"),
            "class_name": "MyPluginWidget",
        }
```

### Required file structure:

```
MyPlugin/
  plugin.py
  tui/
    __init__.py           # MUST export the widget class
    main_widget.py        # Textual widget code
    css.py                # optional CSS constants
    sections/             # optional sub-widgets
      __init__.py
      ...
```

### `tui/__init__.py`:

```python
from .main_widget import MyPluginWidget
__all__ = ["MyPluginWidget"]
```

### `tui/main_widget.py` — relative imports work here:

```python
from textual.widgets import Static, DataTable
from textual.containers import Vertical, Horizontal

from .css import MY_CSS  # relative imports work!

class MyPluginWidget(Vertical):
    DEFAULT_CSS = MY_CSS

    def __init__(self, plugin, **kwargs):
        super().__init__(**kwargs)
        self._plugin = plugin

    def compose(self):
        yield Static("[bold]My Plugin[/bold]", markup=True)
        yield DataTable(id="my-table")

    def on_mount(self):
        table = self.query_one("#my-table", DataTable)
        table.add_columns("Name", "Value")
        self.set_interval(2.0, self._refresh)

    async def _refresh(self):
        # Update data here
        pass
```

### How the Dashboard loads it:

1. Calls `plugin.get_tui_module_info()` — gets path + class_name
2. Registers `tui/` as package `_tui_{PluginName}` in `sys.modules`
3. Executes `__init__.py` — internal relative imports resolve
4. Instantiates widget with `WidgetClass(plugin_instance)`
5. Module cached — reused on tab reopen, cleaned on tab close

### Reference implementation:

See `_private/MemoryPlugin/` (plugin.py + tui/ directory).

## Option 2: `get_tui_menu()` — Declarative Dict (No Textual Dependency)

Return a dict describing the UI. The Dashboard renders it automatically.
This approach requires **no Textual import** in your plugin.

```python
class MyPlugin(Plugin):

    def get_tui_menu(self):
        """Return a declarative menu dict for the Dashboard tab."""
        return {
            "label": "My Plugin",
            "sections": [
                {
                    "title": "Status",
                    "type": "info",
                    "items": [
                        {"label": "State", "value": "Running"},
                        {"label": "Count", "value": str(self._count)},
                    ],
                },
                {
                    "title": "Actions",
                    "type": "actions",
                    "items": [
                        {"label": "Reset Counter", "action": "reset"},
                    ],
                },
                {
                    "title": "Send Command",
                    "type": "input",
                    "action": "run_command",
                },
                {
                    "title": "Toggles",
                    "type": "toggle_list",
                    "items": [
                        {"label": "Verbose logging", "action": "toggle_verbose", "state": False},
                    ],
                },
            ],
        }
```

### Section types:

| Type          | Description                                      |
|---------------|--------------------------------------------------|
| `info`        | Key-value display items                          |
| `actions`     | Buttons that call plugin endpoints               |
| `input`       | Text input + submit button, calls an endpoint    |
| `toggle_list` | On/off switches that call endpoints with state   |

Each `action` string maps to a plugin endpoint `access_name`.
The Dashboard calls `plugin.execute(action, args)` when triggered.

## Opening a Plugin Tab

Users can open a plugin's tab from the Plugins list by selecting a plugin
and clicking "Open Tab". The tab appears alongside the built-in tabs and
can be closed via the "Close Tab" button within it.

## Limitations

- Custom widgets live inside their tab only — no access to other tabs or app globals
- The tab is destroyed and recreated each time it's opened (no persistent state between opens)
- Menu dict is read once at tab creation — to update values, close and reopen the tab
- Widget CSS should not conflict with Dashboard CSS classes
- `get_tui_widget()` is **not supported** — Plexus loads plugins without `__package__`, breaking relative imports. Use `get_tui_module_info()` instead
