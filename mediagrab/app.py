"""MediaGrab GUI. One codebase, two products:
  video  -> "MediaGrab Video Downloader"  (videos up to 8K, playlists, subtitles, audio-only)
  audio  -> "MediaGrab Video to MP3"      (audio from links and from local video files)
"""
from __future__ import annotations

import dataclasses
import itertools
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QDesktopServices, QFontMetrics, QGuiApplication,
                           QKeySequence, QPainter, QPainterPath, QPixmap, QShortcut)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                               QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
                               QTabWidget, QToolButton, QVBoxLayout, QWidget)

from . import engine, theme
from .config import (AUDIO_BITRATES, AUDIO_FORMATS, CACHE_DIR, COOKIE_BROWSERS, VIDEO_QUALITIES,
                     Settings)
from .icons import icon, pixmap, svg

PRODUCTS = {
    "video": {"name": "MediaGrab Video Downloader", "short": "Video Downloader", "icon": "video",
              "accent": "#4f8cff", "accent_hi": "#6b9fff",
              "hint": "Copy a link from YouTube, Vimeo, TikTok, X, Instagram, SoundCloud or 1,800+ other "
                      "sites, then press Paste Link."},
    "audio": {"name": "MediaGrab Video to MP3", "short": "Video to MP3", "icon": "music",
              "accent": "#22c55e", "accent_hi": "#3ad472",
              "hint": "Paste a link from YouTube or 1,800+ sites to save its audio, or drop video files "
                      "here to convert them to MP3."},
}
URL_RE = re.compile(r"https?://\S+", re.I)
MEDIA_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".wmv", ".m4v", ".mpg", ".mpeg", ".ts",
             ".3gp", ".ogv", ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac", ".wma"}
HEIGHT_TAG = {4320: "8K", 2160: "4K", 1440: "2K", 1080: "Full HD", 720: "HD"}
_ids = itertools.count(1)


# ------------------------------------------------------------------ formatting

def fmt_size(n) -> str:
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def fmt_time(sec) -> str:
    if sec is None:
        return ""
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def open_path(p: str):
    QDesktopServices.openUrl(QUrl.fromLocalFile(p))


def show_in_folder(p: str):
    path = Path(p)
    folder = path if path.is_dir() else path.parent
    # Thunar/Nautilus can select the file through the FileManager1 D-Bus interface.
    if path.is_file():
        r = subprocess.run(["dbus-send", "--session", "--dest=org.freedesktop.FileManager1",
                            "--type=method_call", "/org/freedesktop/FileManager1",
                            "org.freedesktop.FileManager1.ShowItems",
                            f"array:string:{QUrl.fromLocalFile(str(path)).toString()}", "string:"],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            return
    open_path(str(folder))


def rounded(px: QPixmap, w: int, h: int, radius: int, ratio: float) -> QPixmap:
    """Scale-to-fill and crop into a rounded rectangle."""
    W, H = int(w * ratio), int(h * ratio)
    src = px.scaled(W, H, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    out = QPixmap(W, H)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, W, H, radius * ratio, radius * ratio)
    p.setClipPath(path)
    p.drawPixmap((W - src.width()) // 2, (H - src.height()) // 2, src)
    p.end()
    out.setDevicePixelRatio(ratio)
    return out


# ------------------------------------------------------------------ jobs

class Job:
    FIELDS = ("id", "kind", "url", "path", "title", "site", "thumb", "opts", "status", "fraction",
              "result", "error", "size", "added", "label", "partials")

    def __init__(self, **kw):
        self.id = next(_ids)
        self.kind = "url"          # url | file
        self.url = ""
        self.path = ""
        self.title = ""
        self.site = ""
        self.thumb = None
        self.opts = {}
        self.status = "queued"     # analyzing | queued | running | paused | done | error
        self.fraction = 0.0
        self.result = ""
        self.error = ""
        self.size = 0
        self.added = time.time()
        self.label = ""            # quality badge, e.g. "1080p MP4"
        self.stop_reason = None    # None | "pause" | "remove"
        self.partials = []         # files a paused download left (deleted if the job is removed)
        self.thread = None
        for k, v in kw.items():
            setattr(self, k, v)

    def to_dict(self):
        d = {k: getattr(self, k) for k in self.FIELDS if k != "id"}
        if d["status"] in ("queued", "running", "analyzing"):
            d["status"] = "paused"
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: v for k, v in d.items() if k in cls.FIELDS and k != "id"})


def job_label(opts: dict) -> str:
    if opts.get("mode") == "audio":
        fmt = opts.get("audio_format", "mp3").upper()
        return fmt if fmt in ("FLAC", "WAV") else f"{fmt} {opts.get('bitrate', '320')}k"
    q = opts.get("quality")
    return f"{q}p {opts.get('container', 'mp4').upper()}" if q else opts.get("container", "mp4").upper()


class Bridge(QObject):
    progress = Signal(int, object)
    done = Signal(int, str)
    failed = Signal(int, str)
    stopped = Signal(int)
    probed = Signal(int, object)
    probe_failed = Signal(int, str)
    thumb = Signal(int, str)


# ------------------------------------------------------------------ widgets

class ElideLabel(QLabel):
    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full if len(self._full) > 40 else "")
        self._elide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        super().setText(QFontMetrics(self.font()).elidedText(self._full, Qt.ElideRight, max(self.width(), 40)))


def tool(name, tip, cb, color=theme.MUTED):
    b = QToolButton()
    b.setIcon(icon(name, color))
    b.setIconSize(QSize(18, 18))
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    b.clicked.connect(cb)
    return b


class Thumb(QLabel):
    W, H = 136, 77

    def __init__(self, fallback_icon: str):
        super().__init__()
        self.setObjectName("Thumb")
        self.setFixedSize(self.W, self.H)
        self.setAlignment(Qt.AlignCenter)
        self.fallback = fallback_icon
        self.set_path(None)

    def set_path(self, path):
        ratio = self.devicePixelRatioF()
        px = QPixmap(path) if path else QPixmap()
        if px.isNull():
            self.setPixmap(pixmap(self.fallback, 28, theme.FAINT, ratio))
        else:
            self.setPixmap(rounded(px, self.W, self.H, 8, ratio))


