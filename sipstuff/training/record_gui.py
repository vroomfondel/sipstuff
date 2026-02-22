#!/usr/bin/env python3
"""
PySide6 GUI application for recording voice samples for Piper TTS training datasets.

Displays a sentence table loaded from a LJSpeech-format metadata.csv file,
lets the user record WAV files by holding the spacebar, and provides a VU meter,
waveform display, and progress tracking. Recorded files are saved to the configured
output directory and the table status is updated in real time.

Dependencies:
    pip install PySide6 sounddevice soundfile numpy

Usage:
    python record_gui.py
    python record_gui.py --metadata metadata.csv --output ./wavs
    python record_gui.py --samplerate 22050 --device 3
"""

import argparse
import os
import signal
import sys
import time
from pathlib import Path
from typing import cast

import numpy as np
import sounddevice as sd
import soundfile as sf
from PySide6.QtCore import QEvent, QObject, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QFont,
    QIcon,
    QKeyEvent,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStyleFactory,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# ─── Audio Recording Thread ────────────────────────────────────────────────


class RecordThread(QThread):
    """QThread that records audio via sounddevice and emits level updates for the VU meter.

    Signals:
        level_update: Emitted with an RMS level in the range [0.0, 1.0] for each audio block.
        finished_recording: Emitted with the concatenated int16 audio array when recording stops.
    """

    level_update = Signal(float)  # RMS-Level 0.0 - 1.0
    finished_recording = Signal(np.ndarray)

    def __init__(self, samplerate: int = 22050, channels: int = 1, device: int | None = None) -> None:
        """Initialise the recording thread.

        Args:
            samplerate: Sample rate in Hz for the audio input stream.
            channels: Number of input channels (1 = mono).
            device: sounddevice input device index, or None for the system default.
        """
        super().__init__()
        self.samplerate = samplerate
        self.channels = channels
        self.device = device
        self._running = False
        self._chunks: list[np.ndarray] = []

    def run(self) -> None:
        """Open the audio input stream and collect PCM chunks until stop() is called.

        Emits level_update for every block and finished_recording with the full recording
        once the stream closes. If no audio was captured, emits an empty int16 array.
        """
        self._running = True
        self._chunks = []
        blocksize = 1024

        def callback(indata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags) -> None:
            if not self._running:
                raise sd.CallbackAbort()
            self._chunks.append(indata.copy())
            # RMS berechnen für VU-Meter
            rms = np.sqrt(np.mean(indata.astype(np.float32) ** 2))
            # Normalisieren auf 0-1 (int16 max = 32768)
            level = min(rms / 8000.0, 1.0)
            self.level_update.emit(level)

        try:
            with sd.InputStream(
                samplerate=self.samplerate,
                channels=self.channels,
                dtype="int16",
                blocksize=blocksize,
                device=self.device,
                callback=callback,
            ):
                while self._running:
                    self.msleep(10)
        except sd.CallbackAbort:
            pass

        if self._chunks:
            audio = np.concatenate(self._chunks, axis=0)
            self.finished_recording.emit(audio)
        else:
            self.finished_recording.emit(np.array([], dtype=np.int16))

    def stop(self) -> None:
        """Signal the recording loop to stop on the next callback invocation."""
        self._running = False


class PlayThread(QThread):
    """QThread that plays a WAV file via sounddevice and emits position updates.

    Signals:
        playback_finished: Emitted when playback completes or is stopped.
        position_update: Emitted with the current playback position as a fraction [0.0, 1.0].
    """

    playback_finished = Signal()
    position_update = Signal(float)  # 0.0 - 1.0

    def __init__(self, filepath: str | Path) -> None:
        """Initialise the playback thread.

        Args:
            filepath: Path to the WAV file to play.
        """
        super().__init__()
        self.filepath = filepath
        self._running = False

    def run(self) -> None:
        """Read the WAV file and stream it to the default output device.

        Emits position_update on every block and playback_finished when done or stopped.
        Handles both mono (1-D) and multi-channel (2-D) audio arrays.
        """
        self._running = True
        data, sr = sf.read(self.filepath, dtype="int16")
        total_frames = len(data)
        blocksize = 1024
        pos = 0

        def callback(outdata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags) -> None:
            nonlocal pos
            if not self._running:
                raise sd.CallbackAbort()
            end = pos + frames
            if end > total_frames:
                chunk = data[pos:total_frames]
                outdata[: len(chunk)] = chunk.reshape(-1, 1) if chunk.ndim == 1 else chunk
                outdata[len(chunk) :] = 0
                self._running = False
                raise sd.CallbackStop()
            else:
                chunk = data[pos:end]
                outdata[:] = chunk.reshape(-1, 1) if chunk.ndim == 1 else chunk
            pos = end
            self.position_update.emit(pos / total_frames)

        try:
            with sd.OutputStream(
                samplerate=sr,
                channels=1 if data.ndim == 1 else data.shape[1],
                dtype="int16",
                blocksize=blocksize,
                callback=callback,
            ):
                while self._running:
                    self.msleep(10)
        except (sd.CallbackAbort, sd.CallbackStop):
            pass

        self.playback_finished.emit()

    def stop(self) -> None:
        """Signal the playback callback to abort on the next invocation."""
        self._running = False


