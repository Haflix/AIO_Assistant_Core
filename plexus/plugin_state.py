"""Plugin state machine types — Session 3 (v0.26.0).

Convention-only read-only data containers for the plugin lifecycle state
machine. ``State`` is a Python ``Enum`` and ``PluginState`` is a plain
``@dataclass`` — both are structurally mutable at the language level, but
external code MUST NOT mutate fields directly. All state mutation happens
through Plexus._transition_plugin(name, new_state) which validates the
transition and emits the ``_core/plugin/state_changed`` event. External
code reads via plx.plugin_states[name].state and
plx.plugin_states[name].last_errors only.

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

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, TYPE_CHECKING
import time

if TYPE_CHECKING:
    from .utils import Plugin


class State(Enum):
    """The six lifecycle states a plugin can be in.

    There is intentionally no `FAILED_ENABLE` or `FAILED_DISABLE` — enable
    failures roll back to INACTIVE with the error captured in
    ``PluginState.last_errors[Phase.ENABLE]``, and disable failures are
    forced through to INACTIVE after timeout with the error in
    ``last_errors[Phase.DISABLE]``.
    """

    UNLOADED = "unloaded"          # Config has entry but no instance.
    INACTIVE = "inactive"          # Instance exists, on_load done, not enabled.
    ENABLING = "enabling"          # on_enable in progress.
    ENABLED = "enabled"            # on_enable returned ok.
    DISABLING = "disabling"        # on_disable in progress.
    FAILED_LOAD = "failed_load"    # on_load raised.


class Phase(Enum):
    LOAD = "load"
    ENABLE = "enable"
    DISABLE = "disable"


@dataclass
class ErrorRecord:
    """C-146: store exception SHAPE + stringified traceback only — NOT
    the live ``BaseException`` instance, which would retain
    ``__traceback__`` and pin stack-frame locals (closures, large
    in-flight objects from the failed call) for the entire plugin
    lifetime. Memory burn was proportional to whatever happened to
    live in the frame at the moment of raise; could easily be
    hundreds of MB on a plugin that failed mid-batch-process. Now
    the record only holds:

    * ``exception_type`` — fully-qualified type name (e.g.
      ``ValueError``, ``my_plugin.MyError``). For operators trying
      to triage what KIND of error happened.
    * ``exception_repr`` — ``repr(exc)`` (typically
      ``ExceptionType('message')``). Captures the message + args
      without retaining the live instance.
    * ``traceback`` — pre-formatted multi-line string (the output
      of ``traceback.format_exc()`` at the catch site).
    * ``ts`` — wall-clock seconds at the catch site.

    Construction sites (``_enable_plugin_under_lock`` rollback,
    ``load_plugin_with_conf`` exec_module wrap, etc.) all already
    call ``traceback.format_exc()`` so the migration is just to
    drop the live exception and add the type/repr fields.
    """
    exception_type: str
    exception_repr: str
    traceback: str
    ts: float


@dataclass
class PluginState:
    name: str
    state: State
    instance: Optional["Plugin"] = None
    last_errors: Dict[Phase, ErrorRecord] = field(default_factory=dict)
    last_state_change: float = field(default_factory=lambda: time.time())