class Row(QFrame):
    def __init__(self, job: Job, win: "MainWindow"):
        super().__init__()
        self.job, self.win = job, win
        self.setObjectName("Row")
        self.setAttribute(Qt.WA_StyledBackground)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(14)
        self.thumb = Thumb(PRODUCTS[win.product]["icon"] if job.kind == "url" else "file-plus")
        lay.addWidget(self.thumb)

        mid = QWidget()
        mid.setObjectName("RowMid")
        ml = QVBoxLayout(mid)
        ml.setContentsMargins(0, 2, 0, 2)
        ml.setSpacing(5)
        self.title = ElideLabel()
        self.title.setObjectName("RowTitle")
        ml.addWidget(self.title)
        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.badge = QLabel()
        self.badge.setObjectName("Badge")
        self.meta = ElideLabel()
        self.meta.setObjectName("Muted")
        meta.addWidget(self.badge)
        meta.addWidget(self.meta, 1)
        ml.addLayout(meta)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1000)
        ml.addWidget(self.bar)
        self.status = ElideLabel()
        self.status.setObjectName("Faint")
        ml.addWidget(self.status)
        lay.addWidget(mid, 1)

        self.btns = QHBoxLayout()
        self.btns.setSpacing(2)
        self.b_pause = tool("pause", "Pause", lambda: win.pause(job))
        self.b_resume = tool("play", "Resume", lambda: win.resume(job), theme.TEXT)
        self.b_retry = tool("retry", "Try again", lambda: win.resume(job), theme.TEXT)
        self.b_play = tool("play", "Open file", lambda: open_path(job.result), theme.TEXT)
        self.b_folder = tool("folder", "Show in folder", lambda: show_in_folder(job.result))
        self.b_more = tool("more", "More", self.menu)
        self.b_remove = tool("x", "Remove from list", lambda: win.remove(job))
        for b in (self.b_pause, self.b_resume, self.b_retry, self.b_play, self.b_folder, self.b_more,
                  self.b_remove):
            self.btns.addWidget(b)
        lay.addLayout(self.btns)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda _p: self.menu())
        self.refresh()

    def enterEvent(self, e):
        self.setProperty("hover", True)
        self.style().polish(self)

    def leaveEvent(self, e):
        self.setProperty("hover", False)
        self.style().polish(self)

    def mouseDoubleClickEvent(self, e):
        if self.job.status == "done" and self.job.result:
            open_path(self.job.result)

    def menu(self):
        j, m = self.job, QMenu(self)
        if j.status == "done":
            m.addAction(icon("play"), "Open file", lambda: open_path(j.result))
            m.addAction(icon("folder"), "Show in folder", lambda: show_in_folder(j.result))
        if j.url:
            m.addAction(icon("link"), "Copy link", lambda: QGuiApplication.clipboard().setText(j.url))
            m.addAction(icon("link"), "Open page in browser", lambda: QDesktopServices.openUrl(QUrl(j.url)))
        if j.status in ("error", "paused", "done"):
            m.addAction(icon("retry"), "Download again" if j.status == "done" else "Retry",
                        lambda: self.win.resume(j, again=j.status == "done"))
        m.addSeparator()
        m.addAction(icon("x"), "Remove from list", lambda: self.win.remove(j))
        if j.status == "done" and j.result and Path(j.result).is_file():
            m.addAction(icon("trash", theme.ERR), "Delete file…", lambda: self.win.delete_file(j))
        m.exec(self.cursor().pos())

    def set_thumb(self, path):
        self.thumb.set_path(path)

    def refresh(self, prog: dict | None = None):
        j = self.job
        self.title.setText(j.title or j.url or Path(j.path).name)
        self.badge.setText(j.label or "")
        self.badge.setVisible(bool(j.label))
        meta = [j.site] if j.site else []
        if j.size:
            meta.append(fmt_size(j.size))
        self.meta.setText("  ·  ".join(meta))
        st = j.status
        vis = {"pause": st in ("queued", "running"), "resume": st == "paused", "retry": st == "error",
               "play": st == "done", "folder": st == "done", "more": st != "analyzing"}
        self.b_pause.setVisible(vis["pause"])
        self.b_resume.setVisible(vis["resume"])
        self.b_retry.setVisible(vis["retry"])
        self.b_play.setVisible(vis["play"])
        self.b_folder.setVisible(vis["folder"])
        self.b_more.setVisible(vis["more"])
        self.bar.setVisible(st not in ("done", "error"))
        self.bar.setProperty("state", "paused" if st in ("paused", "queued") else "")
        self.bar.style().polish(self.bar)
        color = theme.FAINT
        if st == "analyzing":
            self.bar.setRange(0, 0)
            text = "Analyzing link…"
        elif st == "queued":
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(j.fraction * 1000))
            text = "Waiting in queue…"
        elif st == "running":
            p = prog or {}
            frac = p.get("fraction")
            if frac is None:
                self.bar.setRange(0, 0)
            else:
                self.bar.setRange(0, 1000)
                self.bar.setValue(int(frac * 1000))
            stage = p.get("stage", "Starting")
            parts = [stage]
            if stage in ("Downloading",):
                got, tot = p.get("downloaded"), p.get("total")
                if got and tot:
                    parts = [f"{fmt_size(got)} of {fmt_size(tot)}"]
                elif got:
                    parts = [fmt_size(got)]
                if p.get("speed"):
                    parts.append(f"{fmt_size(p['speed'])}/s")
                if p.get("eta") is not None:
                    parts.append(f"{fmt_time(p['eta'])} left")
            elif stage == "Converting" and frac is not None:
                parts = [f"Converting  {frac * 100:.0f}%"]
                if p.get("speed_x"):
                    parts.append(f"{p['speed_x']:.0f}× speed")
                if p.get("eta") is not None:
                    parts.append(f"{fmt_time(p['eta'])} left")
            else:
                parts[0] = stage + "…"
            text = "  ·  ".join(parts)
            color = theme.MUTED
        elif st == "paused":
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(j.fraction * 1000))
            text = f"Paused  ·  {j.fraction * 100:.0f}%" if j.fraction else "Paused"
        elif st == "done":
            text = "✓  Done  ·  " + Path(j.result).name if j.result else "✓  Done"
            color = theme.OK
        else:
            text = "⚠  " + (j.error or "Failed")
            color = theme.ERR
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)
        self.status.setToolTip(text if st == "error" else "")


# ------------------------------------------------------------------ dialogs

def no_auto_default(dlg: QDialog, keep: QPushButton):
    """Return should always press the dialog's main button, not whichever button has focus."""
    for b in dlg.findChildren(QPushButton):
        if b is not keep:
            b.setAutoDefault(False)
    for w in dlg.findChildren(QComboBox) + dlg.findChildren(QCheckBox):
        w.installEventFilter(dlg)   # these swallow Return; hand it to the dialog instead
    dlg._main_button = keep
    QTimer.singleShot(0, keep.setFocus)