# ─── VU Meter Widget ───────────────────────────────────────────────────────


class VUMeter(QWidget):
    """Vertical VU meter widget with a green-to-yellow-to-red gradient bar and peak hold marker.

    The peak marker decays slowly after the signal drops, driven by an internal QTimer.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.level = 0.0
        self.peak = 0.0
        self.peak_decay = 0.0
        self.setMinimumSize(40, 120)
        self.setMaximumWidth(50)

        self._decay_timer = QTimer(self)
        self._decay_timer.timeout.connect(self._decay_peak)
        self._decay_timer.start(50)

    def set_level(self, level: float) -> None:
        """Update the current audio level and refresh the widget.

        Args:
            level: Normalised RMS level in the range [0.0, 1.0].
        """
        self.level = max(0.0, min(1.0, level))
        if self.level > self.peak:
            self.peak = self.level
            self.peak_decay = 0
        self.update()

    def reset(self) -> None:
        """Reset both the current level and peak hold to zero and repaint."""
        self.level = 0.0
        self.peak = 0.0
        self.update()

    def _decay_peak(self) -> None:
        """Timer slot: gradually decay the peak hold marker and the current level bar."""
        self.peak_decay += 1
        if self.peak_decay > 10:
            self.peak = max(0.0, self.peak - 0.02)
        self.level = max(0.0, self.level - 0.03)
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        """Render the gradient level bar, peak marker, and border."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        margin = 4
        bar_w = w - 2 * margin
        bar_h = h - 2 * margin

        # Hintergrund
        painter.fillRect(margin, margin, bar_w, bar_h, QColor(30, 30, 30))

        # Gradient für den Pegel
        level_h = int(bar_h * self.level)
        if level_h > 0:
            gradient = QLinearGradient(0, h - margin, 0, margin)
            gradient.setColorAt(0.0, QColor(0, 200, 0))
            gradient.setColorAt(0.6, QColor(200, 200, 0))
            gradient.setColorAt(1.0, QColor(255, 50, 0))

            painter.setBrush(QBrush(gradient))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(margin, h - margin - level_h, bar_w, level_h)

        # Peak-Marker
        if self.peak > 0.01:
            peak_y = h - margin - int(bar_h * self.peak)
            painter.setPen(QPen(QColor(255, 255, 255), 2))
            painter.drawLine(margin, peak_y, margin + bar_w, peak_y)

        # Rahmen
        painter.setPen(QPen(QColor(80, 80, 80), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(margin, margin, bar_w, bar_h)

        # dB-Skala
        painter.setPen(QColor(150, 150, 150))
        font = painter.font()
        font.setPointSize(7)
        painter.setFont(font)

        painter.end()


# ─── Waveform Widget ──────────────────────────────────────────────────────


class WaveformWidget(QWidget):
    """Widget that renders the waveform of a recorded audio buffer.

    Downsamples the PCM data to fit the widget width and draws a min/max envelope.
    A vertical orange playback-position line is overlaid during playback.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.audio_data: np.ndarray | None = None
        self.playback_pos = 0.0
        self.setMinimumHeight(100)
        self.setMaximumHeight(160)

    def set_audio(self, data: np.ndarray) -> None:
        """Load new audio data and reset the playback cursor.

        Args:
            data: int16 PCM array (mono or multi-channel, any length).
        """
        self.audio_data = data
        self.playback_pos = 0.0
        self.update()

    def set_playback_pos(self, pos: float) -> None:
        """Move the playback cursor and trigger a repaint.

        Args:
            pos: Normalised playback position in the range [0.0, 1.0].
        """
        self.playback_pos = pos
        self.update()

    def clear(self) -> None:
        """Remove the current waveform and reset the playback cursor."""
        self.audio_data = None
        self.playback_pos = 0.0
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        """Render the waveform envelope, playback cursor, and duration label."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        mid_y = h / 2

        # Hintergrund
        painter.fillRect(0, 0, w, h, QColor(25, 25, 30))

        # Mittellinie
        painter.setPen(QPen(QColor(60, 60, 70), 1))
        painter.drawLine(0, int(mid_y), w, int(mid_y))

        if self.audio_data is not None and len(self.audio_data) > 0:
            data = self.audio_data.flatten().astype(np.float32)
            # Downsampling für die Darstellung
            samples_per_pixel = max(1, len(data) // w)

            # Wellenform zeichnen
            pen = QPen(QColor(0, 180, 255), 1)
            painter.setPen(pen)

            path = QPainterPath()
            first = True

            for x in range(w):
                start = x * samples_per_pixel
                end = min(start + samples_per_pixel, len(data))
                if start >= len(data):
                    break

                chunk = data[start:end]
                max_val = np.max(chunk)
                min_val = np.min(chunk)

                # Normalisieren auf Widget-Höhe
                y_max = mid_y - (max_val / 32768.0) * (mid_y - 4)
                y_min = mid_y - (min_val / 32768.0) * (mid_y - 4)

                painter.drawLine(x, int(y_max), x, int(y_min))

            # Playback-Position
            if self.playback_pos > 0.0:
                px = int(w * self.playback_pos)
                painter.setPen(QPen(QColor(255, 100, 0), 2))
                painter.drawLine(px, 0, px, h)

            # Dauer anzeigen
            duration = len(data) / 22050.0
            painter.setPen(QColor(180, 180, 180))
            font = painter.font()
            font.setPointSize(9)
            painter.setFont(font)
            painter.drawText(8, h - 8, f"{duration:.1f}s")
        else:
            # Platzhalter
            painter.setPen(QColor(80, 80, 80))
            font = painter.font()
            font.setPointSize(11)
            painter.setFont(font)
            painter.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter, "Noch keine Aufnahme")

        # Rahmen
        painter.setPen(QPen(QColor(60, 60, 70), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(0, 0, w - 1, h - 1)

        painter.end()


# ─── Status Indicator ──────────────────────────────────────────────────────


class StatusIndicator(QLabel):
    """Coloured status label that reflects the current recorder state (idle/recording/playing/saved)."""

    STYLES = {
        "idle": ("Bereit", "#888888", "#2a2a2a"),
        "recording": ("⏺ AUFNAHME", "#ff4444", "#3a1a1a"),
        "playing": ("▶ Wiedergabe", "#44aaff", "#1a2a3a"),
        "saved": ("✓ Gespeichert", "#44cc44", "#1a3a1a"),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(36)
        font = self.font()
        font.setPointSize(13)
        font.setBold(True)
        self.setFont(font)
        self.set_status("idle")

    def set_status(self, status: str) -> None:
        """Update the label text and stylesheet to reflect the given status.

        Args:
            status: One of ``"idle"``, ``"recording"``, ``"playing"``, or ``"saved"``.
                Unknown values fall back to the idle style.
        """
        text, color, bg = self.STYLES.get(status, self.STYLES["idle"])
        self.setText(text)
        self.setStyleSheet(
            f"QLabel {{ color: {color}; background-color: {bg}; "
            f"border: 1px solid {color}; border-radius: 6px; padding: 4px 12px; }}"
        )


# ─── Hauptfenster ──────────────────────────────────────────────────────────


class RecorderWindow(QMainWindow):
    """Main window for the Piper TTS dataset recorder.

    Loads sentences from a LJSpeech-format metadata.csv file, displays them in a table,
    and allows the user to record each sentence as a WAV file by holding the spacebar.
    Provides a VU meter, waveform display, playback, and progress tracking.
    """

    def __init__(
        self, metadata_path: str, output_dir: str, samplerate: int = 22050, channels: int = 1, device: int | None = None
    ) -> None:
        """Initialise the recorder window and build the UI.

        Args:
            metadata_path: Path to the metadata.csv file in LJSpeech pipe-delimited format.
            output_dir: Directory where recorded WAV files will be saved.
            samplerate: Sample rate in Hz used for recording (default: 22050).
            channels: Number of audio channels to record (1 = mono).
            device: sounddevice input device index, or None for the system default.
        """
        super().__init__()

        self.metadata_path = metadata_path
        self.output_dir = output_dir
        self.samplerate = samplerate
        self.channels = channels
        self.device = device

        self.entries: list[dict[str, str]] = []
        self.current_row = -1
        self.current_audio: np.ndarray | None = None
        self.is_recording = False
        self.is_playing = False

        self.record_thread: RecordThread | None = None
        self.play_thread: PlayThread | None = None

        self._load_metadata()
        self._init_ui()
        self._apply_dark_theme()
        self._update_stats()

        # Capture Space/Return/P globally so table focus doesn't swallow them
        QApplication.instance().installEventFilter(self)  # type: ignore[union-attr]

        # Erste Zeile auswählen
        if self.entries:
            self._select_first_unrecorded()

    # ── Metadata laden ──────────────────────────────────────────────────

    def _load_metadata(self) -> None:
        """Parse the metadata.csv file and populate self.entries.

        Each entry is a dict with keys ``filename``, ``raw``, and ``normalized``.
        Lines that do not contain at least three pipe-delimited fields are skipped.
        """
        self.entries = []
        if not os.path.isfile(self.metadata_path):
            return
        with open(self.metadata_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split("|")
                if len(parts) >= 3:
                    self.entries.append(
                        {
                            "filename": parts[0],
                            "raw": parts[1],
                            "normalized": parts[2],
                        }
                    )

    # ── UI aufbauen ─────────────────────────────────────────────────────

    def _init_ui(self) -> None:
        """Build and arrange all widgets: progress bar, sentence table, recording controls, and device selector."""
        self.setWindowTitle("Piper TTS Dataset Recorder")
        self.setMinimumSize(1000, 750)
        self.resize(1200, 850)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(12, 12, 12, 12)

        # ── Top: Fortschritt ────────────────────────────────────────────
        progress_group = QGroupBox("Fortschritt")
        progress_layout = QVBoxLayout(progress_group)

        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(len(self.entries))
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setMinimumHeight(28)
        progress_layout.addWidget(self.progress_bar)

        self.stats_label = QLabel()
        self.stats_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress_layout.addWidget(self.stats_label)

        main_layout.addWidget(progress_group)

        # ── Splitter: Tabelle + Aufnahmebereich ─────────────────────────
        splitter = QSplitter(Qt.Orientation.Vertical)

        # ── Tabelle ─────────────────────────────────────────────────────
        table_widget = QWidget()
        table_layout = QVBoxLayout(table_widget)
        table_layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["#", "Dateiname", "Text (normalisiert)", "Status"])
        self.table.setRowCount(len(self.entries))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)

        # Spaltenbreiten
        header = self.table.horizontalHeader()
        header.resizeSection(0, 45)
        header.resizeSection(1, 120)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.resizeSection(3, 90)

        # Tabelle befüllen
        for i, entry in enumerate(self.entries):
            wav_path = os.path.join(self.output_dir, entry["filename"] + ".wav")
            exists = os.path.isfile(wav_path)

            num_item = QTableWidgetItem(str(i + 1))
            num_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 0, num_item)

            self.table.setItem(i, 1, QTableWidgetItem(entry["filename"]))
            self.table.setItem(i, 2, QTableWidgetItem(entry["normalized"]))

            status_item = QTableWidgetItem("✓" if exists else "—")
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if exists:
                status_item.setForeground(QColor(80, 200, 80))
            else:
                status_item.setForeground(QColor(120, 120, 120))
            self.table.setItem(i, 3, status_item)

        self.table.currentCellChanged.connect(self._on_row_changed)
        table_layout.addWidget(self.table)
        splitter.addWidget(table_widget)

        # ── Aufnahmebereich ─────────────────────────────────────────────
        record_widget = QWidget()
        record_layout = QVBoxLayout(record_widget)
        record_layout.setSpacing(10)

        # Aktueller Satz
        sentence_group = QGroupBox("Aktueller Satz")
        sentence_layout = QVBoxLayout(sentence_group)

        self.sentence_label = QLabel("Wähle einen Satz aus der Tabelle")
        self.sentence_label.setWordWrap(True)
        self.sentence_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.sentence_label.setMinimumHeight(50)
        font = self.sentence_label.font()
        font.setPointSize(16)
        self.sentence_label.setFont(font)
        self.sentence_label.setStyleSheet(
            "QLabel { color: #ffcc00; padding: 12px; " "background-color: #1a1a2e; border-radius: 8px; }"
        )
        sentence_layout.addWidget(self.sentence_label)

        # Raw-Text klein darunter
        self.raw_label = QLabel("")
        self.raw_label.setWordWrap(True)
        self.raw_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.raw_label.setStyleSheet("QLabel { color: #888888; font-size: 10pt; }")
        sentence_layout.addWidget(self.raw_label)

        record_layout.addWidget(sentence_group)

        # Status + VU-Meter + Wellenform
        viz_layout = QHBoxLayout()

        # Links: Status + Buttons
        left_layout = QVBoxLayout()

        self.status_indicator = StatusIndicator()
        left_layout.addWidget(self.status_indicator)

        # Buttons
        btn_layout = QHBoxLayout()

        self.btn_record = QPushButton("⏺  Aufnehmen")
        self.btn_record.setMinimumHeight(48)
        self.btn_record.setStyleSheet(
            "QPushButton { background-color: #cc3333; color: white; "
            "font-size: 13pt; font-weight: bold; border-radius: 8px; padding: 8px 20px; }"
            "QPushButton:hover { background-color: #ee4444; }"
            "QPushButton:disabled { background-color: #553333; color: #888; }"
        )
        self.btn_record.setToolTip("Leertaste gedrückt halten zum Aufnehmen")
        btn_layout.addWidget(self.btn_record)

        self.btn_play = QPushButton("▶  Abspielen")
        self.btn_play.setMinimumHeight(48)
        self.btn_play.setEnabled(False)
        self.btn_play.setStyleSheet(
            "QPushButton { background-color: #2266aa; color: white; "
            "font-size: 13pt; font-weight: bold; border-radius: 8px; padding: 8px 20px; }"
            "QPushButton:hover { background-color: #3377cc; }"
            "QPushButton:disabled { background-color: #223355; color: #888; }"
        )
        self.btn_play.clicked.connect(self._play_current)
        btn_layout.addWidget(self.btn_play)

        self.btn_next = QPushButton("→  Weiter")
        self.btn_next.setMinimumHeight(48)
        self.btn_next.setStyleSheet(
            "QPushButton { background-color: #227744; color: white; "
            "font-size: 13pt; font-weight: bold; border-radius: 8px; padding: 8px 20px; }"
            "QPushButton:hover { background-color: #339955; }"
            "QPushButton:disabled { background-color: #223333; color: #888; }"
        )
        self.btn_next.clicked.connect(self._next_row)
        btn_layout.addWidget(self.btn_next)

        left_layout.addLayout(btn_layout)

        # Hinweis
        hint_label = QLabel("Leertaste gedrückt halten = Aufnehmen  |  Leertaste loslassen = Stoppen")
        hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_label.setStyleSheet("QLabel { color: #666; font-size: 9pt; padding: 4px; }")
        left_layout.addWidget(hint_label)

        viz_layout.addLayout(left_layout, stretch=1)

        # Rechts: VU-Meter
        vu_layout = QVBoxLayout()
        vu_label = QLabel("Pegel")
        vu_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vu_label.setStyleSheet("QLabel { color: #aaa; font-size: 9pt; }")
        vu_layout.addWidget(vu_label)

        self.vu_meter = VUMeter()
        vu_layout.addWidget(self.vu_meter)
        viz_layout.addLayout(vu_layout)

        record_layout.addLayout(viz_layout)

        # Wellenform
        wave_group = QGroupBox("Wellenform")
        wave_layout = QVBoxLayout(wave_group)
        self.waveform = WaveformWidget()
        wave_layout.addWidget(self.waveform)
        record_layout.addWidget(wave_group)

        splitter.addWidget(record_widget)

        # Splitter-Verhältnis
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        main_layout.addWidget(splitter)

        # ── Audio-Device Auswahl (Bottom) ───────────────────────────────
        device_layout = QHBoxLayout()
        device_layout.addWidget(QLabel("Eingabegerät:"))

        self.device_combo = QComboBox()
        self._populate_devices()
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        device_layout.addWidget(self.device_combo, stretch=1)

        main_layout.addLayout(device_layout)

    # ── Dark Theme ──────────────────────────────────────────────────────

    def _apply_dark_theme(self) -> None:
        """Apply a Catppuccin Mocha-inspired dark stylesheet to the main window."""
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #1e1e2e;
                color: #cdd6f4;
            }
            QGroupBox {
                font-weight: bold;
                font-size: 11pt;
                border: 1px solid #45475a;
                border-radius: 8px;
                margin-top: 8px;
                padding-top: 16px;
                color: #cdd6f4;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QTableWidget {
                background-color: #181825;
                alternate-background-color: #1e1e2e;
                color: #cdd6f4;
                gridline-color: #313244;
                border: 1px solid #45475a;
                border-radius: 6px;
                font-size: 10pt;
            }
            QTableWidget::item:selected {
                background-color: #3b3b5c;
                color: #ffffff;
            }
            QHeaderView::section {
                background-color: #313244;
                color: #cdd6f4;
                padding: 6px;
                border: none;
                border-bottom: 1px solid #45475a;
                font-weight: bold;
            }
            QProgressBar {
                border: 1px solid #45475a;
                border-radius: 6px;
                text-align: center;
                color: #cdd6f4;
                background-color: #181825;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: #89b4fa;
                border-radius: 5px;
            }
            QComboBox {
                background-color: #313244;
                color: #cdd6f4;
                border: 1px solid #45475a;
                border-radius: 4px;
                padding: 4px 8px;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox QAbstractItemView {
                background-color: #313244;
                color: #cdd6f4;
                selection-background-color: #45475a;
            }
            QLabel {
                color: #cdd6f4;
            }
            QSplitter::handle {
                background-color: #45475a;
                height: 3px;
            }
        """)

    # ── Audio-Geräte ────────────────────────────────────────────────────

    def _populate_devices(self) -> None:
        """Populate the input-device combo box with all devices that have at least one input channel."""
        self.device_combo.clear()
        devices = sd.query_devices()
        default_idx = sd.default.device[0]

        self.device_combo.addItem(f"System-Standard (#{default_idx})", None)

        for i, dev in enumerate(devices):
            if dev["max_input_channels"] > 0:
                label = f"[{i}] {dev['name']} ({dev['max_input_channels']}ch)"
                self.device_combo.addItem(label, i)
                if self.device is not None and i == self.device:
                    self.device_combo.setCurrentIndex(self.device_combo.count() - 1)

    def _on_device_changed(self, index: int) -> None:
        """Slot: update the active device index when the combo box selection changes.

        Args:
            index: The new combo box index (unused; the device ID is read from item data).
        """
        self.device = self.device_combo.currentData()

    # ── Statistiken ─────────────────────────────────────────────────────

    def _update_stats(self) -> None:
        """Recount recorded WAV files and refresh the progress bar and stats label."""
        total = len(self.entries)
        recorded = 0
        for entry in self.entries:
            wav_path = os.path.join(self.output_dir, entry["filename"] + ".wav")
            if os.path.isfile(wav_path):
                recorded += 1

        self.progress_bar.setValue(recorded)
        pct = (recorded / total * 100) if total > 0 else 0
        self.progress_bar.setFormat(f"{recorded} / {total}  ({pct:.0f}%)")
        self.stats_label.setText(f"{recorded} aufgenommen  ·  {total - recorded} verbleibend  ·  {total} gesamt")

    # ── Tabellen-Navigation ─────────────────────────────────────────────

    def _select_first_unrecorded(self) -> None:
        """Select and scroll to the first table row whose WAV file has not yet been recorded.

        Falls back to selecting row 0 when all entries are already recorded.
        """
        for i, entry in enumerate(self.entries):
            wav_path = os.path.join(self.output_dir, entry["filename"] + ".wav")
            if not os.path.isfile(wav_path):
                self.table.selectRow(i)
                item = self.table.item(i, 0)
                if item is not None:
                    self.table.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
                return
        # Alle aufgenommen -> erste Zeile
        if self.entries:
            self.table.selectRow(0)

    def _on_row_changed(self, row: int, col: int, prev_row: int, prev_col: int) -> None:
        """Slot: update the sentence display and waveform when the selected table row changes.

        If a WAV file already exists for the newly selected entry, it is loaded into the
        waveform widget and playback is enabled; otherwise the waveform is cleared.

        Args:
            row: Newly selected row index.
            col: Newly selected column index (unused).
            prev_row: Previously selected row index (unused).
            prev_col: Previously selected column index (unused).
        """
        if row < 0 or row >= len(self.entries):
            return

        self.current_row = row
        entry = self.entries[row]
        wav_path = os.path.join(self.output_dir, entry["filename"] + ".wav")

        self.sentence_label.setText(entry["normalized"])
        self.raw_label.setText(f"Roh: {entry['raw']}")

        # Wellenform laden wenn Datei existiert
        if os.path.isfile(wav_path):
            data, sr = sf.read(wav_path, dtype="int16")
            self.waveform.set_audio(data)
            self.btn_play.setEnabled(True)
            self.current_audio = data
        else:
            self.waveform.clear()
            self.btn_play.setEnabled(False)
            self.current_audio = None

        self.status_indicator.set_status("idle")
        self.vu_meter.reset()

    def _next_row(self) -> None:
        """Advance the table selection to the next row and scroll it into view."""
        if self.current_row < len(self.entries) - 1:
            next_row = self.current_row + 1
            self.table.selectRow(next_row)
            item = self.table.item(next_row, 0)
            if item is not None:
                self.table.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)

    def _update_table_status(self, row: int, recorded: bool) -> None:
        """Update the status cell of a table row to reflect whether the WAV file exists.

        Args:
            row: Table row index to update.
            recorded: True to show a green check mark; False to show a grey dash.
        """
        status_item = self.table.item(row, 3)
        if status_item:
            status_item.setText("✓" if recorded else "—")
            status_item.setForeground(QColor(80, 200, 80) if recorded else QColor(120, 120, 120))

    # ── Aufnahme ────────────────────────────────────────────────────────

    def _start_recording(self) -> None:
        """Start a new recording for the currently selected entry.

        No-op if a recording or playback is already in progress, or if no row is selected.
        Creates and starts a RecordThread connected to the VU meter and the finished callback.
        """
        if self.is_recording or self.is_playing:
            return
        if self.current_row < 0:
            return

        self.is_recording = True
        self.status_indicator.set_status("recording")
        self.btn_record.setText("⏺  Aufnahme läuft...")
        self.btn_record.setEnabled(False)
        self.btn_play.setEnabled(False)
        self.btn_next.setEnabled(False)
        self.waveform.clear()

        self.record_thread = RecordThread(
            samplerate=self.samplerate,
            channels=self.channels,
            device=self.device,
        )
        self.record_thread.level_update.connect(self.vu_meter.set_level)
        self.record_thread.finished_recording.connect(self._on_recording_finished)
        self.record_thread.start()

    def _stop_recording(self) -> None:
        """Stop the active recording thread if one is running."""
        if not self.is_recording:
            return
        if self.record_thread:
            self.record_thread.stop()

    def _on_recording_finished(self, audio: np.ndarray) -> None:
        """Slot: handle the completed recording, save it to disk, and update the UI.

        Saves the audio as a 16-bit PCM WAV file in the output directory, updates the
        waveform widget, table status, and progress bar. Silently ignores empty recordings.

        Args:
            audio: int16 numpy array of the recorded PCM samples.
        """
        self.is_recording = False
        self.btn_record.setText("⏺  Aufnehmen")
        self.btn_record.setEnabled(True)
        self.btn_next.setEnabled(True)

        if len(audio) == 0:
            self.status_indicator.set_status("idle")
            return

        entry = self.entries[self.current_row]
        wav_path = os.path.join(self.output_dir, entry["filename"] + ".wav")

        # Speichern
        os.makedirs(self.output_dir, exist_ok=True)
        sf.write(wav_path, audio, self.samplerate, subtype="PCM_16")

        self.current_audio = audio
        self.waveform.set_audio(audio)
        self.btn_play.setEnabled(True)
        self._update_table_status(self.current_row, True)
        self._update_stats()
        self.status_indicator.set_status("saved")
        self.vu_meter.reset()

    # ── Wiedergabe ──────────────────────────────────────────────────────

    def _play_current(self) -> None:
        """Start playback of the WAV file for the currently selected entry.

        No-op if playback or recording is already active, or if no WAV file exists yet.
        Creates and starts a PlayThread connected to the waveform position and finished callback.
        """
        if self.is_playing or self.is_recording:
            return
        if self.current_row < 0:
            return

        entry = self.entries[self.current_row]
        wav_path = os.path.join(self.output_dir, entry["filename"] + ".wav")

        if not os.path.isfile(wav_path):
            return

        self.is_playing = True
        self.status_indicator.set_status("playing")
        self.btn_play.setText("■  Stopp")
        self.btn_play.setStyleSheet(
            "QPushButton { background-color: #aa6622; color: white; "
            "font-size: 13pt; font-weight: bold; border-radius: 8px; padding: 8px 20px; }"
        )
        self.btn_record.setEnabled(False)
        self.btn_play.clicked.disconnect()
        self.btn_play.clicked.connect(self._stop_playback)

        self.play_thread = PlayThread(wav_path)
        self.play_thread.position_update.connect(self.waveform.set_playback_pos)
        self.play_thread.playback_finished.connect(self._on_playback_finished)
        self.play_thread.start()

    def _stop_playback(self) -> None:
        """Stop the active playback thread if one is running."""
        if self.play_thread:
            self.play_thread.stop()

    def _on_playback_finished(self) -> None:
        """Slot: restore UI state after playback ends (re-enable record, reset play button)."""
        self.is_playing = False
        self.status_indicator.set_status("idle")
        self.btn_play.setText("▶  Abspielen")
        self.btn_play.setStyleSheet(
            "QPushButton { background-color: #2266aa; color: white; "
            "font-size: 13pt; font-weight: bold; border-radius: 8px; padding: 8px 20px; }"
            "QPushButton:hover { background-color: #3377cc; }"
            "QPushButton:disabled { background-color: #223355; color: #888; }"
        )
        self.btn_record.setEnabled(True)
        self.btn_play.clicked.disconnect()
        self.btn_play.clicked.connect(self._play_current)
        self.waveform.set_playback_pos(0.0)

    # ── Tastatur-Handling ───────────────────────────────────────────────

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """Application-wide event filter for recording hotkeys.

        Intercepts Space (hold-to-record), Return (next row), and P (playback)
        regardless of which widget currently has focus.
        """
        if event.type() == QEvent.Type.KeyPress:
            key_event = cast(QKeyEvent, event)
            if key_event.isAutoRepeat():
                return False
            if key_event.key() == Qt.Key.Key_Space:
                self._start_recording()
                return True
            if key_event.key() == Qt.Key.Key_Return:
                if not self.is_recording and not self.is_playing:
                    self._next_row()
                return True
            if key_event.key() == Qt.Key.Key_P:
                if not self.is_recording:
                    self._play_current()
                return True
        elif event.type() == QEvent.Type.KeyRelease:
            key_event_rel = cast(QKeyEvent, event)
            if key_event_rel.isAutoRepeat():
                return False
            if key_event_rel.key() == Qt.Key.Key_Space:
                self._stop_recording()
                return True
        return super().eventFilter(obj, event)

    # ── Cleanup ─────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:
        """Gracefully stop any running threads before closing the window.

        Waits up to 2 seconds each for the record and play threads to finish.

        Args:
            event: The Qt close event to accept after cleanup.
        """
        if self.record_thread and self.record_thread.isRunning():
            self.record_thread.stop()
            self.record_thread.wait(2000)
        if self.play_thread and self.play_thread.isRunning():
            self.play_thread.stop()
            self.play_thread.wait(2000)
        event.accept()


# ─── Main ──────────────────────────────────────────────────────────────────


def main() -> None:
    """Parse CLI arguments and launch the PySide6 dataset recorder GUI."""
    parser = argparse.ArgumentParser(
        description="Piper TTS Dataset Recorder – PySide6 GUI",
    )
    parser.add_argument("--metadata", type=str, required=True, help="Pfad zur metadata.csv")
    parser.add_argument(
        "--output", type=str, default="./wavs.local", help="Ausgabeordner für WAV-Dateien (default: ./wavs.local)"
    )
    parser.add_argument("--samplerate", type=int, default=22050, help="Samplerate in Hz (default: 22050)")
    parser.add_argument("--channels", type=int, default=1, help="Kanäle (default: 1)")
    parser.add_argument("--device", type=int, default=None, help="Audio-Eingabegerät ID")

    args = parser.parse_args()

    if not os.path.isfile(args.metadata):
        parser.error(f"Metadata-Datei nicht gefunden: {args.metadata}")

    os.makedirs(args.output, exist_ok=True)

    app = QApplication(sys.argv)
    app.setApplicationName("Piper TTS Recorder")

    window = RecorderWindow(
        metadata_path=args.metadata,
        output_dir=args.output,
        samplerate=args.samplerate,
        channels=args.channels,
        device=args.device,
    )
    window.show()

    # Allow Ctrl+C to terminate the Qt application from the terminal
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    timer = QTimer()
    timer.timeout.connect(lambda: None)  # Let Python process signals periodically
    timer.start(200)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
