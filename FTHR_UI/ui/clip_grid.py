# Responsive library grid grouped by date.
#
# Workers scan files and generate thumbnails; disk caches retain thumbnails
# and durations. Keep filesystem and decoding work off the Qt thread.
import os
import sys
import subprocess
import threading
from core import linux_tools
import hashlib
import math
import time
from pathlib import Path
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QGridLayout,
    QMenu, QApplication,
    QPushButton, QSizePolicy,
)
from PySide6.QtCore import (
    Qt, Signal, QTimer, QRunnable, QThreadPool, QObject,
    QFileSystemWatcher, QPropertyAnimation, QRect, QPoint,
    QUrl, QSize,
)
from PySide6.QtGui import QDesktopServices, QPixmap, QPainter, QColor
from PySide6.QtGui import QImage, QImageReader

from core.clip_files import (
    IMAGE_SUFFIXES,
    VIDEO_SUFFIXES,
    is_completed_video_path,
    is_fthr_temporary_dir,
)
from core.library_index import (
    LibraryRecord, LibraryScanResult, canonical_media_path,
    scan_library,
)
from core.library_cache import maybe_prune_thumbnail_cache
from core.media_process import run_media_process as _run_owned_media_process, media_process_snapshot
from core.library_ownership import MediaOwnership, classify_media_path
from core.media_metadata import parse_ffprobe_video_metadata
from core.ffmpeg_tools import FFmpegUnavailable, get_ffmpeg_exe, get_ffprobe_exe
from core.settings_manager import clips_directory_from
from core.field_diagnostics import (
    emit_event, get_diagnostic_session, process_memory_bytes,
)
from ui.style import WheelSafeComboBox, paint_dropdown_arrow


class _DropdownCombo(WheelSafeComboBox):
    """QComboBox that shows icons/dropdown.png as its arrow, rotated when open."""

    _custom_arrow_managed = True

    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup_open = False

    def showPopup(self):
        self._popup_open = True
        self.update()
        super().showPopup()

    def hidePopup(self):
        super().hidePopup()
        self._popup_open = False
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        paint_dropdown_arrow(p, self.rect(), self._popup_open)
        p.end()

from ui.style import (
    Colors, Fonts, Sizes,
    label_display,
    context_menu_qss, combo_qss,
)
from ui.dialogs import FthrMessageDialog


THUMB_CACHE_DIR = os.path.join(os.path.expanduser('~'), '.fthr', 'thumbnails')

# These folders hold processed/shared clips — not shown in the main library grid
_GRID_EXCLUDED_DIRS = {'Exported', 'Shared'}
_VIDEO_EXTS = VIDEO_SUFFIXES
_IMAGE_EXTS = IMAGE_SUFFIXES


def _rgba(hex_color: str, alpha: int) -> str:
    """Turn a theme hex token into a QSS rgba color with the given alpha."""
    color = QColor(hex_color)
    return f'rgba({color.red()},{color.green()},{color.blue()},{alpha})'


def _is_grid_excluded_dir(path: str) -> bool:
    """Keep app-owned post-processing directories out of the library scan."""

    name = os.path.basename(os.fspath(path))
    return name in _GRID_EXCLUDED_DIRS or is_fthr_temporary_dir(name)


def _show_in_file_manager(file_path: str) -> None:
    """Open the containing folder and highlight ``file_path`` when possible."""

    path = os.path.abspath(os.path.normpath(os.fspath(file_path)))
    if sys.platform == 'win32':
        # Explorer expects /select,"path" as a single command-line expression.
        # Passing the switch as a list argument makes subprocess quote the whole
        # /select expression, which causes Explorer to ignore the selection and
        # only open its default location.
        subprocess.Popen(f'explorer.exe /select,"{path}"')
        return

    _opener = linux_tools.path('xdg-open')
    if _opener:
        subprocess.Popen([_opener, os.path.dirname(path)])
    else:
        print(f'[Clips] {linux_tools.missing_message("xdg-open")}')


# Card geometry
# Cards flex with the viewport. The thumbnail *surface* remains 16:9, while
# source pixels are fitted inside it without cropping or distortion.
_CARD_W      = 320  # preferred / cache sizing reference
_CARD_MIN_W  = 240
_CARD_MAX_W  = 420
_THUMB_H     = 180
_CARD_BODY_H = 104
_CARD_H      = _THUMB_H + _CARD_BODY_H
_MAX_MATERIALIZED_CARDS = 96
_VIRTUAL_OVERSCAN_ROWS = 2
_MAX_THUMBNAIL_QUEUE = 128
_NEGATIVE_CACHE_SECONDS = 30.0
def _get_cached_thumb_path(file_path: str) -> str:
    """Return a cache path based on file path + mtime so stale caches auto-invalidate.

    Including mtime in the hash invalidates the cache automatically when a file
    is overwritten without requiring a separate cache index.
    """
    info = os.stat(file_path)
    # v4 caches preserve source aspect ratio through FFmpeg scaling. The format
    # here prevents an older, force-stretched 16:9 thumbnail from surviving an
    # application update.
    key = f'{canonical_media_path(file_path)}|{info.st_size}|{info.st_mtime_ns}|aspect-v4'.encode(
        'utf-8', errors='surrogateescape')
    return os.path.join(THUMB_CACHE_DIR, hashlib.md5(key).hexdigest() + '.jpg')


def _get_cached_duration_path(thumb_path: str) -> str:
    """Sidecar storing authoritative clip metadata for thumbnail/viewer reuse.

    The versioned filename invalidates old OpenCV-derived FPS caches.
    Format: ``duration width height fps video_bitrate total_bitrate``.
    """
    return thumb_path[:-4] + '.meta-v3'


def _get_negative_cache_path(thumb_path: str) -> str:
    """Fingerprint-bound marker for an unchanged media probe failure."""
    return thumb_path[:-4] + '.failed'


def _has_recent_probe_failure(path: str) -> bool:
    try:
        age = time.time() - os.path.getmtime(path)
        return 0 <= age < _NEGATIVE_CACHE_SECONDS
    except OSError:
        # A missing/unreadable optional marker must not suppress a fresh probe.
        return False


def _read_cached_duration(dur_path: str) -> int:
    """Backwards-compatible reader that returns just the duration."""
    meta = read_cached_metadata(dur_path)
    return int(meta[0]) if meta else 0


def read_cached_metadata(dur_path: str):
    """Return factual cached metadata, or ``None`` when unavailable."""
    try:
        with open(dur_path, 'r') as f:
            parts = f.read().strip().split()
        if not parts:
            return None
        duration = float(parts[0])
        width    = int(parts[1]) if len(parts) > 1 else 0
        height   = int(parts[2]) if len(parts) > 2 else 0
        fps      = float(parts[3]) if len(parts) > 3 else 0.0
        video_bitrate = int(parts[4]) if len(parts) > 4 else 0
        total_bitrate = int(parts[5]) if len(parts) > 5 else 0
        if (not math.isfinite(duration) or duration <= 0
                or width <= 0 or height <= 0 or not math.isfinite(fps)
                or fps < 0 or video_bitrate < 0 or total_bitrate < 0):
            return None
        return (duration, width, height, fps,
                video_bitrate, total_bitrate)
    except (OSError, ValueError):
        # A corrupt/missing sidecar is a cache miss and will be probed again.
        return None


def _write_cached_duration(dur_path: str, duration: float,
                            width: int = 0, height: int = 0, fps: float = 0.0,
                            video_bitrate: int = 0,
                            total_bitrate: int = 0):
    """Persist factual FFprobe metadata so ClipViewer can skip a second probe."""
    try:
        temp_path = dur_path + f'.tmp-{os.getpid()}-{threading.get_ident()}'
        with open(temp_path, 'w') as f:
            f.write(
                f'{float(duration):.6f} {int(width)} {int(height)} '
                f'{float(fps):.6f} {int(video_bitrate)} {int(total_bitrate)}')
        os.replace(temp_path, dur_path)
    except OSError:
        try:
            os.unlink(temp_path)
        except (OSError, UnboundLocalError):
            # A failed optional cache write may leave no temporary file to remove.
            pass


# Public lookup used by ClipViewer to avoid blocking a decoder on first
# open. Returns (duration_sec, width, height, fps) or None on any miss / error.
def get_cached_clip_metadata(file_path: str):
    try:
        cache_path = _get_cached_thumb_path(file_path)
    except OSError:
        # A removed source has no cache identity; callers can show the placeholder.
        return None
    return read_cached_metadata(_get_cached_duration_path(cache_path))


def _humanize_relative(ts: float) -> str:
    """Turn a file mtime into a 'X days ago' / 'just now' style string."""
    now = datetime.now()
    when = datetime.fromtimestamp(ts)
    delta = now - when
    secs = int(delta.total_seconds())
    if secs < 60:
        return 'just now'
    mins = secs // 60
    if mins < 60:
        return f'{mins} min ago'
    hours = mins // 60
    if hours < 24:
        return f'{hours} hr ago'
    days = hours // 24
    if days == 1:
        return 'yesterday'
    if days < 7:
        return f'{days} days ago'
    weeks = days // 7
    if weeks < 5:
        return f'{weeks} wk ago'
    months = days // 30
    if months < 12:
        return f'{months} mo ago'
    return f'{days // 365} yr ago'


def _section_label_for(ts: float) -> str:
    """Group label like 'TUE, APR 28' for grouping cards under date headers."""
    return datetime.fromtimestamp(ts).strftime('%a, %b %d').upper()


def _game_name_from_path(file_path: str, clips_root: str | None = None) -> str:
    """Best-effort game / source name from the parent folder."""
    parent = os.path.basename(os.path.dirname(file_path))
    root_name = os.path.basename(os.path.normpath(
        clips_root or os.path.expanduser('~/FTHR_Clips')))
    if parent in (root_name, ''):
        return 'DESKTOP'
    return parent.upper()


def _clip_title_from_filename(file_path: str) -> str:
    """Friendly title — strip the timestamp tail from the filename."""
    base = os.path.splitext(os.path.basename(file_path))[0]
    # Drop trailing pattern like  *_clip_from_28Apr2026_22-30
    for suffix_marker in ('_clip_from_', '_screenshot_from_', '_from_'):
        idx = base.find(suffix_marker)
        if idx > 0:
            base = base[:idx]
            break
    base = base.replace('_', ' ').replace('-', ' ').strip()
    return base[:1].upper() + base[1:] if base else 'Clip'


class _ThumbnailSignals(QObject):
    finished = Signal(str, str, int)  # file_path, cache_path, duration_sec
    diagnostic_finished = Signal(int, int, bool)  # total_ms, metadata_ms, cache_hit


def _valid_cached_thumbnail(path: str) -> bool:
    """Validate a JPEG header/decode before treating a cache entry as a hit."""
    try:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        image = reader.read()
        return not image.isNull() and image.width() > 0 and image.height() > 0
    except (OSError, RuntimeError):
        # A damaged cached JPEG is a miss, never a reason to reject the clip.
        return False


def _probe_with_owned_process(
        file_path: str, cancel_event: threading.Event):
    try:
        probe = get_ffprobe_exe()
    except FFmpegUnavailable:
        # Missing FFprobe only disables enrichment; opening uses the original media.
        return None
    result = _run_owned_media_process(
        [probe, '-v', 'error', '-show_entries',
         'stream=index,codec_type,width,height,avg_frame_rate,'
         'r_frame_rate,duration,bit_rate:'
         'format_tags=fthr_frame_rate,fthr_video_bitrate_bps:'
         'format=duration,size,bit_rate', '-of', 'json', file_path],
        cancel_event)
    if result is None or result[0] != 0:
        return None
    try:
        size = Path(file_path).stat().st_size
    except OSError:
        size = None
    return parse_ffprobe_video_metadata(
        result[1].decode('utf-8', errors='replace'), file_size=size)


def _decode_thumbnail_with_owned_process(
        file_path: str, cancel_event: threading.Event) -> QImage | None:
    try:
        ffmpeg = get_ffmpeg_exe()
    except FFmpegUnavailable:
        # Missing FFmpeg only disables the thumbnail, not access to the clip.
        return None
    result = _run_owned_media_process(
        [ffmpeg, '-hide_banner', '-loglevel', 'error', '-ss', '0',
         '-i', file_path, '-frames:v', '1', '-vf',
         "scale=w='min(640,iw)':h='min(360,ih)':force_original_aspect_ratio=decrease",
         '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'],
        cancel_event)
    if result is None or result[0] != 0 or not result[1]:
        return None
    image = QImage.fromData(result[1], 'JPEG')
    if image.isNull():
        return None
    return image