class ReturnDialog(QDialog):
    def eventFilter(self, obj, e):
        if e.type() == e.Type.KeyPress and e.key() in (Qt.Key_Return, Qt.Key_Enter) \
                and not (isinstance(obj, QComboBox) and obj.view().isVisible()):
            self.keyPressEvent(e)
            return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and getattr(self, "_main_button", None):
            self._main_button.click()
            return
        super().keyPressEvent(e)


class OptionButton(QPushButton):
    def __init__(self, left: str, right: str = ""):
        super().__init__()
        self.setObjectName("Option")
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 12, 0)
        a, b = QLabel(left), QLabel(right)
        a.setStyleSheet("background: transparent; font-weight: 600;")
        b.setStyleSheet(f"background: transparent; color: {theme.MUTED};")
        for w in (a, b):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(a)
        lay.addStretch()
        lay.addWidget(b)
        self.setMinimumHeight(42)


def folder_picker(parent, line: QLineEdit) -> QWidget:
    w = QWidget()
    w.setStyleSheet("background: transparent;")
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(line, 1)
    b = QPushButton("Browse…")

    def pick():
        d = QFileDialog.getExistingDirectory(parent, "Choose folder", line.text())
        if d:
            line.setText(d)
    b.clicked.connect(pick)
    h.addWidget(b)
    return w


