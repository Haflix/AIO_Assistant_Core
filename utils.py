from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import contextlib
import dataclasses
import datetime
import logging
from logging.handlers import QueueHandler, QueueListener
import os
from pathlib import Path
import queue
import socket
import sys
import threading
from logging import Logger, StreamHandler, DEBUG
from uuid import uuid4
import time
import yaml
from typing import Any, Optional, Tuple, Union, final
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors
from exceptions import RequestException, ConfigException
from colorama import Fore, Style


class _Mute:
    """Sentinel for fully-muted threshold; never compares >= record.levelno."""

    __slots__ = ()

    def __repr__(self):
        return "MUTE"


_MUTE = _Mute()

_LEVEL_NAMES: dict = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "MUTE": _MUTE,
}


def _parse_level(value, *, ctx_logger=None, ctx_label: str = ""):
    """Parse a level string into an int level or _MUTE.

    Returns the parsed level, or None if value is None / invalid.
    On invalid input, logs a warning via ctx_logger when supplied.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        if ctx_logger:
            ctx_logger.warning(
                "Invalid logger level type for %s: %r (expected str). Skipped.",
                ctx_label or "<entry>",
                value,
            )
        return None
    upper = value.strip().upper()
    if upper in _LEVEL_NAMES:
        return _LEVEL_NAMES[upper]
    if ctx_logger:
        ctx_logger.warning(
            "Invalid logger level for %s: %r. Allowed: %s. Skipped.",
            ctx_label or "<entry>",
            value,
            ", ".join(_LEVEL_NAMES.keys()),
        )
    return None


class ColoredFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.GREEN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.RED + Style.BRIGHT,
    }

    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, Fore.RESET)
        original_levelname = record.levelname
        record.levelname = f"{color}{record.levelname}{Style.RESET_ALL}"
        result = super().format(record)
        record.levelname = original_levelname
        return result


class _MutableStream:
    """A stream wrapper that can switch between terminal and logging output.

    In normal mode, writes go to the real terminal (via saved fd).
    When muted, writes are routed to a Python logger instead — UNLESS
    the current thread is in the exempt set (e.g. Textual's output thread).

    Since every reference (sys.stdout, sys.__stdout__, loguru's cached sink,
    any library's cached reference) points to the same _MutableStream object,
    calling mute() affects ALL of them at once. No need to chase individual
    sinks or patch third-party internals.
    """

    def __init__(self, real_stream, logger, log_level):
        self._real_stream = real_stream
        self._logger = logger
        self._log_level = log_level
        self._muted = False
        self._exempt_thread_names = set()  # checked by name at write-time
        self._log_buf = ""
        self._log_lock = threading.Lock()  # protects _log_buf in muted mode

    def mute(self, exempt_thread_names=()):
        """Switch to capture mode. Writes go to logging unless thread exempt.

        Thread names are checked at write-time (not resolved to IDs here)
        because exempt threads like Textual's "textual-output" may not
        exist yet when mute() is called.
        """
        self._exempt_thread_names = set(exempt_thread_names)
        self._muted = True

    def unmute(self):
        """Switch back to terminal mode."""
        # Flush remaining log buffer
        with self._log_lock:
            if self._log_buf.strip():
                self._logger.log(self._log_level, self._log_buf.rstrip())
                self._log_buf = ""
        self._muted = False
        self._exempt_thread_names.clear()

    def _is_exempt(self):
        """Check if current thread is exempt from capture."""
        if not self._exempt_thread_names:
            return False
        t = threading.current_thread()
        return t.name in self._exempt_thread_names

    def write(self, text):
        if not text:
            return 0
        if not self._muted or self._is_exempt():
            return self._real_stream.write(text)
        # Muted: route to logging, split on newlines
        with self._log_lock:
            self._log_buf += text
            while "\n" in self._log_buf:
                line, self._log_buf = self._log_buf.split("\n", 1)
                if line.strip():
                    try:
                        self._logger.log(self._log_level, line.rstrip())
                    except Exception:
                        pass
        return len(text)

    def flush(self):
        if not self._muted or self._is_exempt():
            return self._real_stream.flush()
        # Muted: flush log buffer
        with self._log_lock:
            if self._log_buf.strip():
                try:
                    self._logger.log(self._log_level, self._log_buf.rstrip())
                except Exception:
                    pass
                self._log_buf = ""

    def isatty(self):
        return self._real_stream.isatty()

    def fileno(self):
        return self._real_stream.fileno()

    @property
    def encoding(self):
        return self._real_stream.encoding

    @property
    def errors(self):
        return getattr(self._real_stream, "errors", "strict")

    @property
    def name(self):
        return getattr(self._real_stream, "name", "<mutable-stream>")

    def writable(self):
        return True

    def readable(self):
        return False

    def seekable(self):
        return False


class FDRedirector:
    """Redirect OS-level file descriptors 1 (stdout) and 2 (stderr) through
    Python logging.

    Many C extensions (HuggingFace, ONNX Runtime, llama.cpp, tqdm, etc.)
    write directly to fd 1/2 via native printf/fprintf, completely bypassing
    Python's sys.stdout and the logging module. This class intercepts those
    writes at the OS level and emits them as standard log records.

    Additionally, sys.stdout and sys.stderr are replaced with _MutableStream
    objects that can be switched to capture mode. When muted, ALL Python-level
    writes (including from libraries that cached a reference at import time)
    are routed to logging — except for exempt threads (e.g. Textual's output
    thread that needs real terminal access for rendering).

    Flow after start():
        C code  -> fd 1 (pipe) -> reader thread -> logging.getLogger("captured.stdout")
        Python  -> sys.stdout (_MutableStream) -> saved original fd -> terminal

    Flow after mute():
        C code  -> fd 1 (pipe) -> reader thread -> logging (unchanged)
        Python  -> sys.stdout (_MutableStream) -> logging (muted mode)
        Textual -> sys.stdout (_MutableStream) -> terminal (exempt thread)
    """

    def __init__(self):
        self._active = False
        self._saved_fds = {}  # {fd_num: dup'd copy of original fd}
        self._pipes = {}  # {fd_num: pipe_read_fd}
        self._threads = []
        self._original_streams = {}  # {fd_num: original sys.stdout / sys.stderr}
        self._original_dunder = {}  # {fd_num: original sys.__stdout__ / sys.__stderr__}
        self._redirect_streams = {}  # {fd_num: _MutableStream wrapping saved fd}

    def start(self):
        """Begin intercepting fd 1 and fd 2.

        Must be called AFTER a QueueHandler is attached to the root logger
        (so captured records have somewhere to go) and BEFORE StreamHandler
        creation (so StreamHandler gets sys.stdout pointing to the real
        terminal, not the pipe).
        """
        if self._active:
            return

        for fd_num, attr_name, log_name, log_level in (
            (1, "stdout", "captured.stdout", logging.INFO),
            (2, "stderr", "captured.stderr", logging.WARNING),
        ):
            original_stream = getattr(sys, attr_name)
            self._original_streams[fd_num] = original_stream

            # Save a copy of the original fd (e.g. fd 1 -> fd 5).
            # This copy stays open and points to the real terminal.
            saved_fd = os.dup(fd_num)
            self._saved_fds[fd_num] = saved_fd

            # Create a real stream wrapping the saved fd, then wrap in
            # _MutableStream so all references can be muted collectively.
            encoding = getattr(original_stream, "encoding", "utf-8") or "utf-8"
            real_stream = open(
                saved_fd, "w", encoding=encoding, closefd=False, buffering=1
            )
            logger = logging.getLogger(log_name)
            new_stream = _MutableStream(real_stream, logger, log_level)
            self._redirect_streams[fd_num] = new_stream
            setattr(sys, attr_name, new_stream)

            # Also patch sys.__stdout__ / sys.__stderr__ — some frameworks
            # (e.g. Textual) read these to get a terminal handle. Since they
            # point to the same _MutableStream, mute() affects them too, with
            # exempt threads still writing to the real terminal.
            dunder_name = f"__{attr_name}__"
            self._original_dunder[fd_num] = getattr(sys, dunder_name)
            setattr(sys, dunder_name, new_stream)

            # Create a pipe. The write end replaces fd 1/2 so any C code
            # doing write(1, ...) goes into the pipe. The read end is
            # consumed by a reader thread.
            pipe_r, pipe_w = os.pipe()
            os.dup2(pipe_w, fd_num)
            os.close(pipe_w)  # fd_num is now the only write end
            self._pipes[fd_num] = pipe_r

            # Reader thread: reads from pipe, emits log records
            t = threading.Thread(
                target=self._reader,
                args=(pipe_r, logger, log_level),
                name=f"fd-redirect-{attr_name}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        self._active = True

    def mute(self, exempt_thread_names=()):
        """Switch all streams to capture mode.

        Writes are routed to logging instead of the terminal, except for
        threads whose names are in exempt_thread_names (e.g. "textual-output").
        """
        for stream in self._redirect_streams.values():
            stream.mute(exempt_thread_names)

    def unmute(self):
        """Switch all streams back to terminal mode."""
        for stream in self._redirect_streams.values():
            stream.unmute()

    @staticmethod
    def _reader(pipe_r, logger, level):
        """Read from pipe fd, split on newlines, emit as log records.

        Runs in a daemon thread. Exits when the pipe write end is closed
        (os.read returns empty bytes).
        """
        buf = b""
        while True:
            try:
                data = os.read(pipe_r, 4096)
            except OSError:
                break
            if not data:
                break
            buf += data
            # Split complete lines and emit individually
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", errors="replace").rstrip("\r")
                if text.strip():
                    try:
                        logger.log(level, text)
                    except Exception:
                        pass
        # Flush any remaining partial line
        if buf:
            text = buf.decode("utf-8", errors="replace").rstrip("\r\n")
            if text.strip():
                try:
                    logger.log(level, text)
                except Exception:
                    pass
        os.close(pipe_r)

    def stop(self):
        """Restore original fds and streams. Reader threads drain and exit."""
        if not self._active:
            return

        # Unmute if still muted
        self.unmute()

        # Restore original fds — this closes the pipe write ends,
        # causing reader threads to see EOF and exit.
        for fd_num, saved_fd in self._saved_fds.items():
            os.dup2(saved_fd, fd_num)
            os.close(saved_fd)

        # Wait for reader threads to finish draining
        for t in self._threads:
            t.join(timeout=2.0)

        # Flush and close the real streams inside _MutableStream
        for s in self._redirect_streams.values():
            try:
                s._real_stream.flush()
                s._real_stream.close()
            except Exception:
                pass

        # Restore original Python stream objects
        for fd_num, stream in self._original_streams.items():
            attr_name = "stdout" if fd_num == 1 else "stderr"
            setattr(sys, attr_name, stream)

        # Restore sys.__stdout__ / sys.__stderr__
        for fd_num, stream in self._original_dunder.items():
            dunder_name = "__stdout__" if fd_num == 1 else "__stderr__"
            setattr(sys, dunder_name, stream)

        self._saved_fds.clear()
        self._pipes.clear()
        self._threads.clear()
        self._original_streams.clear()
        self._original_dunder.clear()
        self._redirect_streams.clear()
        self._active = False


class _PerLoggerLevelFilter(logging.Filter):
    """Per-logger threshold filter with prefix matching, longest-match wins.

    Holds two source dicts:
      _config: replaced wholesale on every apply_logger_levels_config().
      _plugin: mutated only by plugin API; survives config reloads.

    Plugin source wins over config for the same prefix. Effective threshold
    for a given record name is found by walking prefixes (longest match first).

    A unique _MUTE sentinel is used in place of a numeric level for muted
    entries — the filter short-circuits to drop before any numeric compare,
    avoiding the `record.levelno >= MUTE_INT` loophole.
    """

    def __init__(self, handler_label: str):
        super().__init__()
        self.handler_label = handler_label
        self._config: dict = {}
        self._plugin: dict = {}
        self._owners: dict[str, list[tuple[str, str]]] = {}
        self._resolved_cache: dict = {}
        self._lock = threading.RLock()

    def _resolve(self, logger_name: str):
        """Find effective threshold for logger_name. Returns int, _MUTE, or None.

        None means "no entry matches" — filter passes record (handler.level decides).
        Caller MUST hold self._lock.
        """
        cached = self._resolved_cache.get(logger_name)
        if cached is not None or logger_name in self._resolved_cache:
            return cached

        def _longest_match(mapping: dict):
            best_prefix = None
            best_len = -1
            for prefix in mapping:
                if logger_name == prefix or logger_name.startswith(prefix + "."):
                    if len(prefix) > best_len:
                        best_prefix = prefix
                        best_len = len(prefix)
            return best_prefix

        plugin_match = _longest_match(self._plugin)
        if plugin_match is not None:
            eff = self._plugin[plugin_match]
        else:
            config_match = _longest_match(self._config)
            eff = self._config[config_match] if config_match is not None else None

        self._resolved_cache[logger_name] = eff
        return eff

    def _eff_for(self, logger_name: str):
        with self._lock:
            return self._resolve(logger_name)

    def filter(self, record):
        eff = self._eff_for(record.name)
        if eff is _MUTE:
            return False
        if eff is None:
            return True
        return record.levelno >= eff

    def would_drop(self, record) -> bool:
        eff = self._eff_for(record.name)
        if eff is _MUTE:
            return True
        if eff is None:
            return False
        return record.levelno < eff

    def set_config(self, mapping: dict) -> None:
        """Wholesale replace of config-source state. Plugin state untouched."""
        with self._lock:
            self._config = dict(mapping)
            self._resolved_cache.clear()

    def set_plugin(self, name: str, level, owner: tuple[str, str]) -> None:
        with self._lock:
            owners = self._owners.setdefault(name, [])
            if owner not in owners:
                owners.append(owner)
            self._plugin[name] = level
            self._resolved_cache.clear()

    def clear_plugin(self, name: str, owner: tuple[str, str] | None = None) -> None:
        with self._lock:
            if owner is None:
                self._plugin.pop(name, None)
                self._owners.pop(name, None)
            else:
                owners = self._owners.get(name)
                if owners and owner in owners:
                    owners.remove(owner)
                    if not owners:
                        self._plugin.pop(name, None)
                        self._owners.pop(name, None)
            self._resolved_cache.clear()

    def clear_owned_by(self, plugin_name: str, plugin_uuid: str) -> None:
        # Matches by plugin_uuid only — uuid4 is unique, and entries set during
        # Plugin.on_load may be registered with the placeholder name "UNKNOWN"
        # (PluginCore assigns the real plugin_name AFTER __init__ returns).
        # plugin_name is accepted for API symmetry but ignored for matching.
        with self._lock:
            mutated = False
            empties: list[str] = []
            for name, owners in self._owners.items():
                stale = [o for o in owners if o[1] == plugin_uuid]
                for o in stale:
                    owners.remove(o)
                    mutated = True
                if not owners:
                    empties.append(name)
            for name in empties:
                self._plugin.pop(name, None)
                self._owners.pop(name, None)
            if mutated:
                self._resolved_cache.clear()

    def snapshot(self) -> dict:
        """Return a snapshot mapping prefix -> (level, owners-list)."""
        with self._lock:
            return {
                name: {
                    "config": self._config.get(name),
                    "plugin": self._plugin.get(name),
                    "owners": list(self._owners.get(name, [])),
                }
                for name in set(self._config) | set(self._plugin)
            }


class _EarlyDropFilter(logging.Filter):
    """Drops records on the caller thread when both per-handler filters would drop.

    Attached to the QueueHandler so fully-muted records never enter the queue —
    preserves perf parity with the old `propagate = False` shortcut.
    """

    def __init__(
        self, console_filter: _PerLoggerLevelFilter, file_filter: _PerLoggerLevelFilter
    ):
        super().__init__()
        self._console = console_filter
        self._file = file_filter

    def filter(self, record):
        if self._console.would_drop(record) and self._file.would_drop(record):
            return False
        return True


def _level_to_display(value):
    """Render a stored level value (int / _MUTE / None) back to a string."""
    if value is None:
        return None
    if value is _MUTE:
        return "MUTE"
    if isinstance(value, int):
        return logging.getLevelName(value)
    return str(value)


class LogUtil(logging.Logger):
    __FORMATTER = f"{Style.DIM}%(asctime)s {Style.RESET_ALL}{Style.BRIGHT}| {Fore.RESET}{Fore.BLUE}%(name)s {Style.RESET_ALL}{Style.BRIGHT}| %(levelname)s {Style.RESET_ALL}{Fore.RESET}{Style.BRIGHT}| {Style.DIM}%(module)s.%(funcName)s:%(lineno)d {Fore.RESET}{Style.RESET_ALL}{Style.BRIGHT}| {Fore.RESET}%(message)s"
    __FORMATTER_FILE = "%(asctime)s | %(name)s | %(levelname)s | %(module)s.%(funcName)s:%(lineno)d | %(message)s"

    def __init__(
        self,
        name: str,
        log_format: str = __FORMATTER,
        level: Union[int, str] = logging.DEBUG,
        *args,
        **kwargs,
    ) -> None:
        super().__init__(name, level)
        self.formatter = logging.Formatter(log_format)

    @staticmethod
    def change_level(log_level: str) -> None:
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)

        for handler in getattr(root_logger, "_custom_handlers", []):
            handler.setLevel(log_level)
        root_logger.info(f"Changed console handler level to {log_level}")

    @staticmethod
    def change_file_level(log_level: str) -> None:
        root_logger = logging.getLogger()
        fh = getattr(root_logger, "_file_handler", None)
        if fh:
            fh.setLevel(log_level)
            root_logger.info(f"Changed file handler level to {log_level}")

    @staticmethod
    def create(
        log_level: str = "DEBUG",
        file_level: str = "DEBUG",
        logger_levels: Optional[dict] = None,
    ) -> logging.Logger:
        """Create and configure the root logger with non-blocking I/O.

        Args:
            log_level: Threshold for the console handler.
            file_level: Threshold for the file handler.
            logger_levels: Optional per-logger threshold mapping (see
                apply_logger_levels_config for schema). Applied immediately so
                there's no bootstrap window before filters take effect.
        """
        logging.setLoggerClass(LogUtil)
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)

        # logging.root.setLevel(log_level)  # NOTE: FOR TESTING

        # If a previous create() ran (e.g. test setUp/tearDown cycle), tear
        # its listener and FD redirector down before installing new ones —
        # otherwise the old QueueListener thread blocks forever on its
        # orphaned queue and FDRedirector keeps the dup'd fds alive.
        old_listener = getattr(root_logger, "_queue_listener", None)
        if old_listener is not None:
            with contextlib.suppress(Exception):
                old_listener.stop()
        old_redirector = getattr(root_logger, "_fd_redirector", None)
        if old_redirector is not None:
            with contextlib.suppress(Exception):
                old_redirector.stop()

        # Remove existing handlers
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)

        # Create thread-safe queue and listener
        log_queue = queue.Queue(-1)  # Unlimited size
        queue_handler = QueueHandler(log_queue)
        root_logger.addHandler(queue_handler)

        # Redirect OS-level fd 1/2 through logging.
        # Must happen AFTER QueueHandler (so captured records route through
        # the logging pipeline) and BEFORE StreamHandler (so StreamHandler
        # gets sys.stdout pointing to the real terminal, not the pipe).
        redirector = FDRedirector()
        redirector.start()

        # Create actual I/O handlers — sys.stdout now wraps original terminal
        formatter = ColoredFormatter(LogUtil.__FORMATTER)
        formatterFile = logging.Formatter(LogUtil.__FORMATTER_FILE)

        # Console handler (writes to real terminal via saved fd, no loop)
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(log_level)

        # File handler
        logs_dir = "logs"
        os.makedirs(logs_dir, exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_filename = f"AIO_AI_{timestamp}.log"
        log_file_path = os.path.join(logs_dir, log_filename)
        file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
        file_handler.setFormatter(formatterFile)
        file_handler.setLevel(file_level)

        # Per-handler threshold filters + early-drop filter on the queue.
        # Replaces the old hardcoded `propagate = False` block — config-driven
        # thresholds now govern the six previously-silenced libs (and any others
        # the user adds in config.yml under general.logger_levels).
        console_filter = _PerLoggerLevelFilter("console")
        file_filter = _PerLoggerLevelFilter("file")
        early_drop = _EarlyDropFilter(console_filter, file_filter)
        stream_handler.addFilter(console_filter)
        file_handler.addFilter(file_filter)
        queue_handler.addFilter(early_drop)

        # Create and start listener
        listener = QueueListener(
            log_queue, stream_handler, file_handler, respect_handler_level=True
        )
        listener.start()

        # Expose references so plugins can manage them
        # (e.g. CLI plugin mutes console output while TUI is active)
        root_logger._queue_listener = listener
        root_logger._custom_handlers = [stream_handler]
        root_logger._file_handler = file_handler
        root_logger._fd_redirector = redirector
        root_logger._console_level_filter = console_filter
        root_logger._file_level_filter = file_filter
        root_logger._early_drop_filter = early_drop

        # Apply initial logger_levels from bootstrap config so the window
        # between filter attach and config application is zero.
        LogUtil.apply_logger_levels_config(logger_levels or {})

        # Ensure proper shutdown — redirector must stop before listener
        # so final captured output can still route through the pipeline.
        def stop_listener():
            redirector.stop()
            listener.stop()
            root_logger.removeHandler(queue_handler)

        import atexit

        atexit.register(stop_listener)

        root_logger.info(
            f"Non-blocking logging initialized with level '{log_level}'. Log file: {log_file_path}"
        )
        return root_logger

    @staticmethod
    def _get_filters() -> (
        tuple[Optional[_PerLoggerLevelFilter], Optional[_PerLoggerLevelFilter]]
    ):
        root = logging.getLogger()
        return (
            getattr(root, "_console_level_filter", None),
            getattr(root, "_file_level_filter", None),
        )

    @staticmethod
    def apply_logger_levels_config(mapping) -> None:
        """Replace config-source per-logger thresholds with the supplied mapping.

        Schema (each value either a string shorthand or a per-handler dict):
            {
                "asyncio": "MUTE",                       # both handlers muted
                "psycopg": {"console": "WARNING", "file": "DEBUG"},
            }

        Levels: DEBUG | INFO | WARNING | ERROR | CRITICAL | MUTE.
        Match is by prefix with dot boundary (longest match wins). Wildcards and
        empty-string keys are rejected with a warning. Invalid level strings are
        logged + skipped; sibling entries still apply.

        Plugin-source thresholds (set via Plugin.set_logger_level / PluginCore
        wrapper) are NOT touched — only the config source is replaced.
        """
        root = logging.getLogger()
        console_filter, file_filter = LogUtil._get_filters()
        if console_filter is None or file_filter is None:
            return

        if mapping is None:
            mapping = {}
        if not isinstance(mapping, dict):
            root.warning(
                "general.logger_levels must be a dict, got %s. Treating as empty.",
                type(mapping).__name__,
            )
            mapping = {}

        console_cfg: dict = {}
        file_cfg: dict = {}
        for key, value in mapping.items():
            if not isinstance(key, str) or not key.strip():
                root.warning(
                    "Invalid logger_levels key %r (empty or non-string). Skipped.", key
                )
                continue
            prefix = key.strip()
            if "*" in prefix or "?" in prefix:
                root.warning(
                    "Wildcards not supported in logger_levels key %r — use the bare prefix "
                    "(it already covers all sub-loggers). Skipped.",
                    prefix,
                )
                continue

            if isinstance(value, dict):
                console_val = _parse_level(
                    value.get("console"), ctx_logger=root, ctx_label=f"{prefix}.console"
                )
                file_val = _parse_level(
                    value.get("file"), ctx_logger=root, ctx_label=f"{prefix}.file"
                )
                if console_val is not None:
                    console_cfg[prefix] = console_val
                if file_val is not None:
                    file_cfg[prefix] = file_val
            else:
                shared = _parse_level(value, ctx_logger=root, ctx_label=prefix)
                if shared is not None:
                    console_cfg[prefix] = shared
                    file_cfg[prefix] = shared

        console_filter.set_config(console_cfg)
        file_filter.set_config(file_cfg)

    @staticmethod
    def set_logger_level(
        name: str,
        *,
        console=None,
        file=None,
        owner: tuple[str, str],
    ) -> None:
        """Set a plugin-source threshold for `name` on console and/or file.

        owner is required and must be (plugin_name, plugin_uuid). None for either
        of console/file means leave that side unchanged.
        """
        root = logging.getLogger()
        console_filter, file_filter = LogUtil._get_filters()
        if console_filter is None or file_filter is None:
            return
        if not isinstance(name, str) or not name.strip():
            root.warning("set_logger_level: invalid name %r. Ignored.", name)
            return
        prefix = name.strip()

        if console is not None:
            parsed = _parse_level(
                console, ctx_logger=root, ctx_label=f"{prefix}.console"
            )
            if parsed is not None:
                console_filter.set_plugin(prefix, parsed, owner)
        if file is not None:
            parsed = _parse_level(file, ctx_logger=root, ctx_label=f"{prefix}.file")
            if parsed is not None:
                file_filter.set_plugin(prefix, parsed, owner)

    @staticmethod
    def clear_logger_level(
        name: str,
        *,
        console: bool = True,
        file: bool = True,
        owner: Optional[tuple[str, str]] = None,
    ) -> None:
        """Clear a plugin-source threshold for `name`.

        owner=None clears the entry unconditionally regardless of which plugins
        own it (admin/CLI use case). When owner is supplied, only that owner is
        removed from the prefix's owner list; the entry persists if other owners
        remain.
        """
        console_filter, file_filter = LogUtil._get_filters()
        if console_filter is None or file_filter is None:
            return
        if console:
            console_filter.clear_plugin(name, owner)
        if file:
            file_filter.clear_plugin(name, owner)

    @staticmethod
    def clear_logger_levels_owned_by(plugin_name: str, plugin_uuid: str) -> None:
        """Remove all plugin-source entries owned by (plugin_name, plugin_uuid)."""
        console_filter, file_filter = LogUtil._get_filters()
        if console_filter is None or file_filter is None:
            return
        console_filter.clear_owned_by(plugin_name, plugin_uuid)
        file_filter.clear_owned_by(plugin_name, plugin_uuid)

    @staticmethod
    def list_logger_levels() -> dict:
        """Snapshot of all configured prefixes across both handlers.

        Shape per prefix:
            {
                "config":    {"console": "...", "file": "..."},
                "plugin":    {"console": "...", "file": "..."},
                "effective": {"console": "...", "file": "..."},
                "owners":    [(plugin_name, plugin_uuid), ...],
            }
        """
        console_filter, file_filter = LogUtil._get_filters()
        if console_filter is None or file_filter is None:
            return {}

        console_snap = console_filter.snapshot()
        file_snap = file_filter.snapshot()
        all_prefixes = set(console_snap) | set(file_snap)

        def _effective(snap_entry):
            if snap_entry is None:
                return None
            return (
                snap_entry["plugin"]
                if snap_entry["plugin"] is not None
                else snap_entry["config"]
            )

        result: dict = {}
        for prefix in sorted(all_prefixes):
            c = console_snap.get(prefix)
            f = file_snap.get(prefix)
            seen: dict = {}
            for o in c["owners"] if c else []:
                seen[o] = None
            for o in f["owners"] if f else []:
                seen[o] = None
            owners = list(seen.keys())
            result[prefix] = {
                "config": {
                    "console": _level_to_display(c["config"]) if c else None,
                    "file": _level_to_display(f["config"]) if f else None,
                },
                "plugin": {
                    "console": _level_to_display(c["plugin"]) if c else None,
                    "file": _level_to_display(f["plugin"]) if f else None,
                },
                "effective": {
                    "console": _level_to_display(_effective(c)),
                    "file": _level_to_display(_effective(f)),
                },
                "owners": owners,
            }
        return result


class ConfigUtil:
    @staticmethod
    @log_errors
    def load_config(config_path: str) -> dict:
        config = yaml.safe_load(Path(config_path).read_text())
        return config

    @staticmethod
    @log_errors
    def quickget_config(config_path: str, fallback_value: Any = None) -> dict:
        config = yaml.safe_load(Path(config_path).read_text())
        try:
            ConfigUtil.check_config_integrity(config)
            return config
        except Exception:
            return fallback_value

    @staticmethod
    @log_errors
    def check_config_integrity(yaml_config: dict, _logger=None):
        # Check required sections
        for section in ["plugins", "general", "networking"]:
            if section not in yaml_config:
                raise ConfigException(f"Missing config section: {section}")

        # Validate plugins
        for plugin in yaml_config.get("plugins", []):
            if "name" not in plugin or "enabled" not in plugin:
                raise ConfigException("Plugin entry missing name/enabled field")

            # Warn if path is empty but plugin_package isn't configured
            if not plugin.get("path") and "plugin_package" not in yaml_config.get(
                "general", {}
            ):
                if _logger:
                    _logger.warning("No path or plugin_package - plugins may not load")

        general = list(yaml_config.get("general", {}).keys())
        for key in ["hostname", "plugin_package", "console_log_level"]:
            if key not in general and _logger:
                _logger.warning(
                    f"Missing config section (Default value will be used): /general/{key}"
                )

        networking = list(yaml_config.get("networking", {}).keys())
        for key in [
            "enabled",
            "node_ips",
            "port",
            "direct_discoverable",
            "auto_discoverable",
            "discover_nodes",
        ]:
            if key not in networking and _logger:
                _logger.warning(
                    f"Missing config key (Default value will be used): /networking/{key}"
                )

    @staticmethod
    @log_errors
    def apply_configvalues(plugin_core):

        general_config = plugin_core.yaml_config.get("general", {})

        hostname = general_config.get("hostname")
        if not hostname:  # Covers None and empty string
            hostname = socket.gethostname()
            plugin_core.yaml_config["general"]["hostname"] = hostname
        plugin_core.hostname = hostname  # uuid4().hex
        plugin_core._logger.info(f"Network hostname: {plugin_core.hostname}")

        # Plugin base directory
        plugin_core.plugin_package = general_config.get("plugin_package", "plugins")
        plugin_core._logger.info(f"Plugin base directory: {plugin_core.plugin_package}")

        networking_config = plugin_core.yaml_config.get("networking")

        plugin_core.networking_enabled = networking_config.get("enabled", False)
        plugin_core.yaml_config["networking"][
            "enabled"
        ] = plugin_core.networking_enabled
        plugin_core._logger.info(
            f"Networking enabled: {plugin_core.networking_enabled}"
        )

        plugin_core.networking_port = networking_config.get("port", 2510)
        plugin_core.yaml_config["networking"]["port"] = plugin_core.networking_port
        plugin_core._logger.info(f"Networking Port: {plugin_core.networking_port}")

        plugin_core.networking_auto_discoverable = networking_config.get(
            "auto_discoverable", False
        )
        plugin_core.yaml_config["networking"][
            "auto_discoverable"
        ] = plugin_core.networking_auto_discoverable
        plugin_core._logger.info(
            f"auto_discoverable: {plugin_core.networking_auto_discoverable}"
        )

        plugin_core.networking_direct_discoverable = networking_config.get(
            "direct_discoverable", False
        )

        if (
            plugin_core.networking_auto_discoverable
            and not plugin_core.networking_direct_discoverable
        ):
            plugin_core._logger.info(
                "direct_discoverable will be set to True as auto_discoverable is active. You cannot deactivate direct_discoverable if auto_discoverable is set to True."
            )
            plugin_core.networking_direct_discoverable = True

        plugin_core.yaml_config["networking"][
            "direct_discoverable"
        ] = plugin_core.networking_direct_discoverable
        plugin_core._logger.info(
            f"direct_discoverable: {plugin_core.networking_direct_discoverable}"
        )

        # Security and connection pool configuration
        plugin_core.networking_secret = networking_config.get("secret", None)
        plugin_core.networking_cert_file = networking_config.get("cert_file", None)
        plugin_core.networking_key_file = networking_config.get("key_file", None)
        plugin_core.networking_pool_size = networking_config.get("pool_size", 5)

        if plugin_core.networking_secret:
            plugin_core._logger.warning(
                "Using shared secret from config file. Consider using environment variable NETWORKING_SECRET for better security."
            )


class Plugin(ABC):
    """Base class for all plugins."""

    @final
    def __init__(self, logger: Logger, plugin_core, arguments):
        self.description = "UNKNOWN"
        self.plugin_name = "UNKNOWN"
        self.version = "0.0.0"
        self.plugin_uuid = uuid4().hex
        self.enabled = False
        self.remote = False
        self.arguments = arguments
        self.endpoints = {}
        # PR3 Stage B: events: + subscriptions: parsed from plugin_config
        # by load_plugin_with_conf. Empty dicts here so plugin code in
        # on_load can read them safely (load order: __init__ -> on_load,
        # then PluginCore overwrites these attributes from YAML).
        self.events = {}
        self.subscriptions = {}
        # PR3 Stage B: prefix + verbose_notifier — resolved final values
        # set by PluginCore.load_plugin_with_conf.
        self.prefix = ""
        self.verbose_notifier = False
        # PR3 Stage B: track sub_uuids registered on behalf of THIS plugin
        # by the on_enable lifecycle wrapper. Used by on_disable wrapper
        # to unregister exactly the subs that were registered.
        self._sub_uuids: list = []

        self._logger = logger
        self._plugin_core = plugin_core
        self.event_loop = plugin_core.main_event_loop

        self.on_load(
            *(
                arguments if isinstance(arguments, (list, tuple)) else []
            ),  # Unpack list/tuple if applicable
            **(
                arguments if isinstance(arguments, dict) else {}
            ),  # Unpack dict if applicable
        )

    async def _to_dict(self):
        info_dict = {}
        info_dict["plugin_name"] = self.plugin_name
        info_dict["version"] = self.version
        info_dict["plugin_uuid"] = self.plugin_uuid
        info_dict["enabled"] = self.enabled
        info_dict["remote"] = self.remote
        info_dict["description"] = self.description
        info_dict["arguments"] = self.arguments
        # TODO: Include endpoint info. For now use PluginCore.get_plugin_info() instead.
        raise NotImplementedError

    def set_logger_level(
        self,
        name: str,
        *,
        console: Optional[str] = None,
        file: Optional[str] = None,
    ) -> None:
        """Override per-logger threshold(s) at runtime.

        Plugin-source overrides survive config.yml reloads but are auto-cleared
        when this plugin is disabled, popped, purged, or shut down.
        """
        self._plugin_core.set_logger_level(
            name,
            console=console,
            file=file,
            plugin_name=self.plugin_name,
            plugin_uuid=self.plugin_uuid,
        )

    def clear_logger_level(
        self,
        name: str,
        *,
        console: bool = True,
        file: bool = True,
    ) -> None:
        """Remove this plugin's override for `name` (other plugins' overrides survive)."""
        self._plugin_core.clear_logger_level(
            name,
            console=console,
            file=file,
            plugin_name=self.plugin_name,
            plugin_uuid=self.plugin_uuid,
        )

    def list_logger_levels(self) -> dict:
        """Snapshot of all configured per-logger thresholds (config + plugin sources)."""
        return self._plugin_core.list_logger_levels()

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
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Call another plugin's method asynchronously with error handling.

        Args:
            plugin: Target plugin name.
            method: Method name to call on the plugin.
            args: Arguments to pass (tuple, dict, or None).
            plugin_uuid: Optional UUID to target a specific plugin instance.
            hosts: Where to run — "any", "local", "remote", a hostname, or a list of hostnames.
            blocked_hosts: Hosts to exclude — same shape as `hosts`, or None for no blocking.
            author: Caller identifier (default "system").
            author_id: Caller ID (default "system").
            timeout: Optional timeout in seconds.

        Returns:
            The result from the target method or None if an error occurs.
        """
        return await self._plugin_core.execute(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            self.plugin_name,
            self.plugin_uuid,
            timeout,
        )

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
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Call another plugin's method synchronously with error handling.

        Args:
            plugin: Target plugin name.
            method: Method name to call on the plugin.
            args: Arguments to pass (tuple, dict, or None).
            plugin_uuid: Optional UUID to target a specific plugin instance.
            hosts: Where to run — "any", "local", "remote", a hostname, or a list of hostnames.
            blocked_hosts: Hosts to exclude — same shape as `hosts`, or None for no blocking.
            author: Caller identifier (default "system").
            author_id: Caller ID (default "system").
            timeout: Optional timeout in seconds.

        Returns:
            The result from the target method or None if an error occurs.
        """
        # PR3 Stage A: retrofit pre-start guard (Q1 closes B-038).
        # Calling execute_sync from on_load (before the framework's
        # main_event_loop is bound) used to silently hang on
        # run_coroutine_threadsafe(..., None); now it raises clearly.
        self._check_framework_started()
        return self._plugin_core.execute_sync(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            self.plugin_name,
            self.plugin_uuid,
            timeout,
        )

    # @async_log_errors
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
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Call another plugin's method asynchronously and stream results with error handling.

        Args:
            plugin: Target plugin name.
            method: Method name to call on the plugin.
            args: Arguments to pass (tuple, dict, or None).
            plugin_uuid: Optional UUID to target a specific plugin instance.
            hosts: Where to run — "any", "local", "remote", a hostname, or a list of hostnames.
            blocked_hosts: Hosts to exclude — same shape as `hosts`, or None for no blocking.
            author: Caller identifier (default "system").
            author_id: Caller ID (default "system").
            timeout: Optional timeout in seconds.

        Yields:
            Each value yielded by the target streaming method.
        """
        async for i in self._plugin_core.execute_stream(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            self.plugin_name,
            self.plugin_uuid,
            timeout,
        ):
            yield i

    @log_errors
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
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Call another plugin's method synchronously and stream results with error handling.

        Args:
            plugin: Target plugin name.
            method: Method name to call on the plugin.
            args: Arguments to pass (tuple, dict, or None).
            plugin_uuid: Optional UUID to target a specific plugin instance.
            hosts: Where to run — "any", "local", "remote", a hostname, or a list of hostnames.
            blocked_hosts: Hosts to exclude — same shape as `hosts`, or None for no blocking.
            author: Caller identifier (default "system").
            author_id: Caller ID (default "system").
            timeout: Optional timeout in seconds.

        Yields:
            Each value yielded by the target streaming method.
        """
        # PR3 Stage A: retrofit pre-start guard at CALL time (Q1 closes
        # B-038, symmetry with execute_sync). The guard cannot live in
        # the generator body itself — Python defers generator-body
        # execution until first iteration. We split into a non-generator
        # wrapper (this method) that runs the guard and returns the
        # inner generator below.
        self._check_framework_started()
        return self._execute_stream_sync_inner(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            author,
            author_id,
            timeout,
        )

    def _execute_stream_sync_inner(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None],
        plugin_uuid: Optional[str],
        hosts: Union[str, list, None],
        blocked_hosts: Union[str, list, None],
        author: str,
        author_id: str,
        timeout: Optional[float],
    ):
        """Generator body for execute_stream_sync (split out so the
        pre-start guard fires at call time, not at first iteration)."""
        for i in self._plugin_core.execute_stream_sync(
            plugin,
            method,
            args,
            plugin_uuid,
            hosts,
            blocked_hosts,
            self.plugin_name,
            self.plugin_uuid,
            timeout,
        ):
            yield i

    # ── Notifier: subscription management ─────────────────────────────

    async def subscribe(
        self,
        topic: str,
        target_access_name: Optional[str] = None,
        *,
        target_plugin: Optional[str] = None,
        target_plugin_uuid: Optional[str] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        authors: Union[str, list, None] = None,
        blocked_authors: Union[str, list, None] = None,
    ) -> str:
        """Subscribe to a topic.

        ``target_access_name`` must be a non-empty string naming a declared
        endpoint on this plugin (or on ``target_plugin`` for cross-plugin
        orchestrator subs). The framework dispatches matching events through
        ``execute()`` to that endpoint. Returns ``sub_uuid``.
        """
        if not isinstance(target_access_name, str) or not target_access_name:
            raise TypeError(
                "subscribe(): target_access_name must be a non-empty string "
                "naming a declared endpoint."
            )
        return await self._plugin_core.subscribe_event(
            topic,
            self.plugin_name,
            self.plugin_uuid,
            target_access_name=target_access_name,
            target_plugin=target_plugin,
            target_plugin_uuid=target_plugin_uuid,
            hosts=hosts,
            blocked_hosts=blocked_hosts,
            authors=authors,
            blocked_authors=blocked_authors,
        )

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a subscription by its sub_uuid."""
        return await self._plugin_core.unsubscribe_event(subscription_id)

    # ── PR3 Stage B: publish_event / request_event API ────────────────

    def _check_framework_started(self) -> None:
        """Guard helper — raise if the main event loop hasn't been bound yet.

        Used by every NEW sync entry point and (per Q1 retrofit
        guidance, closing B-038) by ``execute_sync`` when no event
        loop is available yet.
        """
        if self._plugin_core.main_event_loop is None:
            raise RequestException(
                "Framework not started — sync APIs require running event loop"
            )

    @async_log_errors
    async def publish_event(
        self,
        event_id: str,
        payload: Any = None,
        topic_vars: Optional[dict] = None,
        hosts: Union[str, list, None] = None,
        blocked_hosts: Union[str, list, None] = None,
    ) -> int:
        """Publish an event (1:N fire-and-forget) per PR3 LOCKED L."""
        return await self._plugin_core.publish_event(
            self,
            event_id,
            payload=payload,
            topic_vars=topic_vars,
            hosts=hosts,
            blocked_hosts=blocked_hosts,
        )

    @log_errors
    def publish_event_sync(
        self,
        event_id: str,
        payload: Any = None,
        topic_vars: Optional[dict] = None,
        hosts: Union[str, list, None] = None,
        blocked_hosts: Union[str, list, None] = None,
    ) -> int:
        """Sync variant of publish_event (C16)."""
        self._check_framework_started()
        return self._plugin_core.publish_event_sync(
            self,
            event_id,
            payload=payload,
            topic_vars=topic_vars,
            hosts=hosts,
            blocked_hosts=blocked_hosts,
        )

    @async_log_errors
    async def request_event(
        self,
        event_id: str,
        payload: Any = None,
        topic_vars: Optional[dict] = None,
        hosts: Union[str, list, None] = None,
        blocked_hosts: Union[str, list, None] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Request an event (1:1 ask) per PR3 LOCKED L."""
        return await self._plugin_core.request_event(
            self,
            event_id,
            payload=payload,
            topic_vars=topic_vars,
            hosts=hosts,
            blocked_hosts=blocked_hosts,
            timeout=timeout,
        )

    @log_errors
    def request_event_sync(
        self,
        event_id: str,
        payload: Any = None,
        topic_vars: Optional[dict] = None,
        hosts: Union[str, list, None] = None,
        blocked_hosts: Union[str, list, None] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Sync variant of request_event (C16)."""
        self._check_framework_started()
        return self._plugin_core.request_event_sync(
            self,
            event_id,
            payload=payload,
            topic_vars=topic_vars,
            hosts=hosts,
            blocked_hosts=blocked_hosts,
            timeout=timeout,
        )

    async def request_event_stream(
        self,
        event_id: str,
        payload: Any = None,
        topic_vars: Optional[dict] = None,
        hosts: Union[str, list, None] = None,
        blocked_hosts: Union[str, list, None] = None,
        timeout: Optional[float] = None,
    ):
        """Streaming variant of request_event."""
        async for chunk in self._plugin_core.request_event_stream(
            self,
            event_id,
            payload=payload,
            topic_vars=topic_vars,
            hosts=hosts,
            blocked_hosts=blocked_hosts,
            timeout=timeout,
        ):
            yield chunk

    @log_errors
    def request_event_stream_sync(
        self,
        event_id: str,
        payload: Any = None,
        topic_vars: Optional[dict] = None,
        hosts: Union[str, list, None] = None,
        blocked_hosts: Union[str, list, None] = None,
        timeout: Optional[float] = None,
    ):
        """Sync streaming variant of request_event (C16)."""
        self._check_framework_started()
        return self._request_event_stream_sync_inner(
            event_id, payload, topic_vars, hosts, blocked_hosts, timeout,
        )

    def _request_event_stream_sync_inner(
        self,
        event_id: str,
        payload: Any,
        topic_vars: Optional[dict],
        hosts: Union[str, list, None],
        blocked_hosts: Union[str, list, None],
        timeout: Optional[float],
    ):
        """Generator body — same split pattern as execute_stream_sync
        so the pre-start guard fires at call time, not first iteration."""
        for chunk in self._plugin_core.request_event_stream_sync(
            self,
            event_id,
            payload=payload,
            topic_vars=topic_vars,
            hosts=hosts,
            blocked_hosts=blocked_hosts,
            timeout=timeout,
        ):
            yield chunk

    @log_errors
    def subscribe_sync(
        self,
        topic: str,
        target_access_name: str,
        *,
        target_plugin: Optional[str] = None,
        target_plugin_uuid: Optional[str] = None,
        hosts: Union[str, list, None] = "any",
        blocked_hosts: Union[str, list, None] = None,
        authors: Union[str, list, None] = None,
        blocked_authors: Union[str, list, None] = None,
    ) -> str:
        """Sync variant of subscribe (C16). New-API-only; no legacy
        handler= path here — sync callers running on a worker thread
        should use the declarative target_access_name shape."""
        self._check_framework_started()
        future = asyncio.run_coroutine_threadsafe(
            self._plugin_core.subscribe_event(
                topic,
                self.plugin_name,
                self.plugin_uuid,
                target_access_name=target_access_name,
                target_plugin=target_plugin,
                target_plugin_uuid=target_plugin_uuid,
                hosts=hosts,
                blocked_hosts=blocked_hosts,
                authors=authors,
                blocked_authors=blocked_authors,
            ),
            self._plugin_core.main_event_loop,
        )
        return future.result()

    @log_errors
    def unsubscribe_sync(self, sub_uuid: str) -> bool:
        """Sync variant of unsubscribe (C16)."""
        self._check_framework_started()
        future = asyncio.run_coroutine_threadsafe(
            self._plugin_core.unsubscribe_event(sub_uuid),
            self._plugin_core.main_event_loop,
        )
        return future.result()

    @log_errors
    @abstractmethod
    def on_load(self):
        """Override this method to implement functionality that needs to happen while the plugin gets loaded."""
        raise NotImplementedError

    @async_log_errors
    @abstractmethod
    async def on_enable(self):
        """Override this method to implement plugin starting functionality. All loops and so on should be started here."""
        raise NotImplementedError

    @async_log_errors
    @abstractmethod
    async def on_disable(self):
        """Override this method to implement plugin disabling functionality. All loops and so on should be stopped here."""
        raise NotImplementedError


@dataclasses.dataclass
class Event:
    """Event delivered to subscriber handlers via publish_event/request_event.

    Per PR3 LOCKED I — receiving handlers get one positional arg, an Event,
    instead of the raw args/kwargs that execute() dispatches. Handler shape:

        async def handle_greet(self, event):
            name = event.payload["name"]
            ...

    Endpoints called via execute() are NOT wrapped — they keep the args
    (tuple/dict/None) shape. Dispatch path determines the wrapping; the
    sole place this class is constructed is the kind-aware branch in
    PluginCore._call_endpoint (Stage A) and the local fan-out path
    (Stage B).
    """

    topic: str  # literal topic that fired (post-resolution)
    payload: Any  # whatever was passed as payload to publish_event/request_event
    author: str  # publisher plugin_name
    author_id: str  # publisher plugin_uuid (runtime)
    author_host: str  # publisher hostname
    subscription_id: str  # declared_id (YAML key) or sub_uuid (runtime sub) per C4 (a)
    timestamp: float  # epoch seconds when publish_event/request_event was called

    @classmethod
    def from_request(cls, request: "Request") -> "Event":
        """Build an Event from a kind-aware Request.

        Used by PluginCore._call_endpoint when ``request.kind`` is one of
        ``"publish_event"`` / ``"request_event"``. The Request's
        ``origin_subscription_id`` carries either the declared_id (for
        YAML subs) or the sub_uuid (for runtime subs) — Stage B sets the
        appropriate value at fan-out time per PR3 LOCKED D + C4 (a).

        Raises ValueError if called with an execute-kind Request — that
        signals a Stage B fan-out bug (only event kinds should reach
        from_request). Defensive guard catches Stage B mistakes early.
        """
        if request.kind not in ("publish_event", "request_event"):
            raise ValueError(
                f"Event.from_request requires kind in "
                f"('publish_event', 'request_event'); got kind={request.kind!r}"
            )
        return cls(
            topic=request.topic if request.topic is not None else "",
            payload=request.args,
            author=request.author,
            author_id=request.author_id,
            author_host=request.author_host,
            subscription_id=request.origin_subscription_id or "",
            timestamp=request.timestamp,
        )


class Request:
    """Represents a request from one plugin to another."""

    def __init__(
        self,
        author_host: str,
        plugin: str,
        method: str,
        args: tuple = None,
        plugin_uuid: Optional[str] = "",
        target_hosts: Union[
            str, list
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        request_id: str = None,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
        # PR3 Stage A: notifier-rework fields. Defaults preserve execute path.
        kind: str = "execute",
        topic: Optional[str] = None,
        origin_subscription_id: Optional[str] = None,
        timestamp: Optional[float] = None,
        requester_id: Optional[str] = None,
    ) -> None:
        self.author_host = author_host
        self.author = author
        self.author_id = author_id
        self.id = uuid4().hex if not request_id else request_id
        self.target_plugin = plugin
        self.target_method = method
        self.target_plugin_uuid = plugin_uuid
        self.target_hosts = target_hosts
        self.blocked_hosts = blocked_hosts
        self.args = args
        self.collected = False
        self.timeout = False
        self.ready = False
        self.error = False
        self.result = None
        self.finished_at = None

        # PR3 Stage A: kind-aware fields. `kind` selects execute vs event
        # dispatch in PluginCore._call_endpoint; `topic` carries the
        # resolved literal topic for event kinds; `origin_subscription_id`
        # carries the sub_uuid this Request was fanned out for;
        # `timestamp` is epoch seconds at Request creation;
        # `requester_id` overrides author_id for find_endpoint's access
        # check (defaults to None → falls back to author_id at lookup).
        self.kind = kind
        self.topic = topic
        self.origin_subscription_id = origin_subscription_id
        self.timestamp = timestamp if timestamp is not None else time.time()
        self.requester_id = requester_id

        if type(timeout) == tuple:
            self.timeout_duration = timeout[0]
            self.created_at = timeout[1]
        else:
            self.created_at = time.time()
            self.timeout_duration = timeout

        self.event_loop = event_loop or asyncio.get_event_loop()
        self._future = self.event_loop.create_future()

    async def set_result(self, result: Any, error: bool = False) -> None:
        """Set the result of the request."""
        if not self._future.done():
            self.error = error
            self.result = result
            self._future.set_result((result, error, False))
            self.ready = True
            self.finished_at = time.time()

    async def set_collected(self) -> None:
        """Mark the request as collected for cleanup."""
        self.collected = True

    def get_result_sync(self) -> Any:
        """Get the result synchronously."""
        future = asyncio.run_coroutine_threadsafe(
            self.wait_for_result_async(), self.event_loop
        )
        try:
            result, error, timed_out = future.result()
            if error:
                raise Exception(f"Request failed: {self.result}")
            return self.result
        except Exception as e:
            raise e

    async def wait_for_result_async(self) -> Tuple[Any, bool, bool]:
        """Wait for the result asynchronously."""
        try:
            if self.result is not None:
                return self.result, self.error, self.timeout

            # Check if we need to apply a timeout
            if self.timeout_duration:
                remaining_time = self.timeout_duration - (time.time() - self.created_at)
                if remaining_time <= 0:
                    # Already timed out
                    self.result = f"Request {self.id} timed out"
                    self.error = True
                    self.ready = True
                    self.timeout = True
                    return self.result, True, True

                # Wait with timeout
                try:
                    result, error, timed_out = await asyncio.wait_for(
                        self._future, timeout=remaining_time
                    )
                    return result, error, timed_out
                except asyncio.TimeoutError:
                    self.result = f"Request {self.id} timed out"
                    self.error = True
                    self.ready = True
                    self.timeout = True
                    return self.result, True, True
            else:
                # Wait indefinitely
                result, error, timed_out = await self._future
                return result, error, timed_out
        except Exception as e:
            return str(e), True, False


class GeneratorRequest:
    """Represents a request from one plugin to another. This type of Request is made for use with streams and generators."""

    def __init__(
        self,
        author_host: str,
        plugin: str,
        method: str,
        args: tuple = None,
        plugin_uuid: Optional[str] = "",
        target_hosts: Union[
            str, list
        ] = "any",  # "any", "remote", "local", or list of allowed hosts
        blocked_hosts: Union[str, list, None] = None,  # blocked hosts (str keyword, list, or None)
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        request_id: str = None,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
        # PR3 Stage A: notifier-rework fields. Defaults preserve execute path.
        kind: str = "execute",
        topic: Optional[str] = None,
        origin_subscription_id: Optional[str] = None,
        timestamp: Optional[float] = None,
        requester_id: Optional[str] = None,
    ) -> None:
        self.author_host = author_host
        self.author = author
        self.author_id = author_id
        self.id = uuid4().hex if not request_id else request_id
        self.target_plugin = plugin
        self.target_method = method
        self.target_plugin_uuid = plugin_uuid
        self.target_hosts = target_hosts
        self.blocked_hosts = blocked_hosts
        self.args = args
        self.collected = False
        self.timeout = False
        self.ready = False
        self.error = False
        self.result = None
        self.finished_at = None

        # PR3 Stage A: kind-aware fields. See Request.__init__ for semantics.
        self.kind = kind
        self.topic = topic
        self.origin_subscription_id = origin_subscription_id
        self.timestamp = timestamp if timestamp is not None else time.time()
        self.requester_id = requester_id

        self.queue = asyncio.Queue()

        if type(timeout) == tuple:
            self.timeout_duration = timeout[0]
            self.created_at = timeout[1]
        else:
            self.created_at = time.time()
            self.timeout_duration = timeout

        self.event_loop = event_loop or asyncio.get_event_loop()
        self._future = self.event_loop.create_future()

    async def set_result(
        self, result: Any, error: bool = False, timeout: bool = False
    ) -> None:
        """Set the result of the request."""
        if not self._future.done():
            self.error = error
            self.result = result if result is not None else EndOfQueue()
            self.timeout = timeout
            self._future.set_result((result, error, timeout))
            self.ready = True
            self.finished_at = time.time()

            await self.queue.put((EndOfQueue(), self.error, self.timeout))

    async def set_collected(self) -> None:
        """Mark the request as collected for cleanup."""
        self.collected = True

    def get_queue_stream_sync(self):
        """Get the result stream synchronously."""

        iterator = self.get_queue_stream()
        while True:
            # future = asyncio.run_coroutine_threadsafe(anext(iterator), self.event_loop)
            future = asyncio.run_coroutine_threadsafe(
                iterator.__anext__(), self.event_loop
            )
            try:
                result = future.result()
                yield result
            except StopAsyncIteration:
                break

    async def get_queue_stream(self):
        """get the result stream asynchronously"""
        try:
            if self.result is not None:
                await self.set_result(str(self.result), True, False)

            while True:
                if self.timeout_duration:
                    remaining_time = self.timeout_duration - (
                        time.time() - self.created_at
                    )
                    if remaining_time <= 0:
                        # Already timed out
                        self.result = f"Request {self.id} timed out"
                        self.error = True
                        self.ready = True
                        self.timeout = True
                        await self.set_result(self.result, True, True)
                        break

                try:
                    if self.timeout_duration:
                        item, error, timed_out = await asyncio.wait_for(
                            self.queue.get(), timeout=remaining_time
                        )  #
                        # print(data)
                    else:
                        item, error, timed_out = await self.queue.get()
                        # print(data)

                except asyncio.TimeoutError:
                    self.result = f"Request {self.id} timed out"
                    self.error = True
                    self.ready = True
                    self.timeout = True
                    await self.set_result(self.result, True, True)
                    raise

                try:
                    if error:
                        await self.set_result(self.result, True, self.timeout)
                        # raise Exception(f"Request failed: {self.result}")
                        break
                    if type(item) == EndOfQueue:
                        break
                    yield item, error, timed_out
                except Exception as e:
                    await self.set_result(str(e), True, self.timeout)
                    raise e

        except Exception as e:
            await self.set_result(str(e), True, self.timeout)
            raise e

        finally:
            await self.set_result(None, self.error, self.timeout)


class EndOfQueue:
    def __init__(self):
        pass
