"""
Custom logging handler that routes log records into a Textual DataTable widget.

Used by the Dashboard plugin to capture all application logs and display them
in the TUI instead of (or alongside) stdout.
"""

import itertools
import logging
import threading
import time
from collections import deque


class LogRecord:
    """Structured log record for the TUI log store."""

    __slots__ = ("seq", "timestamp", "level", "level_no", "source", "message", "full_text")

    _seq_counter = itertools.count(1)

    def __init__(self, timestamp: str, level: str, level_no: int,
                 source: str, message: str, full_text: str):
        self.seq = next(LogRecord._seq_counter)
        self.timestamp = timestamp
        self.level = level
        self.level_no = level_no
        self.source = source
        self.message = message
        self.full_text = full_text


class TUILogHandler(logging.Handler):
    """
    Logging handler that stores structured records and renders them
    into a Textual DataTable.

    - Before the TUI is ready, records are buffered (up to `max_buffer`).
    - Once `attach(data_table_widget, app)` is called, buffered records flush
      and new records accumulate in the persistent store.
    - `detach()` stops writing to the widget and re-enables buffering.
    - `display_level` filters which records are shown in the widget
      (records below this level are silently skipped in display, but stored).

    Thread-safe: emit() may be called from any thread; the lock
    protects the `_store` / `_buffer` deques and the `_dirty` flag.
    Cross-thread Textual DOM mutations happen via the TUI thread's
    own refresh timer reading from `_store`.
    """

    MAX_STORE = 5000

    def __init__(self, max_buffer: int = 500, level=logging.DEBUG):
        super().__init__(level)
        self._widget = None       # DataTable
        self._detail_widget = None  # Static for expanded detail
        self._app = None
        self._lock = threading.Lock()
        self._buffer: deque = deque(maxlen=max_buffer)

        # Persistent record store — survives tab switches, filter changes
        self._store: deque = deque(maxlen=self.MAX_STORE)

        # Display filter level — controls what gets shown in the table.
        self._display_level = logging.DEBUG
        # Text search filter
        self._search_filter: str = ""
        # Pause flag — when True, table doesn't update but store keeps growing
        self._paused: bool = False
        # Auto-scroll — move cursor to last row after refresh
        self._auto_scroll: bool = True

        self._formatter = logging.Formatter(
            "%(asctime)s | %(name)s | %(levelname)s | %(module)s.%(funcName)s:%(lineno)d | %(message)s"
        )
        self.setFormatter(self._formatter)

        # Batch refresh flag — set when new records arrive, cleared by timer
        self._dirty = False
        # Track whether a full rebuild is needed (filter/level change) vs append-only
        self._needs_rebuild = False
        # Seq IDs of records currently displayed in the DataTable, in order
        self._displayed_seqs: list = []

    def attach(self, data_table_widget, detail_widget=None, app=None):
        """Attach to a DataTable widget and flush buffered records.

        Buffer-drain happens UNDER the lock so a concurrent `emit()`
        on the logging thread cannot interleave its new-record append
        with the buffered-record replay — that would land the new
        record in `_store` before older buffered ones, breaking
        timestamp/seq ordering for downstream filtering.
        """
        with self._lock:
            self._widget = data_table_widget
            self._detail_widget = detail_widget
            self._app = app or data_table_widget.app
            pending = list(self._buffer)
            self._buffer.clear()
            for rec in pending:
                self._store.append(rec)
            self._dirty = True
            self._needs_rebuild = True

    def detach(self):
        """Detach from the widget, re-enable buffering."""
        with self._lock:
            self._widget = None
            self._detail_widget = None
            self._app = None

    @property
    def display_level(self) -> int:
        return self._display_level

    @display_level.setter
    def display_level(self, level: int) -> None:
        self._display_level = level
        self._dirty = True
        self._needs_rebuild = True

    @property
    def search_filter(self) -> str:
        return self._search_filter

    @search_filter.setter
    def search_filter(self, value: str) -> None:
        self._search_filter = value.lower()
        self._dirty = True
        self._needs_rebuild = True

    @property
    def paused(self) -> bool:
        return self._paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._paused = value
        if not value:
            self._dirty = True
            self._needs_rebuild = True

    @property
    def store(self) -> list:
        """Thread-safe snapshot of the record store."""
        with self._lock:
            return list(self._store)

    def emit(self, record):
        try:
            full_text = self.format(record)
            # Build structured record
            ts = time.strftime("%H:%M:%S", time.localtime(record.created))
            log_rec = LogRecord(
                timestamp=ts,
                level=record.levelname,
                level_no=record.levelno,
                source=record.name,
                message=record.getMessage(),
                full_text=full_text,
            )

            with self._lock:
                if self._widget is not None and self._app is not None:
                    self._store.append(log_rec)
                    self._dirty = True
                else:
                    self._buffer.append(log_rec)
        except Exception:
            self.handleError(record)

    def get_filtered_records(self):
        """Return records matching current display_level and search_filter.

        Takes a snapshot of the store under lock to prevent
        'deque mutated during iteration' from concurrent emit() calls.
        """
        with self._lock:
            snapshot = list(self._store)
        results = []
        level = self._display_level
        search = self._search_filter
        for rec in snapshot:
            if rec.level_no < level:
                continue
            if search:
                haystack = f"{rec.timestamp} {rec.level} {rec.source} {rec.message}".lower()
                if search not in haystack:
                    continue
            results.append(rec)
        return results

    # Maximum rows rendered in the DataTable at once (performance cap).
    MAX_DISPLAY = 500

    @staticmethod
    def _style_for_level(level_no: int) -> str:
        """Return a Rich style string for the given log level."""
        if level_no >= logging.ERROR:
            return "bold red"
        if level_no >= logging.WARNING:
            return "bold #cca75a"
        if level_no <= logging.DEBUG:
            return "#666666"
        return "#d4d4d4"

    def _make_row(self, rec):
        """Build a tuple of styled Text cells for a LogRecord."""
        from rich.text import Text
        style = self._style_for_level(rec.level_no)
        ts = Text(rec.timestamp, style=style)
        level = Text(rec.level, style=style)
        source_short = rec.source.split(".")[-1] if "." in rec.source else rec.source
        source = Text(source_short, style=style)
        msg_text = rec.message[:200] + "..." if len(rec.message) > 200 else rec.message
        msg = Text(msg_text, style=style)
        return ts, level, source, msg

    def refresh_table(self):
        """Update the DataTable from filtered store. Called by timer in app.

        Uses incremental updates (append new, remove old) when possible to
        preserve scroll position. Falls back to full rebuild when filters change.

        Returns (filtered_count, total_count) for status display, or None if
        no update was performed.
        """
        if not self._dirty or self._paused:
            return None
        self._dirty = False

        widget = self._widget
        if widget is None:
            return

        filtered = self.get_filtered_records()
        with self._lock:
            total = len(self._store)
        filtered_count = len(filtered)
        display_slice = filtered[-self.MAX_DISPLAY:] if filtered_count > self.MAX_DISPLAY else filtered
        new_seqs = [rec.seq for rec in display_slice]

        try:
            if self._needs_rebuild:
                # Full rebuild — filter or level changed
                self._needs_rebuild = False
                self._full_rebuild(widget, display_slice, new_seqs)
            else:
                # Incremental — only append/remove as needed
                self._incremental_update(widget, display_slice, new_seqs)

            # Auto-scroll: move cursor to last row
            if self._auto_scroll and widget.row_count > 0:
                widget.move_cursor(row=widget.row_count - 1)
        except Exception as e:
            logging.getLogger(__name__).debug("Log table refresh error: %s", e)

        return (filtered_count, total)

    def _full_rebuild(self, widget, display_slice, new_seqs):
        """Clear and re-add all rows."""
        widget.clear()
        for rec in display_slice:
            widget.add_row(*self._make_row(rec), key=str(rec.seq))
        self._displayed_seqs = new_seqs

    def _incremental_update(self, widget, display_slice, new_seqs):
        """Add new rows and remove dropped rows without clearing the table."""
        displayed_set = set(self._displayed_seqs)
        new_set = set(new_seqs)

        # Remove rows no longer in the display slice (dropped off front)
        removed = displayed_set - new_set
        for seq in removed:
            try:
                widget.remove_row(str(seq))
            except Exception:
                pass

        # Append rows that are new (added to tail)
        to_add = new_set - displayed_set
        if to_add:
            # Build a lookup for quick access
            rec_by_seq = {rec.seq: rec for rec in display_slice}
            # Add in order — only recs at the tail are new
            for rec in display_slice:
                if rec.seq in to_add:
                    widget.add_row(*self._make_row(rec), key=str(rec.seq))

        self._displayed_seqs = new_seqs