class OptionsDialog(ReturnDialog):
    """Shown after a link is analysed (or files are added) unless Smart Mode is on."""

    def __init__(self, win: "MainWindow", info: dict):
        super().__init__(win)
        self.win, self.info, self.s = win, info, win.s
        self.product = win.product
        kind = info["kind"]
        self.setWindowTitle({"video": "Download", "playlist": "Download playlist",
                             "files": "Convert files"}[kind])
        self.setMinimumWidth(640)
        v = QVBoxLayout(self)
        v.setContentsMargins(22, 22, 22, 18)
        v.setSpacing(14)

        # header
        hdr = QHBoxLayout()
        hdr.setSpacing(16)
        th = QLabel()
        ratio = self.devicePixelRatioF()
        px = QPixmap(info.get("thumb_path") or "")
        th.setFixedSize(192, 108)
        if px.isNull():
            th.setPixmap(pixmap("file-plus" if kind == "files" else PRODUCTS[self.product]["icon"],
                                40, theme.FAINT, ratio))
            th.setAlignment(Qt.AlignCenter)
            th.setStyleSheet("background: #0c0e12; border-radius: 10px;")
        else:
            th.setPixmap(rounded(px, 192, 108, 10, ratio))
        hdr.addWidget(th, 0, Qt.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(6)
        t = QLabel(info.get("title", ""))
        t.setWordWrap(True)
        t.setStyleSheet("font-size: 12pt; font-weight: 700;")
        col.addWidget(t)
        bits = [b for b in (info.get("uploader"), info.get("site")) if b]
        if kind == "video" and info.get("duration"):
            bits.append(fmt_time(info["duration"]))
        if kind == "playlist":
            bits.append(f"{len(info['entries'])} items")
        if kind == "files":
            n = len(info["files"])
            bits.append(f"{n} file" + ("s" if n > 1 else ""))
            if info.get("duration"):
                bits.append(fmt_time(info["duration"]))
        sub = QLabel("  ·  ".join(bits))
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        col.addWidget(sub)
        col.addStretch()
        hdr.addLayout(col, 1)
        v.addLayout(hdr)

        # playlist picker
        self.entry_list = None
        if kind == "playlist":
            row = QHBoxLayout()
            row.addWidget(self._section("ITEMS"))
            row.addStretch()
            self.sel_all = QCheckBox("Select all")
            self.sel_all.setChecked(True)
            row.addWidget(self.sel_all)
            v.addLayout(row)
            self.entry_list = QListWidget()
            self.entry_list.setMinimumHeight(150)
            for i, e in enumerate(info["entries"], 1):
                it = QListWidgetItem(f"{i:>3}.  {e['title']}" + (f"   ({fmt_time(e['duration'])})"
                                                                 if e.get("duration") else ""))
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked)
                self.entry_list.addItem(it)
            self.sel_all.toggled.connect(lambda on: [self.entry_list.item(i).setCheckState(
                Qt.Checked if on else Qt.Unchecked) for i in range(self.entry_list.count())])
            v.addWidget(self.entry_list)

        # format
        self.format_box = QComboBox()
        if self.product == "video" and kind != "files":
            for key, text in (("mp4", "Video  ·  MP4"), ("mkv", "Video  ·  MKV"),
                              ("mp3", "Audio only  ·  MP3"), ("m4a", "Audio only  ·  M4A")):
                self.format_box.addItem(text, key)
            self.format_box.setCurrentIndex(max(0, self.format_box.findData(self.s.video_container)))
        else:
            for f in AUDIO_FORMATS:
                self.format_box.addItem(f.upper() + {"mp3": "  ·  works everywhere", "flac": "  ·  lossless",
                                                     "wav": "  ·  uncompressed"}.get(f, ""), f)
            self.format_box.setCurrentIndex(max(0, self.format_box.findData(self.s.audio_format)))
        frow = QHBoxLayout()
        frow.addWidget(self._section("FORMAT"))
        frow.addStretch()
        frow.addWidget(self.format_box)
        v.addLayout(frow)

        # quality grid (rebuilt when the format changes)
        v.addWidget(self._section("QUALITY"))
        self.qhost = QWidget()
        self.qhost.setStyleSheet("background: transparent;")
        self.qgrid = QGridLayout(self.qhost)
        self.qgrid.setContentsMargins(0, 0, 0, 0)
        self.qgrid.setSpacing(8)
        self.qgroup = QButtonGroup(self)
        v.addWidget(self.qhost)

        # extras
        self.subs = self.sub_lang = self.cover = None
        extras = QHBoxLayout()
        if self.product == "video" and kind != "files":
            self.subs = QCheckBox("Download subtitles")
            self.sub_lang = QComboBox()
            langs = info.get("subtitles") or (["en", "es", "pt", "fr", "de", "it", "ja", "ko", "zh"]
                                              if kind == "playlist" else [])
            for l in langs:
                self.sub_lang.addItem(l, l)
            if langs:
                self.sub_lang.setCurrentIndex(max(0, self.sub_lang.findData(self.s.subtitle_lang)))
                self.subs.setChecked(self.s.subtitles)
            else:
                self.subs.setEnabled(False)
                self.subs.setText("No subtitles available")
                self.sub_lang.setVisible(False)
            extras.addWidget(self.subs)
            extras.addWidget(self.sub_lang)
        self.cover = QCheckBox("Embed cover art and tags")
        self.cover.setChecked(self.s.embed_cover)
        extras.addWidget(self.cover)
        extras.addStretch()
        v.addLayout(extras)

        # folder
        v.addWidget(self._section("SAVE TO"))
        self.folder = QLineEdit(self.s.download_dir)
        v.addWidget(folder_picker(self, self.folder))

        self.smart = QCheckBox("Use these settings for every link from now on (Smart Mode)")
        self.smart.setObjectName("Muted")
        v.addWidget(self.smart)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.go = QPushButton("Convert" if kind == "files" else "Download")
        self.go.setObjectName("Accent")
        self.go.setIcon(icon("download", "#ffffff"))
        self.go.setDefault(True)
        self.go.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(self.go)
        v.addLayout(btns)

        self.format_box.currentIndexChanged.connect(self._build_quality)
        self._build_quality()
        no_auto_default(self, self.go)

    def _section(self, text):
        l = QLabel(text)
        l.setObjectName("SectionTitle")
        return l

    def _is_audio(self):
        return self.product == "audio" or self.info["kind"] == "files" or \
            self.format_box.currentData() in ("mp3", "m4a")

    def _build_quality(self):
        for b in self.qgroup.buttons():
            self.qgroup.removeButton(b)
            b.deleteLater()
        audio = self._is_audio()
        if self.cover:
            self.cover.setVisible(audio and self.info["kind"] != "files")
        if self.subs:
            self.subs.setVisible(not audio)
            self.sub_lang.setVisible(not audio and self.subs.isEnabled())
        opts = []
        if audio:
            fmt = self.format_box.currentData() if self.product == "audio" or self.info["kind"] == "files" \
                else self.format_box.currentData()
            if fmt in ("flac", "wav"):
                opts = [("lossless", "Original quality", "lossless" if fmt == "flac" else "")]
            else:
                dur = self.info.get("duration") or 0
                for br in AUDIO_BITRATES:
                    est = fmt_size(dur * int(br) * 125) if dur else ""
                    tag = {"320": "Best", "256": "High", "192": "Good", "160": "Medium",
                           "128": "Small"}[br]
                    opts.append((br, f"{br} kbps", f"{tag}" + (f"  ·  ~{est}" if est else "")))
            want = self.s.audio_bitrate
        else:
            heights = self.info.get("heights") if self.info["kind"] == "video" else VIDEO_QUALITIES[1:7]
            sizes = self.info.get("sizes") or {}
            if not heights:
                opts = [("0", "Original quality", "")]
            for h in heights or []:
                tag = HEIGHT_TAG.get(h, "")
                size = fmt_size(sizes.get(h))
                opts.append((str(h), f"{h}p" + (f"  {tag}" if tag else ""),
                             f"~{size}" if size else ""))
            want = str(min(heights, key=lambda h: abs(h - self.s.video_quality))) if heights else "0"
        cols = 2 if audio else (3 if len(opts) > 4 else 2)
        for i, (key, left, right) in enumerate(opts):
            b = OptionButton(left, right)
            b.setProperty("key", key)
            self.qgroup.addButton(b)
            b.setAutoDefault(False)
            self.qgrid.addWidget(b, i // cols, i % cols)
            if key == want:
                b.setChecked(True)
        if not self.qgroup.checkedButton() and self.qgroup.buttons():
            self.qgroup.buttons()[0].setChecked(True)
        self.adjustSize()

    def result_opts(self) -> dict:
        key = self.qgroup.checkedButton().property("key") if self.qgroup.checkedButton() else ""
        fmt = self.format_box.currentData()
        if self._is_audio():
            o = {"mode": "audio", "audio_format": fmt,
                 "bitrate": key if key.isdigit() else self.s.audio_bitrate,
                 "embed_cover": self.cover.isChecked()}
        else:
            o = {"mode": "video", "quality": int(key) if key.isdigit() and int(key) else 4320,
                 "container": fmt,
                 "subtitles": bool(self.subs and self.subs.isEnabled() and self.subs.isChecked()),
                 "sub_lang": self.sub_lang.currentData() if self.sub_lang else "en"}
            if not int(key or 0):
                o["quality"] = 0
        o["folder"] = self.folder.text().strip() or self.s.download_dir
        return o

    def selected_entries(self):
        if not self.entry_list:
            return None
        return [self.info["entries"][i] for i in range(self.entry_list.count())
                if self.entry_list.item(i).checkState() == Qt.Checked]

    def remember(self, o: dict):
        s = self.s
        s.download_dir = o["folder"]
        if o["mode"] == "audio":
            s.audio_format, s.audio_bitrate, s.embed_cover = o["audio_format"], o["bitrate"], o["embed_cover"]
            if self.product == "video":
                s.video_container = o["audio_format"]
        else:
            if o["quality"]:
                s.video_quality = o["quality"]
            s.video_container, s.subtitles, s.subtitle_lang = o["container"], o["subtitles"], o["sub_lang"]
        if self.smart.isChecked():
            s.smart_mode = True
        s.save()


class TestSignal(QObject):
    done = Signal(bool, str)


class SettingsDialog(ReturnDialog):
    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win, s = win, win.s
        self.setWindowTitle("Settings")
        self.setMinimumWidth(620)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 20, 20, 16)
        tabs = QTabWidget()
        v.addWidget(tabs)

        g = QWidget()
        g.setObjectName("TabPage")
        f = QFormLayout(g)
        f.setContentsMargins(18, 18, 18, 18)
        f.setSpacing(12)
        self.folder = QLineEdit(s.download_dir)
        f.addRow("Save to", folder_picker(self, self.folder))
        self.parallel = QComboBox()
        for n in range(1, 11):
            self.parallel.addItem(str(n), n)
        self.parallel.setCurrentIndex(max(0, self.parallel.findData(s.max_parallel)))
        f.addRow("Downloads at once", self.parallel)
        self.smart = QCheckBox("Smart Mode — skip the options window and use the choices below")
        self.smart.setChecked(s.smart_mode)
        f.addRow("", self.smart)
        if win.product == "video":
            self.vfmt = QComboBox()
            for key, text in (("mp4", "Video · MP4"), ("mkv", "Video · MKV"), ("mp3", "Audio · MP3"),
                              ("m4a", "Audio · M4A")):
                self.vfmt.addItem(text, key)
            self.vfmt.setCurrentIndex(max(0, self.vfmt.findData(s.video_container)))
            f.addRow("Format", self.vfmt)
            self.vq = QComboBox()
            for h in VIDEO_QUALITIES:
                self.vq.addItem(f"{h}p" + (f"  {HEIGHT_TAG[h]}" if h in HEIGHT_TAG else ""), h)
            self.vq.setCurrentIndex(max(0, self.vq.findData(s.video_quality)))
            f.addRow("Best quality up to", self.vq)
            self.subs = QCheckBox("Download subtitles, language:")
            self.subs.setChecked(s.subtitles)
            self.sublang = QLineEdit(s.subtitle_lang)
            self.sublang.setMaximumWidth(90)
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.addWidget(self.subs)
            rl.addWidget(self.sublang)
            rl.addStretch()
            f.addRow("", row)
        self.afmt = QComboBox()
        for a in AUDIO_FORMATS:
            self.afmt.addItem(a.upper(), a)
        self.afmt.setCurrentIndex(max(0, self.afmt.findData(s.audio_format)))
        self.abr = QComboBox()
        for b in AUDIO_BITRATES:
            self.abr.addItem(f"{b} kbps", b)
        self.abr.setCurrentIndex(max(0, self.abr.findData(s.audio_bitrate)))
        if win.product == "audio":
            f.addRow("Audio format", self.afmt)
        f.addRow("Audio quality", self.abr)
        self.cover = QCheckBox("Embed cover art and tags in audio files")
        self.cover.setChecked(s.embed_cover)
        f.addRow("", self.cover)
        tabs.addTab(g, "General")

        lg = QWidget()
        lg.setObjectName("TabPage")
        lf = QFormLayout(lg)
        lf.setContentsMargins(18, 18, 18, 18)
        lf.setSpacing(12)
        note = QLabel("YouTube and some other sites only download for signed-in visitors. MediaGrab uses the "
                      "login from a browser on this computer, so sign in to YouTube there once. Automatic picks "
                      "the browser you used most recently. Your login is only ever sent to the site itself.")
        note.setWordWrap(True)
        note.setObjectName("Muted")
        lf.addRow(note)
        self.browser = QComboBox()
        found = dict(engine.detect_browsers())
        newest = next(iter(found), None)
        for b in COOKIE_BROWSERS:
            if b == "auto":
                text = "Automatic" + (f"  ·  {newest.capitalize()} now" if newest else "  ·  no browser found")
            elif not b:
                text = "Don't use a login"
            else:
                text = b.capitalize() + ("" if b in found else "  ·  not installed")
            self.browser.addItem(text, b)
        self.browser.setCurrentIndex(max(0, self.browser.findData(s.cookies_browser)))
        bw = QWidget()
        bw.setStyleSheet("background: transparent;")
        bh = QHBoxLayout(bw)
        bh.setContentsMargins(0, 0, 0, 0)
        bh.addWidget(self.browser, 1)
        self.test_btn = QPushButton("Test login")
        self.test_btn.clicked.connect(self.test_login)
        bh.addWidget(self.test_btn)
        lf.addRow("Use login from", bw)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        self.test_result.setVisible(False)
        lf.addRow("", self.test_result)
        self._sig = TestSignal()
        self._sig.done.connect(self._tested)
        self.cookies = QLineEdit(s.cookies_file)
        self.cookies.setPlaceholderText("Optional: path to cookies.txt")
        cw = QWidget()
        cw.setStyleSheet("background: transparent;")
        ch = QHBoxLayout(cw)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.addWidget(self.cookies, 1)
        pick = QPushButton("Browse…")
        pick.clicked.connect(lambda: self.cookies.setText(
            QFileDialog.getOpenFileName(self, "cookies.txt", str(Path.home()), "Cookies (*.txt)")[0]
            or self.cookies.text()))
        ch.addWidget(pick)
        lf.addRow("Cookies file", cw)
        self.pot = QLineEdit(s.pot_server)
        self.pot.setPlaceholderText("Optional, advanced: http://127.0.0.1:4416")
        lf.addRow("PO-token server", self.pot)
        tabs.addTab(lg, "Login")

        btns = QHBoxLayout()
        btns.addStretch()
        c = QPushButton("Cancel")
        c.clicked.connect(self.reject)
        ok = QPushButton("Save")
        ok.setObjectName("Accent")
        ok.clicked.connect(self.save)
        btns.addWidget(c)
        btns.addWidget(ok)
        v.addLayout(btns)
        ok.setDefault(True)
        no_auto_default(self, ok)

    def test_login(self):
        trial = dataclasses.replace(self.win.s, cookies_browser=self.browser.currentData(),
                                    cookies_file=self.cookies.text().strip(), pot_server=self.pot.text().strip())
        self.test_btn.setEnabled(False)
        self.test_btn.setText("Testing…")
        self.test_result.setStyleSheet(f"color: {theme.MUTED};")
        self.test_result.setText("Asking YouTube for a video with this login…")
        self.test_result.setVisible(True)
        threading.Thread(target=lambda: self._sig.done.emit(*engine.test_login(trial)), daemon=True).start()

    def _tested(self, ok, msg):
        self.test_btn.setEnabled(True)
        self.test_btn.setText("Test login")
        self.test_result.setStyleSheet(f"color: {theme.OK if ok else theme.ERR};")
        self.test_result.setText(("✓  " if ok else "⚠  ") + msg)

    def save(self):
        s = self.win.s
        s.download_dir = self.folder.text().strip() or s.download_dir
        s.max_parallel = self.parallel.currentData()
        s.smart_mode = self.smart.isChecked()
        if self.win.product == "video":
            s.video_container = self.vfmt.currentData()
            s.video_quality = self.vq.currentData()
            s.subtitles = self.subs.isChecked()
            s.subtitle_lang = self.sublang.text().strip() or "en"
        else:
            s.audio_format = self.afmt.currentData()
        s.audio_bitrate = self.abr.currentData()
        s.embed_cover = self.cover.isChecked()
        s.cookies_browser = self.browser.currentData()
        s.cookies_file = self.cookies.text().strip()
        s.pot_server = self.pot.text().strip()
        s.save()
        self.accept()