class _ThumbnailWorker(QRunnable):
    """Generate and cache a video thumbnail off the main thread."""

    def __init__(self, file_path: str, cancel_event=None):
        super().__init__()
        self.file_path = file_path
        self.cancel_event = cancel_event or threading.Event()
        self.signals = _ThumbnailSignals()

    def run(self):
        started = time.monotonic()
        metadata_ms = 0
        cache_hit = False
        cache_path = ''
        temp_path = ''
        try:
            if not is_completed_video_path(self.file_path):
                self.signals.finished.emit(self.file_path, '', 0)
                return
            if self.cancel_event.is_set():
                return
            cache_path = _get_cached_thumb_path(self.file_path)
            dur_path   = _get_cached_duration_path(cache_path)
            failed_path = _get_negative_cache_path(cache_path)

            # Cache hit: a valid JPEG AND factual metadata sidecar both exist.
            # Fingerprinting in _get_cached_thumb_path means mutations select a
            # new generation; corrupt entries never poison the next run.
            if (os.path.exists(cache_path) and os.path.exists(dur_path)
                    and read_cached_metadata(dur_path) is not None
                    and _valid_cached_thumbnail(cache_path)):
                cache_hit = True
                duration = _read_cached_duration(dur_path)
                self.signals.finished.emit(self.file_path, cache_path, duration)
                return
            if os.path.exists(cache_path) and not _valid_cached_thumbnail(cache_path):
                # Never retain a corrupt generation just because its filename
                # still matches the current source fingerprint.
                try:
                    os.unlink(cache_path)
                except OSError:
                    pass
            if _has_recent_probe_failure(failed_path):
                # A decoder timeout or temporarily unavailable tool says
                # nothing permanent about the media. Retry unchanged files
                # after a short cooldown, including markers from older builds.
                self.signals.finished.emit(
                    self.file_path, '', _read_cached_duration(dur_path))
                return
            metadata_started = time.monotonic()
            probed = _probe_with_owned_process(
                self.file_path, self.cancel_event)
            metadata_ms = round((time.monotonic() - metadata_started) * 1000)
            if self.cancel_event.is_set():
                return
            duration = (probed.duration_seconds
                        if probed and probed.duration_seconds is not None else 0.0)
            width = probed.width if probed and probed.width else 0
            height = probed.height if probed and probed.height else 0
            fps = probed.average_fps if probed and probed.average_fps else 0.0
            video_bitrate = (
                probed.video_bitrate_bps if probed and probed.video_bitrate_bps else 0)
            total_bitrate = (
                probed.total_bitrate_bps if probed and probed.total_bitrate_bps else 0)

            # Metadata is useful even if thumbnail decoding later fails.
            os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
            if probed is not None:
                _write_cached_duration(
                    dur_path, duration, width, height, fps,
                    video_bitrate, total_bitrate)

            image = _decode_thumbnail_with_owned_process(
                self.file_path, self.cancel_event)
            if image is None:
                if self.cancel_event.is_set():
                    return
                try:
                    Path(failed_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(failed_path).touch()
                except OSError:
                    pass
                self.signals.finished.emit(self.file_path, '', int(duration))
                return

            os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
            if not os.path.exists(cache_path):
                # QImage decode/scaling stays on this QRunnable. Only the
                # thumbnail-sized JPEG reaches the UI and cache.
                temp_path = cache_path + f'.tmp-{os.getpid()}-{threading.get_ident()}.jpg'
                if image.save(temp_path, 'JPG', 85):
                    os.replace(temp_path, cache_path)
                else:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
            # Persist full metadata so ClipViewer can skip its own cv2.VideoCapture
            # on subsequent opens — this is what removes the first-launch lag for
            # clips that already appear on the grid.
            self.signals.finished.emit(
                self.file_path, cache_path, int(duration))
        except Exception as error:
            # Thumbnail/metadata enrichment must never invalidate a clip that
            # was already transactionally published by the engine.
            if self.cancel_event.is_set():
                return
            emit_event(
                'library', 'thumbnail_generation_failed', state='FAILED',
                detail=f'{type(error).__name__}: {error}')
            try:
                if cache_path:
                    os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
                    Path(_get_negative_cache_path(cache_path)).touch()
            except (OSError, UnboundLocalError):
                pass
            self.signals.finished.emit(self.file_path, '', 0)
        finally:
            try:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                maybe_prune_thumbnail_cache(THUMB_CACHE_DIR)
            except Exception as error:
                # Cache maintenance is optional; it must never prevent the
                # terminal diagnostic signal that releases the job owner.
                emit_event(
                    'library', 'thumbnail_cache_maintenance_failed',
                    state='FAILED',
                    detail=f'{type(error).__name__}: {error}')
            finally:
                self.signals.diagnostic_finished.emit(
                    round((time.monotonic() - started) * 1000),
                    metadata_ms, cache_hit)


class _ImageThumbnailSignals(QObject):
    finished = Signal(str, object)  # file_path, thumbnail-sized QImage
    diagnostic_finished = Signal(int, int, bool)


class _ImageThumbnailWorker(QRunnable):
    """Decode screenshots with QImageReader away from the Qt GUI thread."""

    def __init__(self, file_path: str, cancel_event=None):
        super().__init__()
        self.file_path = file_path
        self.cancel_event = cancel_event or threading.Event()
        self.signals = _ImageThumbnailSignals()

    def run(self):
        started = time.monotonic()
        cache_hit = False
        cache_path = ''
        temp_path = ''
        try:
            cache_path = _get_cached_thumb_path(self.file_path)
            failed_path = _get_negative_cache_path(cache_path)
            if self.cancel_event.is_set():
                return
            if os.path.exists(cache_path) and _valid_cached_thumbnail(cache_path):
                cache_hit = True
                reader = QImageReader(cache_path)
                reader.setAutoTransform(True)
                image = reader.read()
                self.signals.finished.emit(self.file_path, image)
                return
            if os.path.exists(cache_path):
                try:
                    os.unlink(cache_path)
                except OSError:
                    pass
            if _has_recent_probe_failure(failed_path):
                self.signals.finished.emit(self.file_path, QImage())
                return
            reader = QImageReader(self.file_path)
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and (size.width() > 640 or size.height() > 360):
                size.scale(QSize(640, 360), Qt.AspectRatioMode.KeepAspectRatio)
                reader.setScaledSize(size)
            image = reader.read()
            if self.cancel_event.is_set():
                return
            if image.isNull():
                raise ValueError('image decoder returned an empty image')
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            temp_path = cache_path + f'.tmp-{os.getpid()}-{threading.get_ident()}.jpg'
            if not image.save(temp_path, 'JPG', 85):
                raise OSError('thumbnail cache write failed')
            os.replace(temp_path, cache_path)
            self.signals.finished.emit(self.file_path, image)
        except Exception as error:
            if self.cancel_event.is_set():
                return
            emit_event(
                'library', 'thumbnail_generation_failed', state='FAILED',
                detail=f'{type(error).__name__}: {error}')
            try:
                if cache_path:
                    Path(_get_negative_cache_path(cache_path)).parent.mkdir(
                        parents=True, exist_ok=True)
                    Path(_get_negative_cache_path(cache_path)).touch()
            except (OSError, UnboundLocalError):
                pass
            self.signals.finished.emit(self.file_path, QImage())
        finally:
            try:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                if cache_path:
                    maybe_prune_thumbnail_cache(THUMB_CACHE_DIR)
            except Exception as error:
                # Cache maintenance is optional; it must not strand the
                # in-flight owner by suppressing the terminal signal.
                emit_event(
                    'library', 'thumbnail_cache_maintenance_failed',
                    state='FAILED',
                    detail=f'{type(error).__name__}: {error}')
            finally:
                self.signals.diagnostic_finished.emit(
                    round((time.monotonic() - started) * 1000), 0, cache_hit)


class _FileCollectSignals(QObject):
    # raw_files: set[str], sorted_pairs: list[(mtime, path)], imported: set[str], subdirs: list[str]
    finished = Signal(object, object, object, object)
    # generation, bounded discovery snapshot.  The final ``finished`` result
    # remains authoritative for global ordering and overlap deduplication.
    batch = Signal(int, object)


class _FileCollectWorker(QRunnable):
    """Scans folders, applies filter, sorts by mtime — all off the main thread."""

    def __init__(self, clips_dir: str, import_dirs: list,
                 filter_: str, sort_: str, cancel_event=None):
        super().__init__()
        self.clips_dir   = clips_dir
        self.import_dirs = import_dirs
        self.filter_     = filter_
        self.sort_       = sort_
        self.cancel_event = cancel_event or threading.Event()
        self.result: LibraryScanResult | None = None
        self.signals     = _FileCollectSignals()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self):
        generation = int(getattr(self, 'generation', 0))
        self.result = scan_library(
            self.clips_dir,
            self.import_dirs,
            filter_=self.filter_,
            sort_=self.sort_,
            cancel_event=self.cancel_event,
            on_batch=lambda records: self.signals.batch.emit(
                generation, records),
        )
        records = self.result.records
        found = {record.path for record in self.result.all_records}
        imported = {record.path for record in self.result.all_records
                    if record.imported}
        pairs = [(record.mtime, record.path) for record in records]
        self.signals.finished.emit(
            found, pairs, imported, list(self.result.watched_directories))


# ClipThumbnail — Medal-style card: thumb on top, info row below

