# Custom Plugin Tabs in the CLI Dashboard

Plugins can register custom TUI panels that appear as dedicated tabs in the Dashboard.
Two approaches are available, depending on whether the plugin wants to depend on Textual.

## Option 1: `get_tui_widget()` — Full Textual Widget

Return a Textual `Widget` subclass. The widget gets full control within its tab:
layouts, reactive data, timers, custom CSS, child widgets — anything Textual supports.

```python
from textual.widgets import Static, DataTable
from textual.containers import Vertical

class MyPlugin(Plugin):

    def get_tui_widget(self):
        """Return a full Textual widget for the Dashboard tab."""
        container = Vertical()
        # Mount children in on_mount or compose
        return container
```

The widget is mounted inside a `VerticalScroll` container within the tab.
It **cannot** modify other tabs, app-level bindings, or the header/footer.

### What you can do inside your widget:
- Use any Textual widget (DataTable, RichLog, Input, Button, etc.)
- Define custom CSS (via `DEFAULT_CSS` on your widget class)
- Use reactive attributes and watchers
- Set up timers with `set_interval` / `set_timer`
- Use workers (`@work`) for async operations
- Nest containers and layouts freely

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
- For `get_tui_widget()`: your widget's CSS should not conflict with Dashboard CSS classes
