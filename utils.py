from abc import ABC, abstractmethod
import asyncio
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
from typing import Any, Callable, Optional, Tuple, Union, final
from decorators import log_errors, handle_errors, async_log_errors, async_handle_errors
from exceptions import RequestException, ConfigException
from colorama import Fore, Style


# class LogUtil(logging.Logger):
#    __FORMATTER = "%(asctime)s | %(name)s | %(levelname)s | %(module)s.%(funcName)s:%(lineno)d | %(message)s"
#    def __init__(
#            self,
#            name: str,
#            log_format: str = __FORMATTER,
#            level: Union[int, str] = DEBUG,
#            *args,
#            **kwargs
#    ) -> None:
#        super().__init__(name, level)
#        self.formatter = logging.Formatter(log_format)
#
#    @staticmethod
#    def create(log_level: str = 'DEBUG') -> logging.Logger:
#        """Create and configure the root logger."""
#        logging.setLoggerClass(LogUtil)
#        root_logger = logging.getLogger()
#        root_logger.setLevel(log_level)
#
#        # Remove existing handlers to avoid duplicates
#        for handler in root_logger.handlers[:]:
#            root_logger.removeHandler(handler)
#
#        # Create logs directory if it doesn't exist
#        logs_dir = "logs"
#        os.makedirs(logs_dir, exist_ok=True)
#
#        # Generate log filename with current timestamp
#        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
#        log_filename = f"AIO_AI_{timestamp}.log"
#        log_file_path = os.path.join(logs_dir, log_filename)
#
#        formatter = logging.Formatter(LogUtil.__FORMATTER)
#
#        # Add console handler
#        stream_handler = logging.StreamHandler(sys.stdout)
#        stream_handler.setFormatter(formatter)
#        root_logger.addHandler(stream_handler)
#
#        # Add file handler
#        file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
#        file_handler.setFormatter(formatter)
#        root_logger.addHandler(file_handler)
#
#        root_logger.info(f"Logging initialized. Log file: {log_file_path}")
#        return root_logger
class ColoredFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG:    Fore.CYAN,
        logging.INFO:     Fore.GREEN,
        logging.WARNING:  Fore.YELLOW,
        logging.ERROR:    Fore.RED,
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
        self._saved_fds = {}          # {fd_num: dup'd copy of original fd}
        self._pipes = {}              # {fd_num: pipe_read_fd}
        self._threads = []
        self._original_streams = {}   # {fd_num: original sys.stdout / sys.stderr}
        self._original_dunder = {}    # {fd_num: original sys.__stdout__ / sys.__stderr__}
        self._redirect_streams = {}   # {fd_num: _MutableStream wrapping saved fd}

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
            real_stream = open(saved_fd, "w", encoding=encoding,
                               closefd=False, buffering=1)
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
    def create(log_level: str = "DEBUG") -> logging.Logger:
        """Create and configure the root logger with non-blocking I/O"""
        logging.setLoggerClass(LogUtil)
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)

        # logging.root.setLevel(log_level)  # NOTE: FOR TESTING

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
        file_handler.setLevel(logging.DEBUG)

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

        # Prevent noisy third-party loggers from propagating
        for _name in ("httpx", "httpcore", "psycopg", "psycopg.pool",
                       "asyncio", "urllib3"):
            logging.getLogger(_name).propagate = False

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

    @async_log_errors
    async def execute(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        host: str = "any",  # "any", "remote", "local", or hostname
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
            host: Where to run: "any", "remote", "local", or a hostname.
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
            host,
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
        host: str = "any",  # "any", "remote", "local", or hostname
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
            host: Where to run: "any", "remote", "local", or a hostname.
            author: Caller identifier (default "system").
            author_id: Caller ID (default "system").
            timeout: Optional timeout in seconds.

        Returns:
            The result from the target method or None if an error occurs.
        """
        return self._plugin_core.execute_sync(
            plugin,
            method,
            args,
            plugin_uuid,
            host,
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
        host: str = "any",  # "any", "remote", "local", or hostname
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
            host: Where to run: "any", "remote", "local", or a hostname.
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
            host,
            self.plugin_name,
            self.plugin_uuid,
            timeout,
        ):
            yield i

    # @log_errors
    def execute_stream_sync(
        self,
        plugin: str,
        method: str,
        args: Union[tuple, dict, None] = None,
        plugin_uuid: Optional[str] = "",
        host: str = "any",  # "any", "remote", "local", or hostname
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
            host: Where to run: "any", "remote", "local", or a hostname.
            author: Caller identifier (default "system").
            author_id: Caller ID (default "system").
            timeout: Optional timeout in seconds.

        Yields:
            Each value yielded by the target streaming method.
        """
        for i in self._plugin_core.execute_stream_sync(
            plugin,
            method,
            args,
            plugin_uuid,
            host,
            self.plugin_name,
            self.plugin_uuid,
            timeout,
        ):
            yield i

    # ── Notifier: fire-and-forget (one-to-many) ──────────────────────────

    @async_log_errors
    async def notify(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        host: str = "any",
    ) -> int:
        """
        Publish to a topic (fire-and-forget). All subscribers are called
        concurrently; errors are logged but do not propagate.

        Args:
            topic: Topic string (e.g. "ai/chat", "sensor/bathroom/temperature").
            args: Arguments forwarded to every subscriber.
            host: Where to dispatch: "any", "local", "remote", or a hostname.

        Returns:
            Number of subscribers that were called.
        """
        return await self._plugin_core.notify(
            topic, args, host, self.plugin_name, self.plugin_uuid,
        )

    @log_errors
    def notify_sync(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        host: str = "any",
    ) -> int:
        """Synchronous variant of notify()."""
        return self._plugin_core.notify_sync(
            topic, args, host, self.plugin_name, self.plugin_uuid,
        )

    # ── Notifier: request-by-topic (one-to-one with response) ─────────

    @async_log_errors
    async def request_topic(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        host: str = "any",
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Request a topic — the first matching handler is called and its
        result returned. Same discovery logic as execute() with host="any".

        Args:
            topic: Topic string to request.
            args: Arguments forwarded to the handler.
            host: Where to search: "any", "local", "remote", or a hostname.
            timeout: Optional timeout in seconds.

        Returns:
            The result from the handler.
        """
        return await self._plugin_core.request_topic(
            topic, args, host, self.plugin_name, self.plugin_uuid, timeout,
        )

    @log_errors
    def request_topic_sync(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        host: str = "any",
        timeout: Optional[float] = None,
    ) -> Any:
        """Synchronous variant of request_topic()."""
        return self._plugin_core.request_topic_sync(
            topic, args, host, self.plugin_name, self.plugin_uuid, timeout,
        )

    async def request_topic_stream(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        host: str = "any",
        timeout: Optional[float] = None,
    ) -> Any:
        """
        Request a topic and stream results from the matching handler.

        Yields:
            Each value yielded by the handler.
        """
        async for i in self._plugin_core.request_topic_stream(
            topic, args, host, self.plugin_name, self.plugin_uuid, timeout,
        ):
            yield i

    def request_topic_stream_sync(
        self,
        topic: str,
        args: Union[tuple, dict, None] = None,
        host: str = "any",
        timeout: Optional[float] = None,
    ) -> Any:
        """Synchronous streaming variant of request_topic()."""
        for i in self._plugin_core.request_topic_stream_sync(
            topic, args, host, self.plugin_name, self.plugin_uuid, timeout,
        ):
            yield i

    # ── Notifier: subscription management ─────────────────────────────

    async def subscribe(self, topic: str, handler: Callable) -> str:
        """
        Subscribe to a topic at runtime (code-driven).

        Args:
            topic: Topic pattern (supports "*" wildcard per segment).
            handler: Callable to invoke when the topic is published/requested.

        Returns:
            Subscription ID (use with unsubscribe() to remove).
        """
        return await self._plugin_core.subscribe(
            topic, self.plugin_name, self.plugin_uuid, handler=handler,
        )

    async def unsubscribe(self, subscription_id: str) -> bool:
        """
        Remove a runtime subscription by its ID.

        Returns:
            True if the subscription was found and removed.
        """
        return await self._plugin_core.unsubscribe(subscription_id)

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


class Request:
    """Represents a request from one plugin to another."""

    def __init__(
        self,
        author_host: str,
        plugin: str,
        method: str,
        args: tuple = None,
        plugin_uuid: Optional[str] = "",
        target_host: str = "any",
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        request_id: str = None,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self.author_host = author_host
        self.author = author
        self.author_id = author_id
        self.id = uuid4().hex if not request_id else request_id
        self.target_plugin = plugin
        self.target_method = method
        self.target_plugin_uuid = plugin_uuid
        self.target_host = target_host
        self.args = args
        self.collected = False
        self.timeout = False
        self.ready = False
        self.error = False
        self.result = None
        self.finished_at = None

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
        target_host: str = "any",
        author: str = "system",
        author_id: str = "system",
        timeout: Union[float, tuple] = None,
        request_id: str = None,
        event_loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self.author_host = author_host
        self.author = author
        self.author_id = author_id
        self.id = uuid4().hex if not request_id else request_id
        self.target_plugin = plugin
        self.target_method = method
        self.target_plugin_uuid = plugin_uuid
        self.target_host = target_host
        self.args = args
        self.collected = False
        self.timeout = False
        self.ready = False
        self.error = False
        self.result = None
        self.finished_at = None

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