class ClipThumbnail(QFrame):
    clicked          = Signal(str)
    opened           = Signal(str, QPixmap, QRect)   # video-only: path, thumb, global card rect
    deleted          = Signal(str)
    upload_requested = Signal(str)

    def __init__(self, file_path: str, is_video: bool = True, imported: bool = False,
                  upload_enabled: bool = False, uploaded: bool = False,
                  upload_info: dict | None = None, ready: bool = True,
                  card_width: int = _CARD_W, parent=None,
                  clips_root: str | None = None,
                  defer_image_load: bool = False):
        super().__init__(parent)
        self.file_path      = file_path
        self.is_video       = is_video
        self.imported       = imported
        self.upload_enabled = upload_enabled
        self.uploaded       = uploaded
        self.upload_info    = dict(upload_info or {})
        self._clips_root    = clips_root or os.path.expanduser('~/FTHR_Clips')
        self.upload_link    = ''
        self.ready          = ready
        # Wait for worker-side thumbnail and metadata enrichment before opening.
        # Otherwise a click can trigger synchronous decoding in ClipViewer; large
        # screenshots also need decoding off the GUI thread.
        self._thumbnail_ready = False
        self._card_width = max(_CARD_MIN_W, min(_CARD_MAX_W, int(card_width)))
        self._thumb_height = max(1, round(self._card_width * 9 / 16))
        self._thumb_pixmap = QPixmap()
        self.setObjectName('clipCard')
        self.setFixedSize(
            self._card_width, self._thumb_height + _CARD_BODY_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._fade_anim: QPropertyAnimation | None = None
        self._setup_ui()
        if not self.ready:
            self.share_btn.setEnabled(False)
            self.menu_btn.setEnabled(False)
            self.setCursor(Qt.CursorShape.ArrowCursor)
        if not is_video and not defer_image_load:
            # Compatibility for direct ClipThumbnail callers. Production
            # ClipGrid always opts into the worker below.
            self._load_image_thumbnail()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Thumbnail container
        thumb = QFrame(self)
        self._thumb_frame = thumb
        thumb.setObjectName('cardThumb')
        thumb.setFixedSize(self._card_width, self._thumb_height)

        self.thumb_label = QLabel(thumb)
        self.thumb_label.setGeometry(0, 0, self._card_width, self._thumb_height)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setScaledContents(False)
        self.thumb_label.setStyleSheet('background: transparent; border: none;')

        # Center play glyph (video only)
        if self.is_video:
            self._play_icon = QLabel('▶', thumb)
            self._play_icon.setGeometry(
                0, 0, self._card_width, self._thumb_height)
            self._play_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._play_icon.setStyleSheet(
                f'color: {_rgba(Colors.TEXT, 210)}; font-size: 36px;'
                ' background: transparent; border: none;'
            )
            self._play_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._play_icon.setVisible(False)

        # Top-right duration badge
        if self.is_video:
            self.duration_label = QLabel('0:00', thumb)
            self.duration_label.setObjectName('cardDurationBadge')
            self.duration_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.duration_label.setStyleSheet(
                f'QLabel#cardDurationBadge {{'
                f' color: {Colors.TEXT};'
                f' background: {_rgba(Colors.SURFACE_1, 170)};'
                f' border-radius: 0px; padding: 2px 9px;'
                f' font-family: {Fonts.BODY}; font-size: {Fonts.SIZE_LABEL}px;'
                f' font-weight: bold; letter-spacing: 1px;'
                f'}}'
            )
            self.duration_label.adjustSize()
            self.duration_label.move(
                self._card_width - self.duration_label.width() - 10, 10)

        # Bottom-left imported badge (only for clips from imported folders)
        self._imported_badge_h = 0
        self._imported_badge = None
        if self.imported:
            imp = QLabel('IMPORTED', thumb)
            self._imported_badge = imp
            imp.setObjectName('cardImportedBadge')
            imp.setStyleSheet(
                f'QLabel#cardImportedBadge {{'
                f' color: {Colors.ACCENT};'
                f' background: {_rgba(Colors.SURFACE_1, 180)};'
                f' border-radius: 0px; padding: 2px 7px;'
                f' font-family: {Fonts.DISPLAY}; font-size: {Fonts.SIZE_MICRO}px;'
                f' font-weight: bold; letter-spacing: 2px;'
                f'}}'
            )
            imp.adjustSize()
            imp.move(8, self._thumb_height - imp.height() - 8)
            imp.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._imported_badge_h = imp.height() + 4   # used to stack UPLOADED above it

        self._finalizing_badge = None
        if not self.ready:
            finalizing = QLabel('FINALIZING', thumb)
            self._finalizing_badge = finalizing
            finalizing.setStyleSheet(
                f'color: {Colors.TEXT}; '
                f'background: {_rgba(Colors.WARNING, 220)}; '
                f'font-family: {Fonts.DISPLAY}; '
                f'font-size: {Fonts.SIZE_MICRO}px; '
                'padding: 3px 7px; font-weight: bold;')
            finalizing.adjustSize()
            finalizing.move(8, 8)

        # "UPLOADED" badge — pre-created, shown/hidden dynamically
        self._upload_badge = QLabel('UPLOADED', thumb)
        self._upload_badge.setObjectName('cardUploadedBadge')
        self._upload_badge.setStyleSheet(
            f'QLabel#cardUploadedBadge {{'
            f' color: {Colors.BG};'
            f' background: {_rgba(Colors.SUCCESS, 210)};'
            f' border-radius: 0px; padding: 2px 7px;'
            f' font-family: {Fonts.DISPLAY}; font-size: {Fonts.SIZE_MICRO}px;'
            f' font-weight: bold; letter-spacing: 2px;'
            f'}}'
        )
        self._upload_badge.adjustSize()
        _badge_bottom = self._thumb_height - 8 - self._imported_badge_h
        self._upload_badge.move(8, _badge_bottom - self._upload_badge.height())
        self._upload_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._upload_badge.setVisible(self.uploaded)

        layout.addWidget(thumb)

        # Card body (game / title / share+menu / time-ago)
        body = QFrame(self)
        body.setObjectName('cardBody')
        self._body_frame = body
        body.setFixedSize(self._card_width, _CARD_BODY_H)

        bl = QVBoxLayout(body)
        bl.setContentsMargins(12, 8, 8, 8)
        bl.setSpacing(2)

        # Top row: game name (small dim) + share + menu
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(6)

        self.game_label = QLabel(
            _game_name_from_path(self.file_path, self._clips_root))
        self.game_label.setObjectName('cardGame')
        top_row.addWidget(self.game_label)
        top_row.addStretch(1)

        self.share_btn = QPushButton('⤴')
        self.share_btn.setObjectName('cardIconBtn')
        self.share_btn.setFixedSize(22, 22)
        self.share_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.share_btn.setToolTip('Share')
        self.share_btn.clicked.connect(self._on_share_click)
        top_row.addWidget(self.share_btn)

        self.menu_btn = QPushButton('⋯')
        self.menu_btn.setObjectName('cardIconBtn')
        self.menu_btn.setFixedSize(22, 22)
        self.menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_btn.clicked.connect(self._show_menu)
        top_row.addWidget(self.menu_btn)
        bl.addLayout(top_row)

        # Title row
        self.title_label = QLabel(_clip_title_from_filename(self.file_path))
        self.title_label.setObjectName('cardTitle')
        self.title_label.setWordWrap(False)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bl.addWidget(self.title_label)

        # Time-ago row
        try:
            mtime = os.path.getmtime(self.file_path)
        except OSError:
            mtime = 0.0
        self.time_label = QLabel(_humanize_relative(mtime))
        self.time_label.setObjectName('cardTime')
        bl.addWidget(self.time_label)

        # Returned provider links are the useful post-upload action. Keep the
        # action row out of the card entirely until history has a valid URL.
        bl.addSpacing(3)
        self._link_actions = QWidget(body)
        link_row = QHBoxLayout(self._link_actions)
        link_row.setContentsMargins(0, 0, 0, 0)
        link_row.setSpacing(5)

        self.copy_link_btn = QPushButton('COPY LINK')
        self.copy_link_btn.setObjectName('cardLinkBtn')
        self.copy_link_btn.setFixedHeight(22)
        self.copy_link_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_link_btn.setToolTip('Copy the uploaded file link')
        self.copy_link_btn.clicked.connect(self._copy_upload_link)
        link_row.addWidget(self.copy_link_btn, 1)

        self.open_link_btn = QPushButton('OPEN LINK')
        self.open_link_btn.setObjectName('cardLinkBtn')
        self.open_link_btn.setFixedHeight(22)
        self.open_link_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_link_btn.setToolTip('Open the uploaded file link')
        self.open_link_btn.clicked.connect(self._open_upload_link)
        link_row.addWidget(self.open_link_btn, 1)
        bl.addWidget(self._link_actions)

        layout.addWidget(body)

        self.setStyleSheet(self._card_qss())
        self.set_upload_info(self.upload_info)

    @staticmethod
    def _card_qss() -> str:
        return f'''
            QFrame#clipCard {{
                background-color: {Colors.CARD_BG};
                border: 1px solid {Colors.CARD_BORDER};
                border-radius: {Sizes.RADIUS_CARD}px;
            }}
            QFrame#clipCard:hover {{
                background-color: {Colors.CARD_BG_HI};
                border-color: {Colors.BORDER_HI};
            }}
            QFrame#cardThumb {{
                background-color: {Colors.SURFACE_3};
                border: none;
                border-top-left-radius: {Sizes.RADIUS_CARD}px;
                border-top-right-radius: {Sizes.RADIUS_CARD}px;
            }}
            QFrame#cardBody {{
                background-color: transparent;
                border: none;
            }}
            QLabel#cardGame {{
                color: {Colors.TEXT_DIM};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_MICRO}px;
                font-weight: bold;
                letter-spacing: 2px;
                background: transparent;
            }}
            QLabel#cardTitle {{
                color: {Colors.TEXT};
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_BODY_L}px;
                font-weight: bold;
                background: transparent;
            }}
            QLabel#cardTime {{
                color: {Colors.TEXT_MUTED};
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_LABEL}px;
                background: transparent;
            }}
            QPushButton#cardIconBtn {{
                background: transparent;
                border: none;
                color: {Colors.TEXT_DIM};
                font-size: {Fonts.SIZE_H3}px;
                font-weight: bold;
            }}
            QPushButton#cardIconBtn:hover {{
                color: {Colors.ACCENT};
            }}
            QPushButton#cardLinkBtn {{
                background-color: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER};
                color: {Colors.TEXT_DIM};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_MICRO}px;
                font-weight: bold;
                letter-spacing: 1px;
                padding: 0 4px;
            }}
            QPushButton#cardLinkBtn:hover {{
                background-color: {Colors.SURFACE_3};
                border-color: {Colors.ACCENT};
                color: {Colors.ACCENT};
            }}
            QPushButton#cardLinkBtn:disabled {{
                background-color: transparent;
                border-color: {Colors.CARD_BORDER};
                color: {Colors.TEXT_MUTED};
            }}
        '''

    def set_uploaded(self, val: bool):
        self.uploaded = val
        self._upload_badge.setVisible(val)

    def set_ready(self, ready: bool) -> None:
        """Update finalization state without replacing the entire card."""
        ready = bool(ready)
        if self.ready == ready:
            return
        self.ready = ready
        self.share_btn.setEnabled(ready)
        self.menu_btn.setEnabled(ready)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if ready else Qt.CursorShape.ArrowCursor)
        if ready:
            if self._finalizing_badge is not None:
                self._finalizing_badge.deleteLater()
                self._finalizing_badge = None
            return

        if self._finalizing_badge is None:
            finalizing = QLabel('FINALIZING', self._thumb_frame)
            finalizing.setStyleSheet(
                f'color: {Colors.TEXT}; '
                f'background: {_rgba(Colors.WARNING, 220)}; '
                f'font-family: {Fonts.DISPLAY}; '
                f'font-size: {Fonts.SIZE_MICRO}px; '
                'padding: 3px 7px; font-weight: bold;')
            finalizing.adjustSize()
            finalizing.move(8, 8)
            finalizing.show()
            self._finalizing_badge = finalizing

    @staticmethod
    def _upload_url(info: dict | None) -> str:
        if not isinstance(info, dict):
            return ''
        for key in ('url', 'raw_url'):
            value = str(info.get(key, '') or '').strip()
            if value.startswith(('https://', 'http://')):
                return value
        return ''

    def set_upload_info(self, info: dict | None):
        """Update the link actions from the provider's persisted upload result."""
        self.upload_info = dict(info or {})
        self.upload_link = self._upload_url(self.upload_info)
        enabled = bool(self.upload_link)
        self._link_actions.setVisible(enabled)
        self.copy_link_btn.setEnabled(enabled)
        self.open_link_btn.setEnabled(enabled)

    def _copy_upload_link(self):
        if self.upload_link:
            QApplication.clipboard().setText(self.upload_link)

    def _open_upload_link(self):
        if self.upload_link:
            QDesktopServices.openUrl(QUrl(self.upload_link))

    def resize_card(self, width: int):
        """Resize a card without recreating it or distorting its media."""
        width = max(_CARD_MIN_W, min(_CARD_MAX_W, int(width)))
        if width == self._card_width:
            return
        self._card_width = width
        self._thumb_height = max(1, round(width * 9 / 16))
        self.setFixedSize(width, self._thumb_height + _CARD_BODY_H)
        self._thumb_frame.setFixedSize(width, self._thumb_height)
        self.thumb_label.setGeometry(0, 0, width, self._thumb_height)
        self._body_frame.setFixedSize(width, _CARD_BODY_H)
        if hasattr(self, '_play_icon'):
            self._play_icon.setGeometry(0, 0, width, self._thumb_height)
        if hasattr(self, 'duration_label'):
            self.duration_label.move(
                width - self.duration_label.width() - 10, 10)
        if self._imported_badge is not None:
            self._imported_badge.move(
                8, self._thumb_height - self._imported_badge.height() - 8)
        badge_bottom = self._thumb_height - 8 - self._imported_badge_h
        self._upload_badge.move(
            8, badge_bottom - self._upload_badge.height())
        self._render_thumbnail()

    def _render_thumbnail(self):
        """Fit media inside the 16:9 surface; never stretch or crop it."""
        if self._thumb_pixmap.isNull():
            self.thumb_label.clear()
            return
        fitted = self._thumb_pixmap.scaled(
            self._card_width,
            self._thumb_height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.thumb_label.setPixmap(fitted)

    def refresh_theme(self):
        self.setStyleSheet(self._card_qss())
        if hasattr(self, '_play_icon'):
            self._play_icon.setStyleSheet(
                f'color: {_rgba(Colors.TEXT, 210)}; font-size: 36px;'
                ' background: transparent; border: none;')
        if hasattr(self, 'duration_label'):
            self.duration_label.setStyleSheet(
                f'QLabel#cardDurationBadge {{'
                f' color: {Colors.TEXT};'
                f' background: {_rgba(Colors.SURFACE_1, 170)};'
                f' border-radius: 0px; padding: 2px 9px;'
                f' font-family: {Fonts.BODY}; font-size: {Fonts.SIZE_LABEL}px;'
                f' font-weight: bold; letter-spacing: 1px; }}')
        if self._imported_badge is not None:
            self._imported_badge.setStyleSheet(
                f'QLabel#cardImportedBadge {{'
                f' color: {Colors.ACCENT};'
                f' background: {_rgba(Colors.SURFACE_1, 180)};'
                f' border-radius: 0px; padding: 2px 7px;'
                f' font-family: {Fonts.DISPLAY}; font-size: {Fonts.SIZE_MICRO}px;'
                f' font-weight: bold; letter-spacing: 2px; }}')
        if self._finalizing_badge is not None:
            self._finalizing_badge.setStyleSheet(
                f'color: {Colors.TEXT}; '
                f'background: {_rgba(Colors.WARNING, 220)}; '
                f'font-family: {Fonts.DISPLAY}; '
                f'font-size: {Fonts.SIZE_MICRO}px; '
                'padding: 3px 7px; font-weight: bold;')
        self._upload_badge.setStyleSheet(
            f'QLabel#cardUploadedBadge {{'
            f' color: {Colors.BG};'
            f' background: {_rgba(Colors.SUCCESS, 210)};'
            f' border-radius: 0px; padding: 2px 7px;'
            f' font-family: {Fonts.DISPLAY}; font-size: {Fonts.SIZE_MICRO}px;'
            f' font-weight: bold; letter-spacing: 2px; }}')

    def _on_share_click(self):
        # Share is wired through ClipViewer for now — open the viewer
        if not (self.is_video and self.ready):
            return
        self._emit_opened()

    def _emit_opened(self):
        if not (self.is_video and self.ready):
            return
        # Pass the cached source thumbnail to the editor, not the pixmap that
        # has already been fitted to this card.  The latter is intentionally
        # display-sized and can be much smaller than the editor preview, so it
        # made the clip look soft until QMediaPlayer delivered its first frame
        # (and remained soft while the editor was paused before that happened).
        px = self._thumb_pixmap if not self._thumb_pixmap.isNull() else QPixmap()
        global_rect = QRect(self.mapToGlobal(QPoint(0, 0)), self.size())
        self.opened.emit(self.file_path, px, global_rect)

    def _show_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(context_menu_qss())
        open_act = menu.addAction('Open in viewer')
        explorer_act = menu.addAction('Show in Explorer')
        copy_act = menu.addAction('Copy path')
        if self.upload_enabled:
            menu.addSeparator()
            upload_act = menu.addAction('Upload')
        else:
            upload_act = None
        menu.addSeparator()
        del_act = menu.addAction(
            'Linked original — managed outside FTHR'
            if self.imported else 'Delete')
        del_act.setEnabled(not self.imported)
        action = menu.exec(self.menu_btn.mapToGlobal(QPoint(0, self.menu_btn.height())))
        if action == open_act:
            self._on_share_click()
        elif action == explorer_act:
            _show_in_file_manager(self.file_path)
        elif action == copy_act:
            QApplication.clipboard().setText(self.file_path)
        elif upload_act and action == upload_act:
            self.upload_requested.emit(self.file_path)
        elif action == del_act:
            self._confirm_delete()

    def _confirm_delete(self):
        if not self.ready:
            return
        ownership = classify_media_path(self.file_path, self._clips_root, [])
        if self.imported or ownership is not MediaOwnership.FTHR_OWNED:
            FthrMessageDialog.information(
                self,
                'Linked Original Protected',
                'Imported files stay in their original folder. FTHR will not '
                'delete this file with the generic Delete action.',
            )
            return
        reply = FthrMessageDialog.question(
            self, 'Delete',
            f'Delete {os.path.basename(self.file_path)}?',
        )
        if not reply:
            return

        # Resolve cache paths BEFORE the delete — _get_cached_thumb_path needs
        # the file's mtime, which won't be available once the file is gone.
        cache_path = None
        dur_path = None
        try:
            cache_path = _get_cached_thumb_path(self.file_path)
            dur_path = _get_cached_duration_path(cache_path)
        except OSError:
            pass

        import time as _time
        for attempt in range(3):
            try:
                os.remove(self.file_path)
                # Prune thumbnail cache so orphaned .jpg/.dur files don't pile up.
                for p in (cache_path, dur_path):
                    if p:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                self.deleted.emit(self.file_path)
                return
            except OSError as e:
                # Win32 error 32 = "file in use by another process".
                # Common cause: clip still being encoded/played.
                # Retry twice with a short delay before giving up.
                if sys.platform == 'win32' and getattr(e, 'winerror', 0) == 32:
                    if attempt < 2:
                        _time.sleep(0.2)
                        continue
                    FthrMessageDialog.warning(
                        self, 'Delete Failed',
                        f'"{os.path.basename(self.file_path)}" is still in use.\n\n'
                        'Close the clip viewer and wait for any active clip save\n'
                        'or upload to finish, then try again.',
                    )
                    return
                FthrMessageDialog.warning(self, 'Delete Failed', str(e))
                return

    # Hover (large play glyph + slight border highlight)

    def enterEvent(self, event):
        if self.is_video and hasattr(self, '_play_icon'):
            self._play_icon.setVisible(True)

    def leaveEvent(self, event):
        if self.is_video and hasattr(self, '_play_icon'):
            self._play_icon.setVisible(False)

    def contextMenuEvent(self, event):
        # Right-click anywhere on card — same options as the ⋯ button
        menu = QMenu(self)
        menu.setStyleSheet(context_menu_qss())
        open_act = menu.addAction('Open in viewer')
        explorer_act = menu.addAction('Show in Explorer')
        copy_act = menu.addAction('Copy path')
        if self.upload_enabled:
            menu.addSeparator()
            upload_act = menu.addAction('Upload')
        else:
            upload_act = None
        menu.addSeparator()
        del_act = menu.addAction(
            'Linked original — managed outside FTHR'
            if self.imported else 'Delete')
        del_act.setEnabled(not self.imported)
        action = menu.exec(event.globalPos())
        if action == open_act:
            self._on_share_click()
        elif action == explorer_act:
            _show_in_file_manager(self.file_path)
        elif action == copy_act:
            QApplication.clipboard().setText(self.file_path)
        elif upload_act and action == upload_act:
            self.upload_requested.emit(self.file_path)
        elif action == del_act:
            self._confirm_delete()

    def fade_in(self, delay_ms: int = 0):
        # An opacity effect caches the whole card's backing store. On Windows
        # it can remain blank across background pause/resume and reparenting,
        # even after the thumbnail arrived. Publish cards without that cache.
        self.setGraphicsEffect(None)
        self.update()

    def _load_image_thumbnail(self):
        reader = QImageReader(self.file_path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and (size.width() > 640 or size.height() > 360):
            size.scale(QSize(640, 360), Qt.AspectRatioMode.KeepAspectRatio)
            reader.setScaledSize(size)
        source = reader.read()
        if source.isNull():
            self._thumbnail_ready = True
            return
        # Never retain a camera/screenshot at source resolution for a library
        # card. The viewer reopens the original on demand.
        self._thumb_pixmap = QPixmap.fromImage(source)
        self._render_thumbnail()
        self._thumbnail_ready = True

    def set_image_thumbnail(self, image: QImage | None) -> None:
        """Install a worker-decoded, thumbnail-sized image on the GUI thread."""
        if image is not None and not image.isNull():
            self._thumb_pixmap = QPixmap.fromImage(image)
            self._render_thumbnail()
        self._thumbnail_ready = image is not None and not image.isNull()

    def set_video_thumbnail(self, cache_path: str, duration: int):
        if cache_path and os.path.exists(cache_path):
            self._thumb_pixmap = QPixmap(cache_path)
            self._render_thumbnail()
        if self.is_video and hasattr(self, 'duration_label'):
            mins, secs = divmod(duration, 60)
            self.duration_label.setText(f'{mins}:{secs:02d}' if duration > 0 else '--:--')
            self.duration_label.adjustSize()
            self.duration_label.move(
                self._card_width - self.duration_label.width() - 10, 10)
        if self.is_video:
            self._thumbnail_ready = not self._thumb_pixmap.isNull()

    def mousePressEvent(self, event):
        if not self.ready:
            return
        if event.button() == Qt.MouseButton.LeftButton:
            # Don't trigger card-open if the click was on a child button —
            # those handle their own actions. We can detect by checking the
            # local target widget.
            local = event.position().toPoint()
            child = self.childAt(local)
            if isinstance(child, QPushButton):
                return
            self.clicked.emit(self.file_path)
            if self.is_video:
                self._emit_opened()


# ClipGrid — filter bar + date sections + grid of cards

class ClipGrid(QWidget):
    clip_clicked          = Signal(str)
    clip_opened           = Signal(str, QPixmap, QRect)
    screenshot_clicked    = Signal(str)
    clip_upload_requested = Signal(str)

    # Compact gutters keep the media surface visually dominant while still
    # separating cards at desktop widths.
    _H_MARGIN = 48
    _GRID_SPACING = 12

    def __init__(self, settings_manager=None, parent=None):
        super().__init__(parent)
        self._sm              = settings_manager
        self._upload_checker  = None   # callable(path) -> bool
        self._upload_info_checker = None  # callable(path) -> dict | None
        self._upload_enabled  = None   # callable() -> bool
        self._readiness_checker = None  # callable(path) -> bool
        self.clips_dir      = str(clips_directory_from(self._sm))
        self.thumbnails     = []
        self._thumb_widgets = {}
        self._known_files   = set()
        self._imported_files: set = set()
        # Save completion updates one card in place.  Keep the date-section
        # layouts indexed so adding a clip never has to tear down and recreate
        # every card in a large library.
        self._section_grids: dict[str, QGridLayout] = {}
        self._section_files: dict[str, list[str]] = {}
        self._section_widgets: dict[str, QWidget] = {}
        self._virtual_ready: dict[str, bool] = {}
        self._virtual_sections: dict[str, QWidget] = {}
        self._card_virtual_sections: dict[str, str] = {}
        self._virtual_initialized = False
        self._records: dict[str, LibraryRecord] = {}
        self._scan_generation = 0
        self._active_scan_worker: _FileCollectWorker | None = None
        self._pending_scan_request: tuple[str, str] | None = None
        self._shutdown_started = False
        self._thumbnail_jobs_inflight: set[str] = set()
        self._deferred_thumbnail_paths: set[str] = set()
        self._thumbnail_job_fingerprints: dict[str, str] = {}
        # The set above is kept for bounded queue metrics and compatibility,
        # while this token map is the ownership authority.  A late terminal
        # signal from an older generation must never remove a replacement job
        # for the same path.
        self._thumbnail_job_owners: dict[str, tuple[int, str]] = {}
        # Keep signal senders alive until their queued terminal delivery. A
        # QRunnable may finish before the GUI gets back to its event queue.
        self._thumbnail_workers: dict[str, QRunnable] = {}
        self._thumbnail_cancel_event = threading.Event()
        self._thumbnail_generation = 0
        self._pending_saved_clips: dict[str, bool] = {}
        # CLIP_SAVED can arrive while discovery is already in flight.  Keep
        # the published record until that generation is merged so the final
        # scan cannot erase a valid just-saved clip it raced with.
        self._scan_upserts: dict[str, LibraryRecord] = {}
        self._thread_pool   = QThreadPool()
        # Cap at 2 workers. FFmpeg thumbnail decode is already heavy on disk
        # and CPU; throwing 16 threads at it just thrashes and makes everything
        # slower. 400ms of profiling led me here. don't touch this.
        self._thread_pool.setMaxThreadCount(2)
        # File scans must not sit behind a long queue of video decoders.  A
        # separate single-worker pool makes filter/sort changes respond as soon
        # as the current scan (if any) completes.
        self._scan_thread_pool = QThreadPool()
        self._scan_thread_pool.setMaxThreadCount(1)
        # Pre-compute the column count from the primary screen's available
        # width so the first paint already matches the maximized window.
        # Without this, the grid renders at 3 cols then snaps to N cols once
        # the resize event fires after showMaximized() — visible lag.
        initial_width = self._initial_viewport_width()
        (self._current_columns,
         self._current_card_widths) = self._layout_metrics(initial_width)

        self._filter = 'all'
        self._sort = 'newest'
        self._background_paused = False
        self._background_refresh_pending = False
        self._in_transition = False
        self._transition_anim = None
        # Keep transition workers (and, critically, their signal objects) alive
        # until the queued completion callback has run on the GUI thread.
        self._transition_workers: dict[int, _FileCollectWorker] = {}
        self._diagnostic_scan_started: dict[int, float] = {}
        self._thumbnail_jobs_submitted = 0
        self._thumbnail_jobs_completed = 0
        self._thumbnail_diagnostics_completed = 0
        self._thumbnail_total_elapsed_ms = 0
        self._metadata_total_elapsed_ms = 0
        self._thumbnail_cache_hits = 0
        self._thumbnail_queue_peak = 0

        # Watcher must exist before _load_clips() runs
        self._watcher = QFileSystemWatcher()
        if os.path.exists(self.clips_dir):
            self._watcher.addPath(self.clips_dir)
        self._watcher.directoryChanged.connect(self._on_dir_changed)

        self._setup_ui()
        self._load_clips()

        self._debounce_timer = QTimer()
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._load_clips)

        # Debounced resize — only rebuilds the grid when columns would change.
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._on_resize_settled)

        self.refresh_timer = QTimer()
        self.refresh_timer.setInterval(30000)
        self.refresh_timer.timeout.connect(self._load_clips)
        self.refresh_timer.start()

        # A scroll event changes which small subset of cards is materialized.
        # Connecting to the nearest scroll area's bar keeps the public ClipGrid
        # API unchanged and avoids a per-card event filter.
        QTimer.singleShot(0, self._connect_scroll_updates)

    def _connect_scroll_updates(self) -> None:
        parent = self.parentWidget()
        for _ in range(4):
            if parent is None:
                break
            bar = getattr(parent, 'verticalScrollBar', None)
            if callable(bar):
                try:
                    bar().valueChanged.connect(self._update_virtualized_cards)
                except (AttributeError, RuntimeError):
                    pass
                break
            parent = parent.parentWidget()

    def is_linked_import(self, path: str) -> bool:
        target = os.path.normcase(os.path.realpath(path))
        return any(
            os.path.normcase(os.path.realpath(candidate)) == target
            for candidate in self._imported_files
        )

    def set_upload_checker(self, checker):
        """checker(path: str) -> bool  — True if the clip has been uploaded."""
        self._upload_checker = checker
        self._refresh_upload_states()

    def set_upload_info_checker(self, checker):
        """checker(path: str) -> dict | None — persisted provider result."""
        self._upload_info_checker = checker
        self._refresh_upload_states()

    def set_upload_enabled_checker(self, checker):
        """checker() -> bool  — True if uploads are enabled (shows Upload menu item)."""
        self._upload_enabled = checker
        enabled = bool(checker and checker())
        for card in self._thumb_widgets.values():
            card.upload_enabled = enabled

    def set_readiness_checker(self, checker):
        self._readiness_checker = checker

    def _refresh_upload_states(self):
        """Refresh badges and returned-link actions on cards already in the grid."""
        for path, card in self._thumb_widgets.items():
            uploaded = bool(self._upload_checker and self._upload_checker(path))
            info = (
                self._upload_info_checker(path)
                if uploaded and self._upload_info_checker else None)
            card.set_upload_info(info)
            card.set_uploaded(uploaded)

    def refresh_theme(self):
        for card in self._thumb_widgets.values():
            card.refresh_theme()

    def _viewport_width(self) -> int:
        """The scroll-area viewport is our parent — read its width so column
        calculation works even when the grid layout is wider than the viewport."""
        vp = self.parentWidget()
        return vp.width() if vp else self.width()

    def _initial_viewport_width(self) -> int:
        """Best-guess viewport width before the parent has been laid out.

        This lets the first build match the maximized window and avoids a
        visible reflow during startup.
        """
        screen = QApplication.primaryScreen()
        if screen is None:
            return 1100
        return screen.availableGeometry().width()

    @classmethod
    def _layout_metrics(cls, viewport_width: int) -> tuple[int, list[int]]:
        """Return column count and exact per-column widths for a viewport."""
        available = max(_CARD_MIN_W, int(viewport_width) - cls._H_MARGIN)
        cols = max(
            1,
            int((available + cls._GRID_SPACING)
                // (_CARD_W + cls._GRID_SPACING)),
        )
        while cols > 1:
            card_width = (
                available - cls._GRID_SPACING * (cols - 1)) // cols
            if card_width >= _CARD_MIN_W:
                break
            cols -= 1
        while True:
            card_width = (
                available - cls._GRID_SPACING * (cols - 1)) // cols
            if card_width <= _CARD_MAX_W:
                break
            cols += 1

        usable = available - cls._GRID_SPACING * (cols - 1)
        base, remainder = divmod(usable, cols)
        widths = [base + (1 if col < remainder else 0)
                  for col in range(cols)]
        return cols, widths

    def _initial_columns_from_screen(self) -> int:
        """Compatibility helper used by a few downstream integrations."""
        return self._layout_metrics(self._initial_viewport_width())[0]

    def _compute_columns(self) -> int:
        return self._layout_metrics(self._viewport_width())[0]

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, '_vp_filter', False):
            vp = self.parentWidget()
            if vp:
                vp.installEventFilter(self)
                self._vp_filter = True

    def eventFilter(self, obj, event):
        # Resize events fire like a machine gun while you drag a window edge.
        # Debounce so we only rebuild the grid 200ms after you STOP dragging,
        # not 60 times a second mid-drag. Your CPU thanks you.
        if event.type() == event.Type.Resize:
            self._resize_timer.start(200)
        return super().eventFilter(obj, event)

    def _on_resize_settled(self):
        new_cols, new_widths = self._layout_metrics(self._viewport_width())
        if (new_cols != self._current_columns
                or new_widths != self._current_card_widths):
            self._current_columns = new_cols
            self._current_card_widths = new_widths
            self._relayout_grids()

    def _on_dir_changed(self, path: str):
        if self._background_paused:
            self._background_refresh_pending = True
            return
        self._debounce_timer.start(500)

    def set_background_paused(self, paused: bool) -> None:
        """Suspend library scans while the containing app is in background.

        Capture/save completion can still invalidate the library through
        ``force_refresh``. While paused that work is coalesced into one refresh
        when the UI becomes active again.
        """
        paused = bool(paused)
        if paused == self._background_paused:
            return
        self._background_paused = paused
        if paused:
            # Cancel queued and active decodes cooperatively. Do not clear the
            # pool or release ownership here: a queued/active QRunnable still
            # owns its token and must reach its terminal diagnostic callback.
            # Releasing it globally would let a replacement job run alongside
            # the old worker and would make a late result able to strand or
            # remove the replacement's ownership.
            self._thumbnail_cancel_event.set()
            self._thumbnail_generation += 1
            self._scan_generation += 1
            if self._active_scan_worker is not None:
                self._active_scan_worker.cancel()
            self.refresh_timer.stop()
            self._debounce_timer.stop()
            return

        # A cancelled QRunnable that was already in the pool can finish after
        # this method returns.  Start a fresh generation before re-enriching
        # visible cards so its late signal cannot own or clear a replacement
        # job.
        self._thumbnail_cancel_event = threading.Event()
        self._requeue_visible_thumbnail_jobs()
        self.refresh_timer.start()
        if self._background_refresh_pending:
            self._background_refresh_pending = False
            self._known_files = None
            self._load_clips()
        pending = self._pending_saved_clips
        self._pending_saved_clips = {}
        for path, ready in pending.items():
            self.upsert_saved_clip(path, ready=ready)

    # UI

    def _setup_ui(self):
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(24, 18, 24, 24)
        self.layout.setSpacing(0)

        # Filter bar
        filt = QFrame()
        filt.setObjectName('filterBar')
        filt.setFixedHeight(Sizes.FILTER_H)
        fl = QHBoxLayout(filt)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(12)

        self._all_count_label = QLabel('ALL CLIPS')
        self._all_count_label.setObjectName('allCountLabel')
        fl.addWidget(self._all_count_label)

        fl.addStretch(1)

        self.filter_combo = _DropdownCombo()
        self.filter_combo.addItems(['All clips', 'Clips only',
                                    'Screenshots', 'Imported clips'])
        self.filter_combo.setStyleSheet(combo_qss())
        self.filter_combo.setMinimumWidth(120)
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        fl.addWidget(self.filter_combo)

        self.sort_combo = _DropdownCombo()
        self.sort_combo.addItems(['Newest', 'Oldest'])
        self.sort_combo.setStyleSheet(combo_qss())
        self.sort_combo.setMinimumWidth(110)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        fl.addWidget(self.sort_combo)

        self.layout.addWidget(filt)
        self.layout.addSpacing(8)

        # Section host (date-grouped grids live here)
        self._sections_host = QWidget()
        self._sections_host.setStyleSheet('background: transparent;')
        self._sections_layout = QVBoxLayout(self._sections_host)
        self._sections_layout.setContentsMargins(0, 0, 0, 0)
        self._sections_layout.setSpacing(20)
        self.layout.addWidget(self._sections_host)

        # Empty state
        self._empty_widget = QWidget()
        self._empty_widget.setStyleSheet('background: transparent;')
        ev_layout = QVBoxLayout(self._empty_widget)
        ev_layout.setContentsMargins(0, 96, 0, 0)
        ev_layout.setSpacing(0)
        ev_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self._no_clips_lbl = QLabel('NO CLIPS YET')
        self._no_clips_lbl.setStyleSheet(label_display(Colors.TEXT_GHOST, Fonts.SIZE_H1, 8))
        self._no_clips_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev_layout.addWidget(self._no_clips_lbl)

        self._empty_detail_lbl = QLabel()
        self._empty_detail_lbl.setStyleSheet(
            label_display(Colors.TEXT_DIM, Fonts.SIZE_LABEL, Fonts.TRACK_LABEL))
        self._empty_detail_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_detail_lbl.setVisible(False)
        ev_layout.addSpacing(Sizes.SPACE_2)
        ev_layout.addWidget(self._empty_detail_lbl)

        ev_layout.addSpacing(Sizes.SPACE_4)

        mark_row = QHBoxLayout()
        mark_row.setContentsMargins(0, 0, 0, 0)
        mark_row.addStretch()
        mark = QFrame()
        mark.setFixedSize(56, 2)
        mark.setStyleSheet(f'background: {Colors.ACCENT}; border: none;')
        mark_row.addWidget(mark)
        mark_row.addStretch()
        ev_layout.addLayout(mark_row)

        self.layout.addWidget(self._empty_widget)
        self.layout.addStretch()

        self.setStyleSheet(self._grid_qss())

    @staticmethod
    def _grid_qss() -> str:
        return f'''
            QFrame#filterBar {{
                background: transparent;
                border-bottom: 1px solid {Colors.SHELL_DIVIDER};
            }}
            QLabel#allCountLabel {{
                color: {Colors.TEXT};
                font-family: {Fonts.DISPLAY};
                font-weight: bold;
                font-size: {Fonts.SIZE_LABEL}px;
                letter-spacing: {Fonts.TRACK_LABEL}px;
                background: transparent;
            }}
            QPushButton#viewToggle, QPushButton#viewToggleActive {{
                background-color: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT_DIM};
                font-size: {Fonts.SIZE_BUTTON}px;
            }}
            QPushButton#viewToggle:hover, QPushButton#viewToggleActive:hover {{
                color: {Colors.ACCENT};
                border-color: {Colors.ACCENT};
            }}
            QPushButton#viewToggleActive {{
                color: {Colors.ACCENT};
                border-color: {Colors.ACCENT};
            }}
            QLabel.sectionHeader {{
                color: {Colors.TEXT};
                font-family: {Fonts.DISPLAY};
                font-weight: bold;
                font-size: {Fonts.SIZE_BODY_L}px;
                letter-spacing: {Fonts.TRACK_LABEL}px;
                background: transparent;
            }}
            QLabel.sectionSub {{
                color: {Colors.TEXT_DIM};
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_BODY}px;
                background: transparent;
            }}
        '''

    # Filter / sort handlers

    def _on_filter_changed(self, idx: int):
        mapping = ['all', 'clips', 'screenshots', 'imported']
        self._filter = mapping[idx] if idx < len(mapping) else 'all'
        if self._records:
            self._apply_current_index()
        else:
            # Compatibility path for a filter changed before the first scan
            # has published an index. It is still cancellable and bounded.
            self._fade_out_then_reload()

    def _on_sort_changed(self, idx: int):
        self._sort = ['newest', 'oldest'][idx]
        if self._records:
            self._apply_current_index()
        else:
            self._fade_out_then_reload()

    def _filtered_records(self) -> list[LibraryRecord]:
        records = list(self._records.values())
        if self._filter == 'clips':
            records = [record for record in records if record.kind == 'video']
        elif self._filter == 'screenshots':
            records = [record for record in records if record.kind == 'image']
        elif self._filter == 'imported':
            records = [record for record in records if record.imported]
        if self._sort == 'oldest':
            records.sort(key=lambda record: record.mtime_ns)
        elif self._sort == 'longest':
            records.sort(key=lambda record: record.size, reverse=True)
        else:
            records.sort(key=lambda record: record.mtime_ns, reverse=True)
        return records

    def _records_from_paths(self, paths: set[str]) -> dict[str, LibraryRecord]:
        result: dict[str, LibraryRecord] = {}
        for raw in paths:
            try:
                info = os.stat(raw)
            except OSError:
                continue
            identity = canonical_media_path(raw)
            suffix = Path(raw).suffix.casefold()
            kind = 'video' if suffix in _VIDEO_EXTS else 'image'
            result[identity] = LibraryRecord(
                os.path.abspath(os.path.normpath(raw)), identity, (),
                raw in self._imported_files, kind,
                int(info.st_size), int(info.st_mtime_ns))
        return result

    def _apply_current_index(self) -> None:
        records = self._filtered_records()
        self._deferred_thumbnail_paths.clear()
        self._thumbnail_cancel_event.set()
        self._thumbnail_generation += 1
        self._thumbnail_cancel_event = threading.Event()
        self._clear_sections()
        self.thumbnails.clear()
        self._thumb_widgets.clear()
        self._virtual_initialized = False
        self._virtual_ready.clear()
        if not records:
            self._show_empty(True)
            self._all_count_label.setText(self._filter_heading())
            return
        self._show_empty(False)
        self._all_count_label.setText(
            f'{self._filter_heading()}  ({len(records)})')
        groups: dict[str, list[str]] = {}
        order: list[str] = []
        for record in records:
            key = _section_label_for(record.mtime)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(record.path)
        global_idx = 0
        for section_key in order:
            files = groups[section_key]
            self._add_section(section_key, files, global_idx)
            global_idx += len(files)
        QTimer.singleShot(0, self._update_virtualized_cards)

    def _fade_out_then_reload(self):
        """Scan a category in the background, keeping existing cards visible.

        Avoid QGraphicsOpacityEffect: it can leave the host transparent on Windows.
        """
        if self._transition_anim is not None:
            self._transition_anim.stop()
            self._transition_anim = None

        self._in_transition = True
        # Sequence guard: if you spam the filter dropdown, multiple background
        # workers race. Each gets a seq number and only the latest one's result
        # is allowed to touch the UI — older ones finish and get thrown away.
        # Otherwise a slow earlier scan could overwrite a newer filter's results.
        self._transition_seq = getattr(self, '_transition_seq', 0) + 1
        seq = self._transition_seq
        if self._active_scan_worker is not None:
            self._active_scan_worker.cancel()
        for previous in list(self._transition_workers.values()):
            previous.cancel()
        # Remove queued obsolete transitions; a running worker observes its
        # event and exits at the next directory boundary.
        self._scan_thread_pool.clear()
        self._transition_workers.clear()

        def _launch_worker():
            import_dirs = self._sm.get('imported_clip_folders', []) if self._sm else []
            cancel_event = threading.Event()
            worker = _FileCollectWorker(
                self.clips_dir, import_dirs, self._filter, self._sort,
                cancel_event=cancel_event)
            self._transition_workers[seq] = worker
            self._diagnostic_scan_started[seq] = time.monotonic()
            emit_event(
                'library', 'scan_started', state='SCANNING',
                scan_kind='filter_transition', filter=self._filter,
                active_workers=len(self._transition_workers))

            def _on_done(raw, pairs, imported, subdirs):
                self._transition_workers.pop(seq, None)
                started = self._diagnostic_scan_started.pop(seq, None)
                emit_event(
                    'library', 'scan_completed', state='COMPLETED',
                    scan_kind='filter_transition',
                    candidate_video_count=sum(
                        1 for path in raw if is_completed_video_path(path)),
                    candidate_media_count=len(raw),
                    imported_media_count=len(imported),
                    elapsed_ms=(round((time.monotonic() - started) * 1000)
                                if started is not None else None),
                    active_workers=len(self._transition_workers),
                    process_memory_bytes=process_memory_bytes())
                if (self._transition_seq == seq
                        and (worker.result is None
                             or not worker.result.stats.cancelled)):
                    self._on_files_collected_for_transition(raw, pairs, imported, subdirs)

            worker.signals.finished.connect(_on_done)
            self._scan_thread_pool.start(worker)

        # Do not use a graphics effect here. Besides being unnecessary for the
        # scan, it can leave Qt's backing store transparent on some Windows
        # GPU/driver combinations.
        self._sections_host.setGraphicsEffect(None)
        _launch_worker()

    def _on_files_collected_for_transition(self, raw_files, sorted_pairs, imported_files, subdirs):
        """Runs on the main thread once the background worker finishes."""
        self._sync_watched_directories(subdirs)

        self._imported_files = imported_files
        self._records = self._records_from_paths(set(raw_files))
        self._known_files = set(raw_files)
        self._apply_current_index()

        self._sections_host.setGraphicsEffect(None)
        self._in_transition = False
        self._transition_anim = None
        pending = self._pending_saved_clips
        self._pending_saved_clips = {}
        for path, ready in pending.items():
            self.upsert_saved_clip(path, ready=ready)

    def _filter_heading(self) -> str:
        if self._filter == 'clips':
            return 'CLIPS'
        if self._filter == 'screenshots':
            return 'SCREENSHOTS'
        if self._filter == 'imported':
            return 'IMPORTED CLIPS'
        return 'ALL CLIPS'

    # Filesystem walking

    def _collect_media_files(self) -> set:
        """Compatibility helper; production refreshes use ``_FileCollectWorker``."""
        import_dirs = self._sm.get('imported_clip_folders', []) if self._sm else []
        result = scan_library(self.clips_dir, import_dirs, filter_='all', sort_=self._sort)
        self._imported_files = set(result.imported_paths)
        for path in result.watched_directories:
            self._watch_subdir(path)
        self._records = {record.identity: record for record in result.all_records}
        return {record.path for record in result.all_records}

    def _watch_subdir(self, path: str):
        if (not self._shutdown_started and os.path.isdir(path)
                and path not in self._watcher.directories()):
            self._watcher.addPath(path)

    def _sync_watched_directories(self, paths) -> None:
        """Keep QFileSystemWatcher bounded to the latest scanner result."""
        wanted = {os.path.abspath(os.path.normpath(path)) for path in paths}
        current = set(self._watcher.directories())
        for path in current - wanted:
            self._watcher.removePath(path)
        for path in wanted:
            self._watch_subdir(path)

    # Build the date-grouped layout

    def _load_clips(self):
        if self._background_paused:
            self._background_refresh_pending = True
            return
        if self._in_transition or self._shutdown_started:
            return
        if not os.path.isdir(self.clips_dir):
            os.makedirs(self.clips_dir, exist_ok=True)
            if self.clips_dir not in self._watcher.directories():
                self._watcher.addPath(self.clips_dir)
            self._show_empty(True)
            return
        self._begin_scan('foreground_refresh')

    def _begin_scan(self, scan_kind: str) -> None:
        """Start one cancellable scan; at most one follow-up is retained."""
        import_dirs = self._sm.get('imported_clip_folders', []) if self._sm else []
        self._scan_generation += 1
        generation = self._scan_generation
        if self._active_scan_worker is not None:
            self._active_scan_worker.cancel()
            self._pending_scan_request = (scan_kind, self._filter)
            return
        cancel_event = threading.Event()
        worker = _FileCollectWorker(
            self.clips_dir, import_dirs, 'all', self._sort,
            cancel_event=cancel_event)
        worker.generation = generation
        worker.scan_kind = scan_kind
        self._active_scan_worker = worker
        started = time.monotonic()
        self._diagnostic_scan_started[generation] = started
        emit_event(
            'library', 'scan_started', state='SCANNING', scan_kind=scan_kind,
            generation=generation, root_count=1 + len(import_dirs),
            active_workers=self._scan_thread_pool.activeThreadCount())

        def _on_done(raw, pairs, imported, subdirs):
            if self._active_scan_worker is worker:
                self._active_scan_worker = None
            elapsed = round((time.monotonic() - started) * 1000)
            result = worker.result
            if result is None or result.stats.cancelled:
                emit_event('library', 'scan_cancelled', state='CANCELLED',
                           generation=generation, elapsed_ms=elapsed)
            elif generation == self._scan_generation and not self._shutdown_started:
                self._sync_watched_directories(subdirs)
                next_records = {record.identity: record
                                for record in result.all_records}
                for identity, upserted in list(self._scan_upserts.items()):
                    try:
                        if os.path.isfile(upserted.path):
                            next_records.setdefault(identity, upserted)
                    except OSError:
                        # A deleted path must not be resurrected by a late
                        # CLIP_SAVED notification.
                        continue
                self._scan_upserts.clear()
                self._imported_files = set(result.imported_paths)
                self._known_files = {record.path for record in result.all_records}
                summary = result.stats.as_dict() | {
                    'scan_kind': scan_kind,
                    'generation': generation,
                    'candidate_media_count': len(result.all_records),
                    'candidate_video_count': sum(
                        record.kind == 'video' for record in result.all_records),
                    'imported_media_count': len(self._imported_files),
                    'elapsed_ms': elapsed,
                    'thumbnail_widget_cache_entries': len(self._thumb_widgets),
                    'metadata_cache_entries': len(self._records),
                    'active_thumbnail_workers': self._thread_pool.activeThreadCount(),
                    'active_scan_workers': self._scan_thread_pool.activeThreadCount(),
                    'process_memory_bytes': process_memory_bytes(),
                }
                emit_event('library', 'scan_completed', state='COMPLETED', **summary)
                session = get_diagnostic_session()
                if session is not None:
                    session.update_summary('library', summary)
                if next_records != self._records:
                    self._records = next_records
                    self._apply_current_index()
                else:
                    self._requeue_visible_thumbnail_jobs()
            self._diagnostic_scan_started.pop(generation, None)
            pending = self._pending_scan_request
            self._pending_scan_request = None
            if pending and not self._shutdown_started:
                QTimer.singleShot(0, lambda: self._begin_scan(pending[0]))

        worker.signals.finished.connect(_on_done)
        worker.signals.batch.connect(self._on_scan_batch)
        self._scan_thread_pool.start(worker)
        # Give a tiny initial scan a chance to publish before callers inspect
        # a freshly constructed ClipGrid (and before the first paint).  The
        # wait is hard-bounded; large libraries continue asynchronously and
        # never occupy the main thread for the duration of their scan.
        if (scan_kind == 'foreground_refresh' and not self._records
                and not self.isVisible()):
            self._scan_thread_pool.waitForDone(100)
            QApplication.processEvents()

    def _on_scan_batch(self, generation: int, records) -> None:
        """Publish the first discovery batch before the scan finishes.

        Keep later batches on the worker to avoid repeated Qt model rebuilds;
        publish the sorted result once the generation completes.
        """
        if (self._shutdown_started or generation != self._scan_generation
                or self._active_scan_worker is None
                or getattr(self._active_scan_worker, 'generation', None)
                != generation or self._records):
            if generation != self._scan_generation:
                emit_event('library', 'stale_scan_batch_discarded',
                           state='DISCARDED', generation=generation)
            return
        partial = {record.identity: record for record in records}
        if not partial:
            return
        self._records = partial
        self._known_files = {record.path for record in partial.values()}
        self._imported_files = {
            record.path for record in partial.values() if record.imported}
        emit_event('library', 'scan_batch_published', state='PARTIAL',
                   generation=generation, record_count=len(partial))
        self._apply_current_index()

    def _finish_thumbnail_job(
            self, file_path: str, generation: int, fingerprint: str) -> bool:
        """Release one job only when the callback still owns its token."""
        token = (generation, fingerprint)
        if self._thumbnail_job_owners.get(file_path) != token:
            return False
        self._thumbnail_job_owners.pop(file_path, None)
        self._thumbnail_workers.pop(file_path, None)
        self._thumbnail_jobs_inflight.discard(file_path)
        if self._thumbnail_job_fingerprints.get(file_path) == fingerprint:
            self._thumbnail_job_fingerprints.pop(file_path, None)
        return True

    def _requeue_visible_thumbnail_jobs(self) -> None:
        """Re-enrich visible cards without rebuilding the library index."""
        if self._background_paused or self._shutdown_started:
            return
        for file_path, card in tuple(self._thumb_widgets.items()):
            if card.ready and not card._thumbnail_ready:
                self._start_thumbnail_worker(file_path)

    def _start_thumbnail_worker(self, file_path: str) -> None:
        if self._background_paused or self._shutdown_started:
            return
        card = self._thumb_widgets.get(file_path)
        if (card is None or not card.ready
                or card._thumbnail_ready
                or file_path in self._thumbnail_jobs_inflight):
            return
        if len(self._thumbnail_jobs_inflight) >= _MAX_THUMBNAIL_QUEUE:
            self._deferred_thumbnail_paths.add(file_path)
            return
        getattr(self, '_deferred_thumbnail_paths', set()).discard(file_path)
        try:
            fingerprint = self._records.get(
                canonical_media_path(file_path)).fingerprint
        except AttributeError:
            fingerprint = ''
        generation = self._thumbnail_generation
        if card.is_video:
            worker = _ThumbnailWorker(file_path, self._thumbnail_cancel_event)
            worker.signals.finished.connect(
                lambda path, cache, duration, expected=fingerprint, generation=generation:
                    self._on_thumb_ready(
                        path, cache, duration, expected, generation))
        else:
            worker = _ImageThumbnailWorker(
                file_path, self._thumbnail_cancel_event)
            worker.signals.finished.connect(
                lambda path, image, expected=fingerprint, generation=generation:
                    self._on_image_thumb_ready(
                        path, image, expected, generation))
        worker.signals.diagnostic_finished.connect(
            lambda elapsed_ms, metadata_ms, cache_hit,
                   path=file_path, generation=generation, expected=fingerprint:
                self._on_thumb_diagnostic(
                    elapsed_ms, metadata_ms, cache_hit,
                    path, generation, expected))
        self._thumbnail_jobs_inflight.add(file_path)
        self._thumbnail_workers[file_path] = worker
        self._thumbnail_job_owners[file_path] = (generation, fingerprint)
        self._thumbnail_queue_peak = max(
            self._thumbnail_queue_peak, len(self._thumbnail_jobs_inflight))
        self._thumbnail_job_fingerprints[file_path] = fingerprint
        self._thumbnail_jobs_submitted += 1
        self._thread_pool.start(worker)

    def _create_thumbnail(
            self, file_path: str, card_width: int,
            *, ready_override: bool | None = None) -> ClipThumbnail:
        is_video = is_completed_video_path(file_path)
        uploaded = bool(
            self._upload_checker and self._upload_checker(file_path))
        upload_info = (
            self._upload_info_checker(file_path)
            if uploaded and self._upload_info_checker else None)
        ready = bool(
            ready_override
            if ready_override is not None
            else (self._readiness_checker(file_path)
                  if self._readiness_checker else True))
        thumb = ClipThumbnail(
            file_path, is_video=is_video,
            imported=file_path in self._imported_files,
            upload_enabled=bool(
                self._upload_enabled and self._upload_enabled()),
            uploaded=uploaded,
            upload_info=upload_info,
            ready=ready,
            card_width=card_width,
            clips_root=self.clips_dir,
            defer_image_load=not is_video,
        )
        thumb.opened.connect(self.clip_opened.emit)
        thumb.clicked.connect(
            self.clip_clicked.emit
            if is_video else self.screenshot_clicked.emit)
        thumb.deleted.connect(self._on_clip_deleted)
        thumb.upload_requested.connect(self.clip_upload_requested.emit)
        self.thumbnails.append(thumb)
        self._thumb_widgets[file_path] = thumb
        return thumb

    def _add_section(
            self, header_text: str, files: list[str], starting_idx: int,
            *, insert_at: int | None = None,
            ready_overrides: dict[str, bool] | None = None):
        """Build a date header with a small grid or a virtual canvas.

        Above the card budget, store geometry and position only visible cards
        through _layout_virtual_section instead of allocating every layout item.
        """
        section = QFrame()
        section.setStyleSheet('background: transparent;')
        sl = QVBoxLayout(section)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(8)

        # Header row: date + dropdown caret (visual)
        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(0, 0, 0, 0)
        hdr_row.setSpacing(10)

        date_lbl = QLabel(f'{header_text}  ▾')
        date_lbl.setProperty('class', 'sectionHeader')
        date_lbl.setStyleSheet(
            f'color: {Colors.TEXT};'
            f' font-family: {Fonts.DISPLAY}; font-weight: bold;'
            f' font-size: {Fonts.SIZE_BODY_L}px;'
            f' letter-spacing: {Fonts.TRACK_LABEL}px;'
            f' background: transparent;'
        )
        hdr_row.addWidget(date_lbl)

        # Game/source name comes from the most-recent file's parent folder
        sample_game = (
            _game_name_from_path(files[0], self.clips_dir) if files else '')
        if sample_game:
            sub_lbl = QLabel(f'·  {sample_game}')
            sub_lbl.setStyleSheet(
                f'color: {Colors.TEXT_DIM}; font-family: {Fonts.BODY};'
                f' font-size: {Fonts.SIZE_BODY}px; background: transparent;'
            )
            hdr_row.addWidget(sub_lbl)

        hdr_row.addStretch(1)
        sl.addLayout(hdr_row)

        self._section_files[header_text] = list(files)
        self._section_widgets[header_text] = section
        for fp, value in (ready_overrides or {}).items():
            self._virtual_ready[fp] = bool(value)

        # Do not create a partially materialized grid with one spacer per
        # undisplayed file.  That old placeholder model was still O(total
        # files) in both QObject and QLayoutItem count.
        use_virtual = (
            len(self._records) > _MAX_MATERIALIZED_CARDS
            or len(files) > _MAX_MATERIALIZED_CARDS
            or len(self._thumb_widgets) + len(files)
                > _MAX_MATERIALIZED_CARDS)
        if use_virtual:
            canvas = QWidget(section)
            canvas.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            canvas.setMinimumWidth(1)
            self._virtual_sections[header_text] = canvas
            sl.addWidget(canvas)
            self._set_virtual_canvas_height(header_text)
        else:
            cols = self._current_columns
            widths = self._current_card_widths
            grid = QGridLayout()
            grid.setHorizontalSpacing(self._GRID_SPACING)
            grid.setVerticalSpacing(self._GRID_SPACING)
            grid.setContentsMargins(0, 0, 0, 0)
            for i, fp in enumerate(files):
                ready_override = self._virtual_ready.get(fp)
                row, col = divmod(i, cols)
                thumb = self._create_thumbnail(
                    fp, widths[col], ready_override=ready_override)
                grid.addWidget(
                    thumb, row, col,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                )
                if not self._in_transition:
                    thumb.fade_in(delay_ms=min((starting_idx + i) * 35, 600))
                self._start_thumbnail_worker(fp)
            for col, width in enumerate(widths):
                grid.setColumnMinimumWidth(col, width)
            sl.addLayout(grid)
            self._section_grids[header_text] = grid
        if insert_at is None:
            self._sections_layout.addWidget(section)
        else:
            self._sections_layout.insertWidget(insert_at, section)

    def _scroll_viewport(self):
        parent = self.parentWidget()
        for _ in range(5):
            if parent is None:
                return None
            if (hasattr(parent, 'verticalScrollBar')
                    and callable(parent.verticalScrollBar)):
                return parent.viewport()
            parent = parent.parentWidget()
        return None

    def _set_virtual_canvas_height(self, section_key: str) -> None:
        canvas = self._virtual_sections.get(section_key)
        files = self._section_files.get(section_key, ())
        if canvas is None:
            return
        cols = max(1, self._current_columns)
        rows = (len(files) + cols - 1) // cols
        height = max(0, rows * self._virtual_row_height()
                     - (self._GRID_SPACING if rows else 0))
        canvas.setFixedHeight(height)

    def _virtual_row_height(self) -> int:
        width = max(self._current_card_widths, default=_CARD_W)
        return round(width * 9 / 16) + _CARD_BODY_H + self._GRID_SPACING

    def _release_virtual_card(self, path: str) -> None:
        self._deferred_thumbnail_paths.discard(path)
        card = self._thumb_widgets.pop(path, None)
        self._card_virtual_sections.pop(path, None)
        if card is None:
            return
        try:
            self.thumbnails.remove(card)
        except ValueError:
            pass
        card.setParent(None)
        card.deleteLater()

    def _convert_section_to_virtual(self, section_key: str) -> None:
        """Replace a small grid before it can grow an unbounded placeholder list."""
        grid = self._section_grids.pop(section_key, None)
        section = self._section_widgets.get(section_key)
        if grid is None or section is None:
            return
        section_layout = section.layout()
        if section_layout is None:
            return
        grid_index = next(
            (index for index in range(section_layout.count())
             if section_layout.itemAt(index).layout() is grid), None)
        if grid_index is None:
            return
        section_layout.takeAt(grid_index)
        for path in list(self._section_files.get(section_key, ())):
            card = self._thumb_widgets.pop(path, None)
            if card is None:
                continue
            try:
                self.thumbnails.remove(card)
            except ValueError:
                pass
            card.setParent(None)
            card.deleteLater()
        canvas = QWidget(section)
        canvas.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._virtual_sections[section_key] = canvas
        section_layout.insertWidget(grid_index, canvas)
        self._set_virtual_canvas_height(section_key)
        self._layout_virtual_section(section_key)

    def _layout_virtual_section(self, section_key: str, *, release_only=False) -> None:
        """Position only visible cards on a fixed-height section canvas."""
        canvas = self._virtual_sections.get(section_key)
        files = self._section_files.get(section_key, ())
        if canvas is None:
            return
        self._set_virtual_canvas_height(section_key)
        cols = max(1, self._current_columns)
        widths = self._current_card_widths
        row_height = self._virtual_row_height()
        rows = (len(files) + cols - 1) // cols
        viewport = self._scroll_viewport()
        if viewport is None:
            row_start, row_end = 0, min(rows, _MAX_MATERIALIZED_CARDS // cols + 1)
        else:
            try:
                top = canvas.mapTo(viewport, QPoint(0, 0)).y()
                row_start = max(
                    0, int((-top) // row_height) - _VIRTUAL_OVERSCAN_ROWS)
                row_end = min(
                    rows,
                    int((viewport.height() - top) // row_height)
                    + _VIRTUAL_OVERSCAN_ROWS + 1,
                )
            except (RuntimeError, AttributeError):
                row_start, row_end = 0, min(rows, _MAX_MATERIALIZED_CARDS // cols + 1)
        wanted: list[tuple[str, int, int]] = []
        for row in range(row_start, row_end):
            for col in range(cols):
                index = row * cols + col
                if index >= len(files):
                    break
                wanted.append((files[index], row, col))
        # A very tall viewport must still respect the global live-card bound.
        wanted = wanted[:_MAX_MATERIALIZED_CARDS]
        wanted_paths = {path for path, _, _ in wanted}
        for path, owner in list(self._card_virtual_sections.items()):
            if owner == section_key and path not in wanted_paths:
                # Keep any in-flight job alive.  Its result populates cache and
                # rematerialization will hit it without starting a duplicate.
                self._release_virtual_card(path)
        if release_only:
            return
        for path, row, col in wanted:
            card = self._thumb_widgets.get(path)
            if card is None:
                if len(self._thumb_widgets) >= _MAX_MATERIALIZED_CARDS:
                    break
                card = self._create_thumbnail(
                    path, widths[col],
                    ready_override=self._virtual_ready.get(path))
                self._card_virtual_sections[path] = section_key
                card.fade_in(delay_ms=0)
                self._start_thumbnail_worker(path)
            elif self._card_virtual_sections.get(path) != section_key:
                self._card_virtual_sections[path] = section_key
            card.setParent(canvas)
            card.resize_card(widths[col])
            card.setGeometry(
                col * (widths[col] + self._GRID_SPACING),
                row * row_height,
                widths[col], card.height())
            card.show()

    def _update_virtualized_cards(self, *_args) -> None:
        """Materialize only visible/overscan records and release distant cards."""
        if self._shutdown_started:
            return
        # Free offscreen cards in ALL sections before any section claims the
        # shared budget (especially when scrolling back towards newer clips).
        for section_key in tuple(self._virtual_sections):
            self._layout_virtual_section(section_key, release_only=True)
        for section_key in tuple(self._virtual_sections):
            self._layout_virtual_section(section_key)
        self._virtual_initialized = True

    def upsert_saved_clip(self, file_path: str, *, ready: bool) -> None:
        """Insert or finalize a known saved path without rescanning the library.

        Update only its date section so save work stays independent of library size.
        """
        update_started = time.monotonic()
        file_path = os.path.abspath(os.path.normpath(os.fspath(file_path)))
        if not is_completed_video_path(file_path):
            return

        if self._background_paused or self._in_transition:
            self._pending_saved_clips[file_path] = (
                bool(ready)
                or self._pending_saved_clips.get(file_path, False))
            if self._background_paused:
                self._background_refresh_pending = True
            return

        if self._known_files is None:
            self._known_files = set(self._thumb_widgets)
        self._known_files.add(file_path)

        existing = self._thumb_widgets.get(file_path)
        if existing is not None:
            self._virtual_ready[file_path] = bool(ready)
            existing.set_ready(ready)
            existing_record = self._records.get(canonical_media_path(file_path))
            if self._active_scan_worker is not None and existing_record is not None:
                self._scan_upserts[existing_record.identity] = existing_record
            if ready:
                self._start_thumbnail_worker(file_path)
            emit_event(
                'library', 'saved_clip_upserted', state='UPDATED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round(
                    (time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            return

        # A virtualized record may already be indexed without having a live
        # card. Do not create a second path entry for a CLIP_SAVED race.
        identity = canonical_media_path(file_path)
        if identity in self._records:
            self._virtual_ready[file_path] = bool(ready)
            if self._active_scan_worker is not None:
                self._scan_upserts[identity] = self._records[identity]
            emit_event(
                'library', 'saved_clip_upserted', state='INDEXED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round((time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            self._update_virtualized_cards()
            return

        # A locally saved video is not visible in screenshot/imported-only
        # views.  It is still added to _known_files so the debounced filesystem
        # watcher sees no unexplained change and does not trigger a rebuild.
        if self._filter not in {'all', 'clips'}:
            emit_event(
                'library', 'saved_clip_upserted', state='FILTERED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round(
                    (time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            return

        try:
            section_key = _section_label_for(os.path.getmtime(file_path))
        except OSError:
            section_key = 'OTHER'

        try:
            info = os.stat(file_path)
            self._records[identity] = LibraryRecord(
                file_path, identity, (canonical_media_path(self.clips_dir),),
                False, 'video', int(info.st_size), int(info.st_mtime_ns))
        # CLIP_SAVED can race with a file that is still being finalized.
        except OSError:
            pass

        if self._active_scan_worker is not None:
            upserted = self._records.get(identity)
            if upserted is not None:
                self._scan_upserts[identity] = upserted

        if len(self._records) > _MAX_MATERIALIZED_CARDS:
            for key in tuple(self._section_grids):
                self._convert_section_to_virtual(key)

        grid = self._section_grids.get(section_key)
        virtual_canvas = self._virtual_sections.get(section_key)
        if grid is None and virtual_canvas is not None:
            files = self._section_files.setdefault(section_key, [])
            if file_path not in files:
                files.append(file_path)

            def _virtual_sort_value(path: str) -> int | float:
                try:
                    return (
                        os.path.getsize(path)
                        if self._sort == 'longest'
                        else os.path.getmtime(path))
                except OSError:
                    return 0

            files.sort(
                key=_virtual_sort_value,
                reverse=self._sort != 'oldest')
            self._virtual_ready[file_path] = bool(ready)
            self._set_virtual_canvas_height(section_key)
            self._layout_virtual_section(section_key)
            self._show_empty(False)
            self._all_count_label.setText(
                f'{self._filter_heading()}  ({len(self._filtered_records())})')
            emit_event(
                'library', 'saved_clip_upserted', state='ADDED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round(
                    (time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            return
        if grid is None:
            insert_at = 0 if self._sort != 'oldest' else None
            self._add_section(
                section_key, [file_path], 0, insert_at=insert_at,
                ready_overrides={file_path: bool(ready)})
        else:
            files = self._section_files.setdefault(section_key, [])
            if file_path not in files:
                files.append(file_path)

            def _sort_value(path: str) -> int | float:
                try:
                    return (
                        os.path.getsize(path)
                        if self._sort == 'longest'
                        else os.path.getmtime(path))
                except OSError:
                    # The file may disappear between publication and reflow;
                    # retain the card until the normal watcher removes it.
                    return 0

            files.sort(
                key=_sort_value,
                reverse=self._sort != 'oldest')
            self._virtual_ready[file_path] = bool(ready)
            # Convert before the section can acquire placeholder items beyond
            # the global live-card budget. The virtual canvas has O(visible)
            # children regardless of how many records the section contains.
            if (len(files) > _MAX_MATERIALIZED_CARDS
                    or len(self._thumb_widgets) > _MAX_MATERIALIZED_CARDS):
                self._convert_section_to_virtual(section_key)
                self._set_virtual_canvas_height(section_key)
                self._layout_virtual_section(section_key)
                self._show_empty(False)
                self._all_count_label.setText(
                    f'{self._filter_heading()}  ({len(self._filtered_records())})')
                emit_event(
                    'library', 'saved_clip_upserted', state='ADDED',
                    ready=bool(ready), full_scan=False,
                    elapsed_ms=round(
                        (time.monotonic() - update_started) * 1000),
                    visible_card_count=len(self._thumb_widgets))
                return
            # Reflow this small section only; no spacer is needed because this
            # branch is guaranteed to remain within the live-card bound.
            while grid.count():
                grid.takeAt(0)
            cols = self._current_columns
            widths = self._current_card_widths
            for index, path in enumerate(files):
                row, col = divmod(index, cols)
                card = self._thumb_widgets.get(path)
                if card is None and len(self._thumb_widgets) < _MAX_MATERIALIZED_CARDS:
                    card = self._create_thumbnail(
                        path, widths[col],
                        ready_override=self._virtual_ready.get(path))
                if card is not None and index < _MAX_MATERIALIZED_CARDS:
                    card.resize_card(widths[col])
                    grid.addWidget(
                        card, row, col,
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            for col, width in enumerate(widths):
                grid.setColumnMinimumWidth(col, width)
            if file_path in self._thumb_widgets:
                self._thumb_widgets[file_path].fade_in()
                self._start_thumbnail_worker(file_path)
            QTimer.singleShot(0, self._update_virtualized_cards)

        self._show_empty(False)
        self._all_count_label.setText(
            f'{self._filter_heading()}  ({len(self._filtered_records())})')
        emit_event(
            'library', 'saved_clip_upserted', state='ADDED',
            ready=bool(ready), full_scan=False,
            elapsed_ms=round((time.monotonic() - update_started) * 1000),
            visible_card_count=len(self._thumb_widgets))

    def _relayout_grids(self):
        """Reposition existing cards for the new column count.

        Reusing widgets avoids flicker and restarting thumbnail workers on resize.
        """
        cols = self._current_columns
        widths = self._current_card_widths
        for si in range(self._sections_layout.count()):
            section_widget = self._sections_layout.itemAt(si).widget()
            if section_widget is None:
                continue
            section_layout = section_widget.layout()
            if section_layout is None:
                continue
            for li in range(section_layout.count()):
                item = section_layout.itemAt(li)
                if item is None:
                    continue
                grid = item.layout()
                if not isinstance(grid, QGridLayout):
                    continue
                widgets = []
                while grid.count():
                    child = grid.takeAt(0)
                    w = child.widget()
                    if w:
                        widgets.append(w)
                for i, w in enumerate(widgets):
                    if isinstance(w, ClipThumbnail):
                        w.resize_card(widths[i % cols])
                    grid.addWidget(
                        w, i // cols, i % cols,
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    )
                for col, width in enumerate(widths):
                    grid.setColumnMinimumWidth(col, width)
        for section_key in tuple(self._virtual_sections):
            self._layout_virtual_section(section_key)

    def set_clips_directory(self, path: str) -> None:
        """Switch the primary library root and refresh the visible media."""
        resolved = str(Path(path).expanduser().resolve(strict=False))
        if resolved == self.clips_dir:
            self.force_refresh()
            return

        self._transition_seq = getattr(self, '_transition_seq', 0) + 1
        if self._transition_anim is not None:
            self._transition_anim.stop()
            self._transition_anim = None
        self._in_transition = False
        self._sections_host.setGraphicsEffect(None)

        old_root = Path(self.clips_dir).resolve(strict=False)
        for watched in list(self._watcher.directories()):
            try:
                Path(watched).resolve(strict=False).relative_to(old_root)
            except ValueError:
                continue
            self._watcher.removePath(watched)

        self.clips_dir = resolved
        self._known_files = set()
        self._imported_files = set()
        Path(resolved).mkdir(parents=True, exist_ok=True)
        if resolved not in self._watcher.directories():
            self._watcher.addPath(resolved)
        self._load_clips()

    def force_refresh(self):
        """Clear the file cache and immediately reload — called when import folders change."""
        if self._background_paused:
            self._background_refresh_pending = True
            return
        self._known_files = set()
        self._load_clips()

    def shutdown(self) -> None:
        """Stop library timers/workers within a bounded grace period."""
        if self._shutdown_started:
            return
        self._shutdown_started = True
        for timer_name in ('refresh_timer', '_debounce_timer', '_resize_timer'):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        if self._active_scan_worker is not None:
            self._active_scan_worker.cancel()
        self._pending_scan_request = None
        self._thumbnail_cancel_event.set()
        self._thread_pool.clear()
        self._scan_thread_pool.clear()
        # Third-party decoders are bounded to a short grace period; shutdown
        # must remain responsive if one ignores cancellation.
        self._thread_pool.waitForDone(1000)
        self._scan_thread_pool.waitForDone(1000)
        for file_path, (generation, fingerprint) in tuple(
                self._thumbnail_job_owners.items()):
            self._finish_thumbnail_job(file_path, generation, fingerprint)
        self._active_scan_worker = None
        for path in list(self._watcher.directories()):
            self._watcher.removePath(path)

    def _clear_sections(self):
        self._section_grids.clear()
        self._section_files.clear()
        self._section_widgets.clear()
        self._virtual_sections.clear()
        self._card_virtual_sections.clear()
        while self._sections_layout.count():
            item = self._sections_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _show_empty(self, show: bool):
        if show:
            detail = ''
            if self._filter == 'screenshots':
                self._no_clips_lbl.setText('NO SCREENSHOTS YET')
                hotkeys = self._sm.get('hotkeys', {}) if self._sm else {}
                screenshot_key = hotkeys.get('save_screenshot', 'F12')
                detail = f'PRESS {screenshot_key} TO TAKE A SCREENSHOT'
            elif self._filter == 'clips':
                self._no_clips_lbl.setText('NO CLIPS YET')
            elif self._filter == 'imported':
                self._no_clips_lbl.setText('NO IMPORTED CLIPS')
            else:
                self._no_clips_lbl.setText('NO CLIPS YET')
            self._empty_detail_lbl.setText(detail)
            self._empty_detail_lbl.setVisible(bool(detail))
        self._empty_widget.setVisible(show)
        self._sections_host.setVisible(not show)

    def _on_clip_deleted(self, file_path: str):
        identity = canonical_media_path(file_path)
        self._records.pop(identity, None)
        if isinstance(self._known_files, set):
            self._known_files.discard(file_path)
        self._imported_files.discard(file_path)
        self._virtual_ready.pop(file_path, None)
        for section_key, files in list(self._section_files.items()):
            if file_path not in files:
                continue
            self._section_files[section_key] = [path for path in files
                                                if path != file_path]
        # This rebuild uses the in-memory index only; no filesystem walk is
        # scheduled for an external delete.
        self._apply_current_index()

    def _on_thumb_ready(self, file_path: str, cache_path: str, duration: int,
                        expected_fingerprint: str = '', generation: int | None = None):
        current = self._records.get(canonical_media_path(file_path))
        if (generation is not None and generation != self._thumbnail_generation):
            emit_event('library', 'stale_thumbnail_result_discarded',
                       state='DISCARDED')
            return
        if (expected_fingerprint and current is not None
                and current.fingerprint != expected_fingerprint):
            emit_event('library', 'stale_thumbnail_result_discarded',
                       state='DISCARDED')
            return
        widget = self._thumb_widgets.get(file_path)
        if widget:
            widget.set_video_thumbnail(cache_path, duration)

    def _on_image_thumb_ready(
            self, file_path: str, image: QImage,
            expected_fingerprint: str = '', generation: int | None = None):
        current = self._records.get(canonical_media_path(file_path))
        if (generation is not None and generation != self._thumbnail_generation):
            emit_event('library', 'stale_thumbnail_result_discarded',
                       state='DISCARDED')
            return
        if (expected_fingerprint and current is not None
                and current.fingerprint != expected_fingerprint):
            emit_event('library', 'stale_thumbnail_result_discarded',
                       state='DISCARDED')
            return
        widget = self._thumb_widgets.get(file_path)
        if widget is not None:
            widget.set_image_thumbnail(image)

    def _on_thumb_diagnostic(
            self, elapsed_ms: int, metadata_ms: int, cache_hit: bool,
            file_path: str | None = None, generation: int | None = None,
            fingerprint: str = ''):
        # This is the single terminal cleanup path. Both successful and failed
        # workers emit diagnostic_finished from finally; cancelled workers do
        # too. Pause/rebuild leaves ownership intact until this callback so a
        # replacement can never run concurrently with its predecessor.
        if file_path is not None and generation is not None:
            current = self._records.get(canonical_media_path(file_path))
            obsolete_owner = generation != self._thumbnail_generation
            fingerprint_changed = (
                bool(fingerprint) and current is not None
                and current.fingerprint != fingerprint)
            if self._finish_thumbnail_job(file_path, generation, fingerprint):
                self._thumbnail_jobs_completed += 1
                # Queue pressure must not strand a visible card until another
                # capture, scan or scroll happens. Only drain deferred work;
                # failed current jobs retain the normal scan retry cooldown.
                deferred = getattr(self, '_deferred_thumbnail_paths', set())
                if (deferred and not self._background_paused
                        and not self._shutdown_started):
                    for path in tuple(deferred):
                        if len(self._thumbnail_jobs_inflight) >= _MAX_THUMBNAIL_QUEUE:
                            break
                        deferred.discard(path)
                        self._start_thumbnail_worker(path)
                # A cancelled old generation may have left a visible card
                # non-ready. Once its owner has actually reached the terminal
                # signal, enqueue exactly one replacement for the current
                # foreground generation.
                if obsolete_owner or fingerprint_changed:
                    self._requeue_visible_thumbnail_jobs()
        self._thumbnail_diagnostics_completed += 1
        self._thumbnail_total_elapsed_ms += max(0, int(elapsed_ms))
        self._metadata_total_elapsed_ms += max(0, int(metadata_ms))
        self._thumbnail_cache_hits += int(bool(cache_hit))
        active_processes, peak_processes = media_process_snapshot()
        if (self._thumbnail_diagnostics_completed == self._thumbnail_jobs_submitted
                or self._thumbnail_diagnostics_completed % 25 == 0):
            emit_event(
                'library', 'thumbnail_health_snapshot',
                metadata_jobs=self._thumbnail_jobs_submitted,
                thumbnail_jobs=self._thumbnail_jobs_submitted,
                completed_thumbnail_jobs=self._thumbnail_jobs_completed,
                completed_diagnostic_jobs=self._thumbnail_diagnostics_completed,
                total_thumbnail_elapsed_ms=self._thumbnail_total_elapsed_ms,
                total_metadata_probe_elapsed_ms=self._metadata_total_elapsed_ms,
                thumbnail_cache_hits=self._thumbnail_cache_hits,
                active_workers=self._thread_pool.activeThreadCount(),
                thumbnail_queue_depth=len(self._thumbnail_jobs_inflight),
                thumbnail_queue_peak=self._thumbnail_queue_peak,
                media_process_active=active_processes,
                media_process_peak=peak_processes,
                materialized_card_count=len(self._thumb_widgets),
                cache_entry_count=self._thumbnail_diagnostics_completed,
                process_memory_bytes=process_memory_bytes())
