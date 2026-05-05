"""TestEventBadActor — bad-actor subscriber for PR3 Stage E.

Loaded on-demand by TestEventSuite (enabled: false in test_config.yml).
Handlers raise / hang / take a configurable amount of time on demand.
Used by sync-dispatcher serialization tests, exception-logging tests, and
hang-guard tests.
"""

import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import threading  # noqa: E402
from typing import Any, Dict, List  # noqa: E402

from utils import Plugin  # noqa: E402
from decorators import async_log_errors, log_errors  # noqa: E402


class TestEventBadActor(Plugin):
    """Bad-actor subscriber. Configurable failure modes."""

    @log_errors
    def on_load(self, *args, **kwargs):
        self.raise_msg: str = "bad actor raised"
        self.long_sync_secs: float = 0.0
        self.hanging_sync_secs: float = 0.0
        self.call_log: List[Dict[str, Any]] = []

    @async_log_errors
    async def on_enable(self):
        self._logger.info("TestEventBadActor enabled")

    @async_log_errors
    async def on_disable(self):
        self._logger.info("TestEventBadActor disabled")

    async def configure(self, settings):
        if isinstance(settings, dict):
            if "raise_msg" in settings:
                self.raise_msg = str(settings["raise_msg"])
            if "long_sync_secs" in settings:
                self.long_sync_secs = float(settings["long_sync_secs"])
            if "hanging_sync_secs" in settings:
                self.hanging_sync_secs = float(settings["hanging_sync_secs"])
        return {
            "raise_msg": self.raise_msg,
            "long_sync_secs": self.long_sync_secs,
            "hanging_sync_secs": self.hanging_sync_secs,
        }

    async def get_call_log(self):
        return list(self.call_log)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def handle_raising_async(self, event):
        self.call_log.append({
            "handler": "raising_async",
            "started_at": time.time(),
            "thread": threading.current_thread().name,
        })
        raise RuntimeError(self.raise_msg)

    def handle_raising_sync(self, event):
        self.call_log.append({
            "handler": "raising_sync",
            "started_at": time.time(),
            "thread": threading.current_thread().name,
        })
        raise RuntimeError(self.raise_msg)

    def handle_hanging_sync(self, event):
        started = time.time()
        self.call_log.append({
            "handler": "hanging_sync",
            "started_at": started,
            "thread": threading.current_thread().name,
        })
        # Sleep is tightly bounded by the test's hard_timeout_s.
        time.sleep(max(0.0, self.hanging_sync_secs))
        self.call_log.append({
            "handler": "hanging_sync_done",
            "finished_at": time.time(),
            "thread": threading.current_thread().name,
        })

    def handle_long_sync(self, event):
        started = time.time()
        self.call_log.append({
            "handler": "long_sync",
            "started_at": started,
            "thread": threading.current_thread().name,
        })
        time.sleep(max(0.0, self.long_sync_secs))
        finished = time.time()
        self.call_log.append({
            "handler": "long_sync_done",
            "finished_at": finished,
            "thread": threading.current_thread().name,
        })