# ------------------------------------------------------------------ main window

class MainWindow(QMainWindow):
    def __init__(self, product: str):
        super().__init__()
        self.product = product
        self.P = PRODUCTS[product]
        self.s = Settings.load(product)
        self.jobs: dict[int, Job] = {}
        self.rows: dict[int, Row] = {}
        self.last_prog: dict[int, dict] = {}
        self.bridge = Bridge()
        self.pool = ThreadPoolExecutor(4)
        self.bridge.progress.connect(self._on_progress)
        self.bridge.done.connect(self._on_done)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.stopped.connect(self._on_stopped)
        self.bridge.probed.connect(self._on_probed)
        self.bridge.probe_failed.connect(self._on_failed)
        self.bridge.thumb.connect(lambda jid, p: (self.jobs.get(jid) and setattr(self.jobs[jid], "thumb", p),
                                                  self.rows.get(jid) and self.rows[jid].set_thumb(p)))

        self.setWindowTitle(self.P["name"])
        self.setWindowIcon(icon(self.P["icon"], self.P["accent"]))
        self.resize(1040, 720)
        self.setAcceptDrops(True)
        self._build()
        self._load_history()
        QShortcut(QKeySequence.Paste, self, activated=self.paste_link)
        self._save_timer = QTimer(self, singleShot=True, interval=800, timeout=self._save)

    # ---- layout
    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        top = QWidget()
        top.setObjectName("TopBar")
        th = QHBoxLayout(top)
        th.setContentsMargins(20, 12, 14, 12)
        logo = QLabel()
        logo.setPixmap(pixmap(self.P["icon"], 30, self.P["accent"], self.devicePixelRatioF()))
        th.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        b1 = QLabel(self.P["short"])
        b1.setObjectName("Brand")
        b2 = QLabel("MediaGrab")
        b2.setObjectName("BrandSub")
        names.addWidget(b1)
        names.addWidget(b2)
        th.addLayout(names)
        th.addStretch()
        self.smart_btn = QPushButton("  Smart Mode")
        self.smart_btn.setObjectName("Toggle")
        self.smart_btn.setCheckable(True)
        self.smart_btn.setFocusPolicy(Qt.NoFocus)
        self.smart_btn.setCursor(Qt.PointingHandCursor)
        self.smart_btn.setToolTip("Smart Mode: download with your saved choices without asking each time")
        self.smart_btn.toggled.connect(self._toggle_smart)
        th.addWidget(self.smart_btn)
        th.addWidget(tool("settings", "Settings", self.open_settings, theme.TEXT))
        v.addWidget(top)

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(20, 18, 20, 8)
        bl.setSpacing(14)

        card = QFrame()
        card.setObjectName("InputCard")
        cl = QHBoxLayout(card)
        cl.setContentsMargins(14, 14, 14, 14)
        cl.setSpacing(10)
        self.url = QLineEdit()
        self.url.setObjectName("UrlEdit")
        self.url.setPlaceholderText("Paste a video, playlist or channel link…")
        self.url.setClearButtonEnabled(True)
        self.url.returnPressed.connect(self._add_from_field)
        cl.addWidget(self.url, 1)
        paste = QPushButton("  Paste Link")
        paste.setObjectName("Accent")
        paste.setProperty("class", "Big")
        paste.setIcon(icon("clipboard", "#ffffff"))
        paste.setIconSize(QSize(18, 18))
        paste.setCursor(Qt.PointingHandCursor)
        paste.setToolTip("Download the link currently in your clipboard (Ctrl+V)")
        paste.clicked.connect(self.paste_link)
        paste.setStyleSheet("padding: 10px 20px; font-size: 11pt; border-radius: 11px;")
        cl.addWidget(paste)
        if self.product == "audio":
            addf = QPushButton("  Add Files")
            addf.setIcon(icon("file-plus"))
            addf.setIconSize(QSize(18, 18))
            addf.setCursor(Qt.PointingHandCursor)
            addf.setToolTip("Convert video files on this computer to audio")
            addf.setStyleSheet("padding: 10px 18px; font-size: 11pt; border-radius: 11px;")
            addf.clicked.connect(self.add_files)
            cl.addWidget(addf)
        bl.addWidget(card)

        lh = QHBoxLayout()
        self.list_title = QLabel("DOWNLOADS")
        self.list_title.setObjectName("SectionTitle")
        lh.addWidget(self.list_title)
        lh.addStretch()
        self.clear_btn = QPushButton("Clear finished")
        self.clear_btn.setObjectName("Link")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clear_finished)
        lh.addWidget(self.clear_btn)
        bl.addLayout(lh)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        host = QWidget()
        host.setObjectName("ListHost")
        self.list_lay = QVBoxLayout(host)
        self.list_lay.setContentsMargins(0, 0, 6, 0)
        self.list_lay.setSpacing(8)
        self.list_lay.addStretch()
        self.scroll.setWidget(host)
        bl.addWidget(self.scroll, 1)

        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.setAlignment(Qt.AlignCenter)
        ei = QLabel()
        ei.setPixmap(pixmap("download", 64, theme.FAINT, self.devicePixelRatioF(), width=1.5))
        ei.setAlignment(Qt.AlignCenter)
        et = QLabel("No downloads yet")
        et.setAlignment(Qt.AlignCenter)
        et.setStyleSheet("font-size: 14pt; font-weight: 700; margin-top: 10px;")
        eh = QLabel(self.P["hint"])
        eh.setObjectName("Muted")
        eh.setWordWrap(True)
        eh.setAlignment(Qt.AlignCenter)
        eh.setMaximumWidth(720)
        for w in (ei, et, eh):
            el.addWidget(w, 0, Qt.AlignHCenter)
        bl.addWidget(self.empty, 1)
        v.addWidget(body, 1)

        foot = QWidget()
        foot.setObjectName("Footer")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(20, 8, 14, 8)
        self.foot_lbl = QLabel()
        self.foot_lbl.setObjectName("Muted")
        fl.addWidget(self.foot_lbl)
        fl.addStretch()
        of = QPushButton("  Open folder")
        of.setObjectName("Link")
        of.setIcon(icon("folder", theme.MUTED))
        of.setCursor(Qt.PointingHandCursor)
        of.clicked.connect(lambda: (Path(self.s.download_dir).mkdir(parents=True, exist_ok=True),
                                    open_path(self.s.download_dir)))
        fl.addWidget(of)
        v.addWidget(foot)
        self.smart_btn.setChecked(self.s.smart_mode)
        self._toggle_smart(self.s.smart_mode)
        self._update_counts()

    # ---- adding things
    def _toggle_smart(self, on):
        self.smart_btn.setIcon(icon("zap", self.P["accent"] if on else theme.MUTED))
        if self.s.smart_mode != on:
            self.s.smart_mode = on
            self.s.save()

    def paste_link(self):
        text = QGuiApplication.clipboard().text() or ""
        urls = URL_RE.findall(text)
        if not urls:
            self._toast("The clipboard has no link. Copy a video's address first, then press Paste Link.")
            return
        for u in dict.fromkeys(urls):
            self.add_url(u.rstrip(").,;'\""))

    def _add_from_field(self):
        text = self.url.text().strip()
        if not text:
            return self.paste_link()
        if not URL_RE.match(text):
            text = "https://" + text
        self.url.clear()
        self.add_url(text)

    def add_url(self, url: str):
        job = Job(url=url, status="analyzing", title=url)
        self._add_job(job)

        def work():
            try:
                info = engine.probe(url, self.s)
                info["thumb_path"] = engine.fetch_thumbnail(info.get("thumbnail"))
                self.bridge.probed.emit(job.id, info)
            except engine.EngineError as e:
                self.bridge.probe_failed.emit(job.id, str(e))
            except Exception as e:  # never let a worker die silently
                self.bridge.probe_failed.emit(job.id, engine.friendly_error(e))
        threading.Thread(target=work, daemon=True).start()

    def add_files(self, paths=None):
        if not paths:
            paths, _ = QFileDialog.getOpenFileNames(
                self, "Choose video or audio files", str(Path.home()),
                "Media files (" + " ".join("*" + e for e in sorted(MEDIA_EXT)) + ");;All files (*)")
        paths = [p for p in paths or [] if Path(p).is_file()]
        if not paths:
            return
        if self.s.smart_mode:
            opts = self._smart_opts(audio=True)
        else:
            info = {"kind": "files", "files": paths,
                    "title": Path(paths[0]).name if len(paths) == 1 else f"{len(paths)} files"}
            try:
                info["thumb_path"] = engine.local_thumbnail(paths[0])
                info["duration"] = sum(engine.media_info(p)["duration"] for p in paths[:50])
            except Exception:
                pass
            dlg = OptionsDialog(self, info)
            if dlg.exec() != QDialog.Accepted:
                return
            opts = dlg.result_opts()
            dlg.remember(opts)
            self.smart_btn.setChecked(self.s.smart_mode)
        for p in paths:
            job = Job(kind="file", path=p, title=Path(p).name, site="Local file", opts=opts,
                      label=job_label(opts), size=Path(p).stat().st_size)
            self._add_job(job)
            self.pool.submit(lambda j=job: self.bridge.thumb.emit(j.id, engine.local_thumbnail(j.path) or ""))
        self._pump()

    def _smart_opts(self, audio=False) -> dict:
        s = self.s
        if audio or self.product == "audio" or s.video_container in ("mp3", "m4a"):
            fmt = s.video_container if self.product == "video" and s.video_container in ("mp3", "m4a") \
                else s.audio_format
            return {"mode": "audio", "audio_format": fmt, "bitrate": s.audio_bitrate,
                    "embed_cover": s.embed_cover, "folder": s.download_dir}
        return {"mode": "video", "quality": s.video_quality, "container": s.video_container,
                "subtitles": s.subtitles, "sub_lang": s.subtitle_lang, "folder": s.download_dir}

    def _on_probed(self, jid, info):
        job = self.jobs.get(jid)
        if not job:
            return
        if self.s.smart_mode:
            opts = self._smart_opts()
            entries = info["entries"] if info["kind"] == "playlist" else None
        else:
            dlg = OptionsDialog(self, info)
            if dlg.exec() != QDialog.Accepted:
                self.remove(job)
                return
            opts = dlg.result_opts()
            dlg.remember(opts)
            self.smart_btn.setChecked(self.s.smart_mode)
            entries = dlg.selected_entries()
        if info["kind"] == "playlist":
            self.remove(job)
            folder = Path(opts["folder"]) / safe_name(info["title"])
            for e in reversed(entries or []):
                if not e.get("url"):
                    continue
                j = Job(url=e["url"], title=e["title"], site=info["site"],
                        opts=dict(opts, folder=str(folder)), label=job_label(opts))
                self._add_job(j)
                if e.get("thumbnail"):
                    self.pool.submit(lambda jj=j, t=e["thumbnail"]:
                                     self.bridge.thumb.emit(jj.id, engine.fetch_thumbnail(t) or ""))
        else:
            job.title, job.site, job.thumb = info["title"], info["site"], info.get("thumb_path")
            job.url = info.get("url") or job.url
            job.opts, job.label, job.status = opts, job_label(opts), "queued"
            if opts["mode"] == "video" and opts.get("quality"):
                hs = [h for h in info.get("heights", []) if h <= opts["quality"]]
                job.size = (info.get("sizes") or {}).get(max(hs)) if hs else 0
                if hs:
                    job.label = job_label(dict(opts, quality=max(hs)))
            self.rows[jid].set_thumb(job.thumb)
            self.rows[jid].refresh()
        self._pump()
        self._changed()

    # ---- queue
    def _add_job(self, job: Job, top=True):
        self.jobs[job.id] = job
        row = Row(job, self)
        self.rows[job.id] = row
        self.list_lay.insertWidget(0 if top else self.list_lay.count() - 1, row)
        if job.thumb:
            row.set_thumb(job.thumb)
        self._update_counts()
        if top:
            self.scroll.verticalScrollBar().setValue(0)

    def _pump(self):
        running = sum(1 for j in self.jobs.values() if j.status == "running")
        queued = sorted((j for j in self.jobs.values() if j.status == "queued"), key=lambda j: j.added)
        for j in queued[: max(0, self.s.max_parallel - running)]:
            self._start(j)
        self._update_counts()

    def _start(self, job: Job):
        job.status, job.stop_reason, job.error = "running", None, ""
        self.rows[job.id].refresh({"stage": "Starting"})
        jid = job.id

        def work():
            try:
                emit = lambda d: self.bridge.progress.emit(jid, d)
                stop = lambda: job.stop_reason is not None
                if job.kind == "file":
                    res = engine.convert_file(dict(job.opts, path=job.path), self.s, emit, stop)
                else:
                    res = engine.download(dict(job.opts, url=job.url), self.s, emit, stop)
                self.bridge.done.emit(jid, res)
            except engine.Cancelled as c:
                job.partials = sorted(set(job.partials) | set(c.partials))
                self.bridge.stopped.emit(jid)
            except engine.EngineError as e:
                self.bridge.failed.emit(jid, str(e))
            except Exception as e:
                self.bridge.failed.emit(jid, engine.friendly_error(e))
        job.thread = threading.Thread(target=work, daemon=True)
        job.thread.start()

    def _on_progress(self, jid, d):
        job = self.jobs.get(jid)
        if not job or job.status != "running":
            return
        if d.get("fraction") is not None and d.get("stage") in ("Downloading", "Converting"):
            job.fraction = d["fraction"]
        if d.get("total"):
            job.size = d["total"]
        if d.get("title") and d["title"] != job.title:
            job.title, job.site = d["title"], d.get("site") or job.site   # playlist items arrive untitled
        if d.get("thumbnail") and not job.thumb:
            job.thumb = "pending"
            self.pool.submit(lambda t=d["thumbnail"]: self.bridge.thumb.emit(jid, engine.fetch_thumbnail(t) or ""))
        self.rows[jid].refresh(d)

    def _on_done(self, jid, path):
        job = self.jobs.get(jid)
        if not job:
            return
        job.status, job.result, job.fraction = "done", path, 1.0
        try:
            job.size = Path(path).stat().st_size
        except OSError:
            pass
        self.rows[jid].refresh()
        self._pump()
        self._changed()

    def _on_failed(self, jid, msg):
        job = self.jobs.get(jid)
        if not job:
            return
        if job.status == "analyzing":
            job.title = job.url
        job.status, job.error = "error", msg
        self.rows[jid].refresh()
        self._pump()
        self._changed()

    def _on_stopped(self, jid):
        job = self.jobs.get(jid)
        if not job:
            return
        if job.stop_reason == "remove":
            engine.remove_partials(job.partials)
            self._drop(job)
        else:
            job.status = "paused"
            self.rows[jid].refresh()
        self._pump()
        self._changed()

    # ---- row actions
    def pause(self, job: Job):
        if job.status == "running":
            job.stop_reason = "pause"
            self.rows[job.id].status.setText("Pausing…")
        elif job.status == "queued":
            job.status = "paused"
            self.rows[job.id].refresh()
            self._changed()

    def resume(self, job: Job, again=False):
        if job.status == "error" and not job.opts:   # failed while analysing: analyse again
            self._drop(job)
            self.add_url(job.url)
            return
        if again:
            job.fraction, job.result = 0.0, ""
        job.status, job.added = "queued", time.time()
        self.rows[job.id].refresh()
        self._pump()
        self._changed()

    def remove(self, job: Job):
        if job.status == "running":
            job.stop_reason = "remove"
            self.rows[job.id].setEnabled(False)
        else:
            if job.status != "done":   # never touch a finished file
                engine.remove_partials(job.partials)
            self._drop(job)

    def _drop(self, job: Job):
        self.jobs.pop(job.id, None)
        row = self.rows.pop(job.id, None)
        if row:
            row.setParent(None)
            row.deleteLater()
        self._update_counts()
        self._changed()

    def delete_file(self, job: Job):
        if QMessageBox.question(self, "Delete file", f"Delete this file from the disk?\n\n{job.result}") \
                != QMessageBox.Yes:
            return
        try:
            Path(job.result).unlink()
        except OSError as e:
            self._toast(f"Couldn't delete: {e}")
            return
        self._drop(job)

    def clear_finished(self):
        for j in [j for j in self.jobs.values() if j.status == "done"]:
            self._drop(j)

    def open_settings(self):
        if SettingsDialog(self).exec() == QDialog.Accepted:
            self.smart_btn.setChecked(self.s.smart_mode)
            self._pump()

    # ---- misc
    def _update_counts(self):
        js = list(self.jobs.values())
        n = {k: sum(1 for j in js if j.status == k) for k in ("running", "queued", "done", "error", "paused")}
        has = bool(js)
        self.scroll.setVisible(has)
        self.empty.setVisible(not has)
        self.clear_btn.setVisible(n["done"] > 0)
        self.list_title.setText(f"DOWNLOADS  ·  {len(js)}" if has else "DOWNLOADS")
        bits = []
        if n["running"]:
            bits.append(f"{n['running']} active")
        if n["queued"]:
            bits.append(f"{n['queued']} waiting")
        if n["paused"]:
            bits.append(f"{n['paused']} paused")
        if n["done"]:
            bits.append(f"{n['done']} done")
        if n["error"]:
            bits.append(f"{n['error']} failed")
        folder = self.s.download_dir.replace(str(Path.home()), "~")
        self.foot_lbl.setText(("  ·  ".join(bits) + "      " if bits else "") + f"Saving to {folder}")

    def _toast(self, text):
        self.foot_lbl.setText(text)
        self.foot_lbl.setStyleSheet(f"color: {theme.WARN};")
        QTimer.singleShot(5000, lambda: (self.foot_lbl.setStyleSheet(""), self._update_counts()))

    def _changed(self):
        self._update_counts()
        self._save_timer.start()

    def _save(self):
        order = sorted(self.jobs.values(), key=lambda j: j.added)
        self.s.history = [j.to_dict() for j in order if j.status != "analyzing"][-500:]
        self.s.save()

    def _load_history(self):
        for d in self.s.history:
            try:
                job = Job.from_dict(d)
            except TypeError:
                continue
            if job.status == "done" and job.result and not Path(job.result).exists():
                job.status, job.error = "error", "File was moved or deleted."
            self._add_job(job)
        self._update_counts()

    def closeEvent(self, e):
        active = [j for j in self.jobs.values() if j.status == "running"]
        for j in active:
            j.stop_reason = "pause"
        self._save()
        deadline = time.monotonic() + 3
        for j in active:
            if j.thread:
                j.thread.join(max(0, deadline - time.monotonic()))
        super().closeEvent(e)

    # ---- drag and drop
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasText():
            e.acceptProposedAction()

    def dropEvent(self, e):
        md = e.mimeData()
        files, links = [], []
        for u in md.urls():
            if u.isLocalFile():
                files.append(u.toLocalFile())
            elif u.scheme().startswith("http"):
                links.append(u.toString())
        if not md.urls() and md.hasText():
            links = URL_RE.findall(md.text())
        for l in links:
            self.add_url(l)
        if files:
            if self.product == "audio":
                self.add_files(files)
            else:
                self._toast("Local files are converted in MediaGrab Video to MP3.")


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip(" .")[:120] or "Playlist"


def main(argv=None):
    argv = argv if argv is not None else sys.argv
    product = "audio" if any(a in ("audio", "--audio", "mp3") for a in argv[1:]) else "video"
    desktop_id = "mediagrab-mp3" if product == "audio" else "mediagrab-video"  # = .desktop file name
    QApplication.setApplicationName(desktop_id)
    QApplication.setDesktopFileName(desktop_id)
    app = QApplication(argv)
    app.setStyle("Fusion")
    P = PRODUCTS[product]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    check = CACHE_DIR / "check.svg"
    check.write_bytes(svg("check", "#ffffff", width=3.5))
    app.setStyleSheet(theme.qss(P["accent"], P["accent_hi"]).replace(":/check", str(check)))
    w = MainWindow(product)
    w.show()
    w.url.setFocus()
    for a in argv[1:]:
        if URL_RE.match(a):
            w.add_url(a)
        elif Path(a).is_file() and product == "audio":
            w.add_files([a])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
