"""Plugin state machine types — Session 3 (v0.26.0).

Read-only data containers for the plugin lifecycle state machine. All state
mutation happens through Plexus._transition_plugin(name, new_state).
External code reads via plx.plugin_states[name].state and
plx.plugin_states[name].last_errors but must NOT mutate fields directly —
no setters are exposed.

Iteration contract: external code reading `plx.plugin_states` MUST snapshot
before iterating. Single-key lookup via `.get(name)` / `[name]` is GIL-atomic
and safe under concurrency, but `pop_plugin` may `del` an entry between
iterator creation and consumption, which raises
`RuntimeError: dictionary changed size during iteration`. Snapshot pattern:

    states_snapshot = dict(plx.plugin_states)
    for name, ps in states_snapshot.items():
        ...

State enum is open-ended; future versions may add states. External tooling
reading `state.value` should handle unknown values gracefully (e.g. fall
back to a generic display).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from .utils import Plugin


class State(Enum):
    UNLOADED = "unloaded"        # Config has entry but no instance.
    INACTIVE = "inactive"        # Instance exists, on_load done, not enabled.
    ENABLING = "enabling"        # on_enable in progress.
    ENABLED = "enabled"          # on_enable returned ok.
    DISABLING = "disabling"      # on_disable in progress.
    FAILED_LOAD = "failed_load"  # on_load raised.


class Phase(Enum):
    LOAD = "load"
    ENABLE = "enable"
    DISABLE = "disable"


@dataclass
class ErrorRecord:
    exception: BaseException
    traceback: str
    ts: float


@dataclass
class PluginState:
    name: str
    state: State
    instance: Optional["Plugin"] = None
    last_errors: Dict[Phase, ErrorRecord] = field(default_factory=dict)
    last_state_change: float = field(default_factory=lambda: time.time())
