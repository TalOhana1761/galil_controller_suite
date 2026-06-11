"""
Panel 5 — Command Log.
Monospace log display with TX/RX/Error/Warning filtering.
Raw command input for direct DMC interaction.
Export to .txt.
"""
import time
import os
from datetime import datetime

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QLineEdit,
    QPlainTextEdit, QComboBox, QFileDialog,
    QSizePolicy,
)
from PyQt5.QtCore import Qt, pyqtSlot
from PyQt5.QtGui import QFont, QTextCursor, QColor, QTextCharFormat

_COLORS = {
    "TX":    "#00C9A7",
    "RX":    "#8B949E",
    "ERROR": "#FF4C4C",
    "WARN":  "#F0B429",
    "INFO":  "#3DBA6F",
}


class LogPanel(QWidget):
    def __init__(self, controller, worker, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.worker = worker
        self._lines: list[tuple[str, str, str]] = []   # (ts, direction, text)
        self._filter = "All"
        self._search = ""
        self._tx_count = 0
        self._rx_count = 0
        self._err_count = 0
        self._t0 = time.time()
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        title = QLabel("COMMAND LOG")
        title.setFont(QFont("Segoe UI", 13, QFont.Bold))
        title.setObjectName("axis_A")
        root.addWidget(title)

        # Command input
        cmd_row = QHBoxLayout()
        self._cmd_input = QLineEdit()
        self._cmd_input.setPlaceholderText("Enter DMC command (e.g. TP, MG _TPA, PRA=1000)")
        self._cmd_input.setFont(QFont("Consolas", 10))
        self._cmd_input.returnPressed.connect(self._send_cmd)
        cmd_row.addWidget(self._cmd_input)
        btn_send = QPushButton("SEND")
        btn_send.setObjectName("btn_enable")
        btn_send.clicked.connect(self._send_cmd)
        cmd_row.addWidget(btn_send)
        root.addLayout(cmd_row)

        # Filter + search + clear + export
        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("Filter:"))
        self._filter_combo = QComboBox()
        self._filter_combo.addItems(["All", "TX >>", "RX <<", "Errors", "Warnings"])
        self._filter_combo.currentTextChanged.connect(self._on_filter_changed)
        ctrl_row.addWidget(self._filter_combo)
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("Search…")
        self._search_box.setMaximumWidth(200)
        self._search_box.textChanged.connect(self._on_search_changed)
        ctrl_row.addWidget(self._search_box)
        ctrl_row.addStretch()
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._clear)
        btn_export = QPushButton("Export .txt")
        btn_export.clicked.connect(self._export)
        ctrl_row.addWidget(btn_clear)
        ctrl_row.addWidget(btn_export)
        root.addLayout(ctrl_row)

        # Log display
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setFont(QFont("Consolas", 9))
        self._log_view.setMaximumBlockCount(5000)
        root.addWidget(self._log_view, stretch=1)

        # Stats bar
        self._stats_lbl = QLabel("TX: 0   RX: 0   Errors: 0   Session: 0:00:00")
        self._stats_lbl.setStyleSheet("color: #8B949E; font-size: 9pt;")
        root.addWidget(self._stats_lbl)

    # ── Append ─────────────────────────────────────────────────────────────

    def append(self, direction: str, text: str):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self._lines.append((ts, direction, text))

        if direction in ("TX", ">>"):
            self._tx_count += 1
            dir_key = "TX"
        elif direction in ("RX", "<<"):
            self._rx_count += 1
            dir_key = "RX"
        elif direction in ("ERROR", "ERR"):
            self._err_count += 1
            dir_key = "ERROR"
        elif direction == "WARN":
            dir_key = "WARN"
        else:
            dir_key = "INFO"

        self._update_stats()

        if not self._matches_filter(dir_key, text):
            return
        self._append_line(ts, dir_key, text)

    def _append_line(self, ts: str, direction: str, text: str):
        color = _COLORS.get(direction, "#E6EDF3")
        dir_label = {"TX": ">> ", "RX": "<< ", "ERROR": "ERR", "WARN": "WRN", "INFO": "   "}.get(
            direction, "   ")
        line = f"{ts}  {dir_label}  {text}"
        cursor = self._log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.insertText(line + "\n", fmt)
        self._log_view.setTextCursor(cursor)
        self._log_view.ensureCursorVisible()

    # ── Send command ────────────────────────────────────────────────────────

    def _send_cmd(self):
        cmd = self._cmd_input.text().strip()
        if not cmd:
            return
        self.append("TX", cmd)
        self._cmd_input.clear()

        def _do_send():
            try:
                resp = self.controller.send(cmd)
                self.append("RX", resp.strip() or ":")
            except Exception as e:
                self.append("ERROR", str(e))

        self.worker.queue_command(_do_send)

    # ── Filter ─────────────────────────────────────────────────────────────

    def _on_filter_changed(self, text: str):
        self._filter = text
        self._redraw()

    def _on_search_changed(self, text: str):
        self._search = text.lower()
        self._redraw()

    def _matches_filter(self, direction: str, text: str) -> bool:
        filt = self._filter
        if filt == "TX >>" and direction != "TX":
            return False
        if filt == "RX <<" and direction != "RX":
            return False
        if filt == "Errors" and direction != "ERROR":
            return False
        if filt == "Warnings" and direction != "WARN":
            return False
        if self._search and self._search not in text.lower():
            return False
        return True

    def _redraw(self):
        self._log_view.clear()
        for ts, direction, text in self._lines:
            dir_key = {">>": "TX", "<<": "RX"}.get(direction, direction)
            if self._matches_filter(dir_key, text):
                self._append_line(ts, dir_key, text)

    # ── Stats ──────────────────────────────────────────────────────────────

    def _update_stats(self):
        elapsed = int(time.time() - self._t0)
        h, m = divmod(elapsed, 3600)
        m, s = divmod(m, 60)
        self._stats_lbl.setText(
            f"TX: {self._tx_count}   RX: {self._rx_count}   "
            f"Errors: {self._err_count}   Session: {h}:{m:02d}:{s:02d}"
        )

    # ── Utility ────────────────────────────────────────────────────────────

    def _clear(self):
        self._log_view.clear()

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Log", "galil_log.txt", "Text Files (*.txt)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            for ts, direction, text in self._lines:
                dir_label = {"TX": ">>", "RX": "<<"}.get(direction, direction)
                f.write(f"{ts}  {dir_label}  {text}\n")
        self.append("INFO", f"Log exported → {path}")

    # ── Slots ──────────────────────────────────────────────────────────────

    @pyqtSlot(str)
    def on_error(self, msg: str):
        self.append("ERROR", msg)

    @pyqtSlot(str, str, str)
    def on_log_line(self, ts: str, direction: str, text: str):
        self.append(direction, text)
