"""Shared color, font, and spacing tokens for FTHR widgets.

Defaults use black and neutral grays, a teal accent, Oswald, square corners,
and an 8px spacing grid. Imported themes can override fonts and colors.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QComboBox, QPushButton, QWidget

from core.theme_manager import ThemeManager


# Palette
class Colors:
    # Body canvas — pure black, matching fthrclips.com
    BG          = '#000000'
    SURFACE_1   = '#0a0a0a'   # raised — card body, panels
    SURFACE_2   = '#111111'   # raised more — popups, dialogs
    SURFACE_3   = '#1c1c1c'   # hover state, sub-panels

    # Shell surfaces (top bar + status row + filter row).
    SHELL_BG    = '#000000'
    SHELL_BG_2  = '#0a0a0a'   # status row tint, slightly raised
    SHELL_DIVIDER = '#222222'

    # Card surfaces (clip cards in the grid)
    CARD_BG     = '#0a0a0a'
    CARD_BG_HI  = '#111111'   # hover
    CARD_BORDER = '#222222'

    # Hairlines and borders
    HAIRLINE    = '#111111'
    BORDER      = '#222222'
    BORDER_HI   = '#333333'

    # Text
    TEXT        = '#ffffff'
    TEXT_DIM    = '#888888'   # secondary copy on dark
    TEXT_MUTED  = '#555555'   # tertiary / timestamps
    TEXT_GHOST  = '#222222'   # empty-state mark

    # Brand accent (FTHR teal)
    ACCENT      = '#00ffaa'
    ACCENT_DIM  = '#00aa72'
    ACCENT_SOFT = '#0a2218'   # quiet teal background tint for pills

    # Functional states
    ERROR       = '#cc0000'
    ERROR_SOFT  = '#240606'
    WARNING     = '#E8871A'
    SUCCESS     = '#00aa00'
    DELETE      = '#cc0000'

    # Legacy — kept for files that import it.
    BAR_BG      = '#000000'
    BAR_FG      = '#ffffff'
    BAR_FG_DIM  = '#888888'
    BAR_HAIRLINE = '#222222'


# Type
class Fonts:
    # Display face — Oswald is bundled in fonts/.  The leading family in each
    # stack is themeable; the remaining families are deliberate fallbacks.
    DEFAULT_DISPLAY_FAMILY = 'Oswald'
    DEFAULT_BODY_FAMILY = 'Oswald'
    DISPLAY_FAMILY = DEFAULT_DISPLAY_FAMILY
    BODY_FAMILY = DEFAULT_BODY_FAMILY
    DISPLAY = '"Oswald", "DejaVu Sans Condensed", "Bahnschrift", "Segoe UI", sans-serif'
    BODY    = '"Oswald", "DejaVu Sans", "Segoe UI", sans-serif'

    # Modular scale, ratio ≈1.2, anchored at 11px body
    SIZE_MICRO  = 9
    SIZE_LABEL  = 10
    SIZE_BODY   = 11
    SIZE_BODY_L = 13
    SIZE_H3     = 16
    SIZE_H2     = 22
    SIZE_H1     = 32
    SIZE_BUTTON = 14

    # Tracking presets (px)
    TRACK_LABEL   = 2
    TRACK_DISPLAY = 4
    TRACK_HEADING = 6

    @classmethod
    def configure(cls, display_family: str = '', body_family: str = '') -> None:
        """Apply the font families stored in the current exported theme."""
        cls.DISPLAY_FAMILY = str(display_family or cls.DEFAULT_DISPLAY_FAMILY)
        cls.BODY_FAMILY = str(body_family or cls.DEFAULT_BODY_FAMILY)

        def _quoted(family: str) -> str:
            return '"' + family.replace('"', '') + '"'

        cls.DISPLAY = (
            f'{_quoted(cls.DISPLAY_FAMILY)}, "Oswald", '
            '"DejaVu Sans Condensed", "Bahnschrift", "Segoe UI", sans-serif'
        )
        cls.BODY = (
            f'{_quoted(cls.BODY_FAMILY)}, "DejaVu Sans", "Noto Sans", '
            '"Ubuntu", "Cantarell", "Segoe UI", sans-serif'
        )


def themed_dropdown_pixmap(size: int = 14) -> QPixmap:
    """Return the current themed dropdown arrow at the requested size."""
    theme = ThemeManager()
    custom = theme.get_custom_icon_path('dropdown.png')
    path = (custom or Path(__file__).parent.parent / 'assets' / 'icons'
            / 'dropdown.png')
    pixmap = QPixmap(str(path)) if path.exists() else QPixmap()
    if pixmap.isNull():
        return pixmap

    # The bundled asset is recolored from the shared icon tint. Imported
    # icons retain their authored colors, matching the rest of the icon system.
    if custom is None:
        tinted = QPixmap(pixmap.size())
        tinted.fill(Qt.GlobalColor.transparent)
        painter = QPainter(tinted)
        painter.drawPixmap(0, 0, pixmap)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(tinted.rect(), QColor(
            theme.get_icon_tint('dropdown.png')))
        painter.end()
        pixmap = tinted

    return pixmap.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def paint_dropdown_arrow(
        painter: QPainter,
        rect: QRect,
        expanded: bool = False,
        size: int = 14,
        right_padding: int = 8,
) -> None:
    """Paint the shared dropdown arrow inside *rect*."""
    pixmap = themed_dropdown_pixmap(size)
    if pixmap.isNull():
        return

    x = rect.right() - size - right_padding + 1
    y = rect.top() + (rect.height() - size) // 2
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    if expanded:
        painter.translate(x + size / 2.0, y + size / 2.0)
        painter.rotate(180)
        painter.drawPixmap(QRect(-size // 2, -size // 2, size, size), pixmap)
    else:
        painter.drawPixmap(x, y, pixmap)
    painter.restore()


class ThemedDropdownArrow(QWidget):
    """Small standalone dropdown indicator for custom expandable controls."""

    def __init__(self, size: int = 14, parent=None):
        super().__init__(parent)
        self._size = size
        self._expanded = False
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def setExpanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if self._expanded == expanded:
            return
        self._expanded = expanded
        self.update()

    def refresh_theme(self) -> None:
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt API name
        del event
        painter = QPainter(self)
        paint_dropdown_arrow(
            painter, self.rect(), self._expanded,
            size=self._size, right_padding=0)
        painter.end()


class ThemedDropdownButton(QPushButton):
    """Push button with the shared dropdown arrow and open-state rotation."""

    def __init__(self, text: str = '', parent=None):
        self._dropdown_expanded = False
        self._dropdown_text = ''
        super().__init__(parent)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API name
        self._dropdown_text = str(text)
        super().setText(self._dropdown_text)

    def setExpanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if self._dropdown_expanded == expanded:
            return
        self._dropdown_expanded = expanded
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt API name
        super().paintEvent(event)
        painter = QPainter(self)
        paint_dropdown_arrow(painter, self.rect(), self._dropdown_expanded)
        painter.end()


class WheelSafeComboBox(QComboBox):
    """A combo whose closed value cannot be changed by incidental scrolling.

    Ignoring the event lets the surrounding settings scroll area consume it.
    The popup view still receives wheel events normally while it is open.
    """

    # Specialized dropdowns that rotate the arrow while open handle the same
    # paint step themselves; ordinary settings combos use this shared path.
    _custom_arrow_managed = False

    def paintEvent(self, event):  # noqa: N802 - Qt API name
        super().paintEvent(event)
        if self._custom_arrow_managed:
            return

        painter = QPainter(self)
        paint_dropdown_arrow(painter, self.rect())
        painter.end()

    def wheelEvent(self, event):  # noqa: N802 - Qt API name
        if self.view().isVisible():
            super().wheelEvent(event)
            return
        event.ignore()


def retarget_widget_font_styles(root, old_display: str, old_body: str) -> None:
    """Refresh already-created inline QSS after a theme font change."""
    widgets = [root, *root.findChildren(QWidget)] if root is not None else []
    for widget in widgets:
        style_getter = getattr(widget, 'styleSheet', None)
        style_setter = getattr(widget, 'setStyleSheet', None)
        if not callable(style_getter) or not callable(style_setter):
            continue
        sheet = style_getter()
        if not sheet:
            continue
        updated = sheet.replace(old_display, Fonts.DISPLAY)
        updated = updated.replace(old_body, Fonts.BODY)
        if updated != sheet:
            style_setter(updated)


def set_theme_style(widget: QWidget, factory) -> None:
    """Keep the token-based recipe so an existing control can change themes.

    Replacing old hex values loses token identity when two tokens happen to
    share a color. Re-evaluate the recipe instead, preserving widget state.
    """
    widget._theme_style_factory = factory
    widget.setStyleSheet(factory())


def refresh_theme_styles(root: QWidget) -> None:
    for widget in [root, *root.findChildren(QWidget)]:
        factory = getattr(widget, '_theme_style_factory', None)
        if factory is not None:
            widget.setStyleSheet(factory())


# Geometry
class Sizes:
    # 8-px grid
    SPACE_2  = 4
    SPACE_3  = 8
    SPACE_4  = 12
    SPACE_5  = 16
    SPACE_6  = 20
    SPACE_7  = 24
    SPACE_8  = 32
    SPACE_9  = 48

    # Heights
    TOP_BAR_H   = 52
    HEADER_H    = 60   # logo + community + account row
    STATUS_H    = 48   # capture-status row
    UNIFIED_BAR_H = 56 # unified top bar that merges header + status row
    FILTER_H    = 52   # all-clips / filter / sort row
    STATS_BAR_H = 28
    BUTTON_H    = 32
    INPUT_H     = 32
    HW_BAR_H    = 36

    # Radius — sharp corners everywhere, matching fthrclips.com
    RADIUS      = 0
    RADIUS_SM   = 0
    RADIUS_MD   = 0
    RADIUS_CARD = 0
    RADIUS_PILL = 0

    # Borders
    BORDER_W   = 1
    HAIRLINE_W = 1


# Inline label helpers
def label_display(
    color: str = Colors.TEXT,
    size: int = Fonts.SIZE_H2,
    tracking: int = Fonts.TRACK_DISPLAY,
) -> str:
    """Oswald display label — for page titles and brand marks."""
    return (
        f'color: {color}; font-size: {size}px; font-weight: bold;'
        f' font-family: {Fonts.DISPLAY}; letter-spacing: {tracking}px;'
        f' background: transparent;'
    )


def label_uppercase(
    color: str = Colors.TEXT,
    size: int = Fonts.SIZE_LABEL,
    tracking: int = Fonts.TRACK_LABEL,
) -> str:
    """Oswald uppercase tracking label — for group titles, section headers."""
    return (
        f'color: {color}; font-size: {size}px; font-weight: bold;'
        f' font-family: {Fonts.DISPLAY}; letter-spacing: {tracking}px;'
        f' background: transparent;'
    )


def label_body(color: str = Colors.TEXT, size: int = Fonts.SIZE_BODY) -> str:
    """Segoe UI body text."""
    return (
        f'color: {color}; font-size: {size}px;'
        f' font-family: {Fonts.BODY};'
        f' background: transparent;'
    )


# Capture status text (shown in the top bar)
def _status_text_base() -> str:
    return (
        f'font-size: {Fonts.SIZE_MICRO}px; font-family: {Fonts.DISPLAY};'
        f' letter-spacing: 1px; font-weight: bold;'
        f' background: transparent; border: none; padding: 0 2px;'
    )


def status_active_qss() -> str:
    return _status_text_base() + f' color: {Colors.ACCENT};'

def status_idle_qss() -> str:
    return _status_text_base() + f' color: {Colors.TEXT_MUTED};'

def status_warning_qss() -> str:
    return _status_text_base() + f' color: {Colors.ERROR};'


# Reusable QSS fragments
def combo_qss(background: str | None = None) -> str:
    """Return the shared combo style, with an optional non-default surface."""
    combo_background = background or Colors.SURFACE_2
    return f'''
    QComboBox {{
        combobox-popup: 0;
        background-color: {combo_background};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
        padding: 6px 12px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY}px;
        font-family: {Fonts.BODY};
        min-width: 90px;
        min-height: 22px;
    }}
    QComboBox:hover {{
        border-color: {Colors.ACCENT};
        color: {Colors.TEXT};
    }}
    QComboBox:focus {{
        border-color: {Colors.ACCENT};
        outline: none;
    }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox::down-arrow {{ image: none; width: 0; height: 0; border: none; }}
    QComboBox QAbstractItemView {{
        background-color: {combo_background};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER_HI};
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY}px;
        font-family: {Fonts.BODY};
        selection-background-color: {Colors.SURFACE_3};
        selection-color: {Colors.ACCENT};
        padding: 0px;
        outline: none;
    }}
    QComboBox QAbstractItemView::item {{
        background-color: {combo_background};
        color: {Colors.TEXT};
        min-height: 26px;
        padding-left: 10px;
        border-radius: 0px;
    }}
    QComboBox QAbstractItemView::item:hover {{
        background-color: {Colors.SURFACE_3};
        color: {Colors.TEXT};
    }}
    QComboBox QAbstractItemView::item:selected {{
        background-color: {Colors.SURFACE_3};
        color: {Colors.ACCENT};
    }}
'''


def button_primary_qss() -> str:
    return f'''
    QPushButton {{
        background-color: {Colors.ACCENT};
        border: none;
        border-radius: {Sizes.RADIUS_MD}px;
        color: {Colors.BG};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 10px 18px;
    }}
    QPushButton:hover {{
        background-color: {Colors.TEXT};
    }}
    QPushButton:pressed {{
        background-color: {Colors.ACCENT_DIM};
        color: {Colors.TEXT};
    }}
    QPushButton:disabled {{
        background-color: {Colors.BORDER};
        color: {Colors.TEXT_MUTED};
    }}
'''


def button_outline_qss() -> str:
    return f'''
    QPushButton {{
        background-color: transparent;
        border: {Sizes.BORDER_W}px solid {Colors.BORDER_HI};
        border-radius: {Sizes.RADIUS_MD}px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 9px 16px;
    }}
    QPushButton:hover {{
        border-color: {Colors.ACCENT};
        color: {Colors.ACCENT};
    }}
    QPushButton:pressed {{
        background-color: {Colors.ACCENT};
        color: {Colors.BG};
        border-color: {Colors.ACCENT};
    }}
'''


def button_secondary_qss() -> str:
    return f'''
    QPushButton {{
        background-color: {Colors.SURFACE_3};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 10px 18px;
    }}
    QPushButton:hover {{
        background-color: {Colors.BORDER};
        border-color: {Colors.BORDER_HI};
    }}
    QPushButton:pressed {{
        background-color: {Colors.SURFACE_2};
    }}
'''


def button_pill_qss() -> str:
    return f'''
    QPushButton {{
        background-color: {Colors.ACCENT_SOFT};
        border: none;
        border-radius: {Sizes.RADIUS_PILL}px;
        color: {Colors.ACCENT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 7px 16px;
    }}
    QPushButton:hover {{
        background-color: {Colors.ACCENT};
        color: {Colors.BG};
    }}
'''


def button_pill_ghost_qss() -> str:
    return f'''
    QPushButton {{
        background-color: transparent;
        border: 1px solid {Colors.BORDER_HI};
        border-radius: {Sizes.RADIUS_PILL}px;
        color: {Colors.TEXT_DIM};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 7px 14px;
    }}
    QPushButton:hover {{
        border-color: {Colors.ACCENT};
        color: {Colors.ACCENT};
    }}
'''


def button_ghost_qss() -> str:
    return f'''
    QPushButton {{
        background-color: transparent;
        border: none;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 6px 10px;
    }}
    QPushButton:hover {{
        color: {Colors.ACCENT};
    }}
'''


def slider_qss() -> str:
    return f'''
    QSlider {{
        background: transparent;
    }}
    QSlider::groove:horizontal {{
        background: {Colors.BORDER};
        height: 2px;
        border: none;
    }}
    QSlider::sub-page:horizontal {{ background: {Colors.ACCENT}; }}
    QSlider::add-page:horizontal {{ background: {Colors.BORDER}; }}
    QSlider::handle:horizontal {{
        background: {Colors.ACCENT};
        width: 12px; height: 12px;
        margin: -5px 0;
        border: none;
        border-radius: 0px;
    }}
    QSlider::handle:horizontal:hover {{ background: {Colors.TEXT}; }}
    QSlider::handle:horizontal:pressed {{ background: {Colors.TEXT}; }}
'''


def checkbox_qss() -> str:
    return f'''
    QCheckBox {{
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY_L}px;
        font-family: {Fonts.BODY};
        spacing: 10px;
        background: transparent;
    }}
    QCheckBox::indicator {{
        width: 14px; height: 14px;
        border: {Sizes.BORDER_W}px solid {Colors.TEXT};
        background-color: {Colors.BG};
        border-radius: 0px;
    }}
    QCheckBox::indicator:hover {{
        border-color: {Colors.ACCENT};
    }}
    QCheckBox::indicator:checked {{
        background-color: {Colors.ACCENT};
        border-color: {Colors.ACCENT};
    }}
'''


def groupbox_qss() -> str:
    return f'''
    QGroupBox {{
        background-color: {Colors.SURFACE_1};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
        margin-top: 16px;
        padding: 22px 18px 16px 18px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-weight: bold;
        font-family: {Fonts.DISPLAY};
        letter-spacing: {Fonts.TRACK_LABEL}px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 14px;
        padding: 0 8px;
        color: {Colors.ACCENT};
        background-color: {Colors.BG};
    }}
'''


def scrollbar_qss() -> str:
    return f'''
    QScrollArea {{ border: none; background-color: {Colors.BG}; }}
    QScrollBar:vertical {{
        background-color: {Colors.BG};
        width: 8px;
        margin: 4px 0;
        border: none;
    }}
    QScrollBar::handle:vertical {{
        background-color: {Colors.BORDER_HI};
        min-height: 32px;
        border-radius: 0px;
    }}
    QScrollBar::handle:vertical:hover {{ background-color: {Colors.ACCENT}; }}
    QScrollBar::add-line:vertical,
    QScrollBar::sub-line:vertical {{ height: 0px; background: none; border: none; }}
    QScrollBar::add-page:vertical,
    QScrollBar::sub-page:vertical {{ background: none; }}
'''


def card_qss() -> str:
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
'''


def details_panel_qss() -> str:
    return f'''
    QFrame#detailsPanel {{
        background-color: {Colors.SURFACE_1};
        border: 1px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
    }}
    QPushButton#panelHeader {{
        background-color: transparent;
        border: none;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_LABEL}px;
        font-family: {Fonts.DISPLAY};
        font-weight: bold;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        padding: 12px 14px;
        text-align: left;
    }}
    QPushButton#panelHeader:hover {{ color: {Colors.ACCENT}; }}
'''


def tooltip_qss() -> str:
    return f'''
    QToolTip {{
        background-color: {Colors.BG};
        border: {Sizes.BORDER_W}px solid {Colors.ACCENT};
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY}px;
        font-family: {Fonts.BODY};
        padding: 4px 8px;
    }}
'''


def lineedit_qss() -> str:
    return f'''
    QLineEdit {{
        background-color: {Colors.SURFACE_2};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER};
        border-radius: {Sizes.RADIUS_MD}px;
        padding: 6px 10px;
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY}px;
        font-family: {Fonts.BODY};
        selection-background-color: {Colors.ACCENT};
        selection-color: {Colors.BG};
    }}
    QLineEdit:hover {{
        border-color: {Colors.BORDER_HI};
    }}
    QLineEdit:focus {{
        border-color: {Colors.ACCENT};
        outline: none;
    }}
    QLineEdit:disabled {{
        background-color: {Colors.SURFACE_1};
        color: {Colors.TEXT_MUTED};
        border-color: {Colors.BORDER};
    }}
'''


def radiobutton_qss() -> str:
    return f'''
    QRadioButton {{
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY_L}px;
        font-family: {Fonts.BODY};
        spacing: 10px;
        background: transparent;
    }}
    QRadioButton::indicator {{
        width: 14px; height: 14px;
        border: {Sizes.BORDER_W}px solid {Colors.TEXT};
        background-color: {Colors.BG};
        border-radius: 7px;
    }}
    QRadioButton::indicator:hover {{
        border-color: {Colors.ACCENT};
    }}
    QRadioButton::indicator:checked {{
        background-color: {Colors.ACCENT};
        border-color: {Colors.ACCENT};
    }}
'''


def context_menu_qss() -> str:
    return f'''
    QMenu {{
        background-color: {Colors.SURFACE_1};
        border: {Sizes.BORDER_W}px solid {Colors.BORDER_HI};
        color: {Colors.TEXT};
        font-size: {Fonts.SIZE_BODY}px;
        font-family: {Fonts.BODY};
        padding: 4px 0;
    }}
    QMenu::item {{ padding: 8px 22px; }}
    QMenu::item:selected {{
        background-color: {Colors.SURFACE_3};
        color: {Colors.ACCENT};
    }}
    QMenu::separator {{
        height: 1px;
        background: {Colors.BORDER};
        margin: 4px 0;
    }}
'''
