"""Application-wide Qt style setup.

Qt otherwise follows the host desktop style, so Linux can pick GTK/KDE themed
controls while Windows uses its own style. FTHR has a fully custom QSS skin, so
we pin the small unstyled pieces to one deterministic Qt style on every OS.
"""
from __future__ import annotations

import os

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

from core.theme_manager import ThemeManager, normalize_font_scale
from ui.style import Colors, Fonts


def configure_qt_for_linux_ui() -> None:
    """Disable host desktop theming before QApplication snapshots it."""
    os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")
    scale = normalize_font_scale(ThemeManager().get_font_scale())
    os.environ['QT_SCALE_FACTOR'] = f'{scale:g}'
    QApplication.setDesktopSettingsAware(False)


def apply_app_style(app: QApplication) -> None:
    """Make Qt's native fallback widgets look the same on Linux and Windows."""
    if "Fusion" in QStyleFactory.keys():
        app.setStyle(QStyleFactory.create("Fusion"))

    app.setFont(QFont(Fonts.BODY_FAMILY, Fonts.SIZE_BODY))

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(Colors.BG))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(Colors.TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(Colors.SURFACE_1))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(Colors.SURFACE_2))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(Colors.SURFACE_2))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(Colors.TEXT))
    palette.setColor(QPalette.ColorRole.Text, QColor(Colors.TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(Colors.SURFACE_2))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(Colors.TEXT))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(Colors.ERROR))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(Colors.ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(Colors.BG))
    app.setPalette(palette)
