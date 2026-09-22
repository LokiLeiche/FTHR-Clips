from __future__ import annotations
from collections import deque

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from ui.style import Colors, Fonts, Sizes

class ErrorBar(QFrame):
    """Persistent error bar at the bottom of the main window.

    Call push() from the Qt main thread only. For background threads, use
    QTimer.singleShot(0, lambda: self.push(...)).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: deque[dict] = deque()
        self._current: dict | None = None
        self._action_buttons: list[QPushButton] = []

        self.setFixedHeight(Sizes.HW_BAR_H)
        self.setVisible(False)

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(Sizes.SPACE_6, 0, Sizes.SPACE_6, 0)
        self._layout.setSpacing(10)

        self._icon = QLabel('⚠')
        self._layout.addWidget(self._icon)

        self._title = QLabel()
        self._layout.addWidget(self._title)

        sep = QLabel('—')
        sep.setStyleSheet(f'background: transparent; color: {Colors.TEXT};')
        self._separator = sep
        self._layout.addWidget(sep)

        self._detail = QLabel()
        self._detail.setStyleSheet(
            f'font-size: {Fonts.SIZE_BODY}px; font-family: {Fonts.BODY};'
            f' background: transparent; color: {Colors.TEXT};'
        )
        self._layout.addWidget(self._detail)

        self._layout.addStretch()

        self._more_lbl = QLabel()
        self._more_lbl.setStyleSheet(
            f'font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' letter-spacing: 1px; background: transparent; color: {Colors.TEXT_DIM};'
        )
        self._more_lbl.setVisible(False)
        self._layout.addWidget(self._more_lbl)

        # Dismiss button — always last; action buttons inserted before it
        self._dismiss_btn = QPushButton('✕')
        self._dismiss_btn.setFixedWidth(28)
        self._dismiss_btn.setStyleSheet(
            'QPushButton { background: transparent; border: none;'
            f' color: {Colors.TEXT_DIM}; font-size: {Fonts.SIZE_BUTTON}px; }}'
            f'QPushButton:hover {{ color: {Colors.TEXT}; }}'
        )
        self._dismiss_btn.clicked.connect(self.dismiss_current)
        self._layout.addWidget(self._dismiss_btn)

    def refresh_theme(self) -> None:
        """Refresh both idle chrome and any currently displayed error."""
        self._separator.setStyleSheet(
            f'background: transparent; color: {Colors.TEXT};')
        self._detail.setStyleSheet(
            f'font-size: {Fonts.SIZE_BODY}px; font-family: {Fonts.BODY};'
            f' background: transparent; color: {Colors.TEXT};')
        self._more_lbl.setStyleSheet(
            f'font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' letter-spacing: 1px; background: transparent; color: {Colors.TEXT_DIM};')
        self._dismiss_btn.setStyleSheet(
            'QPushButton { background: transparent; border: none;'
            f' color: {Colors.TEXT_DIM}; font-size: {Fonts.SIZE_BUTTON}px; }}'
            f'QPushButton:hover {{ color: {Colors.TEXT}; }}')
        if self._current is not None:
            self._show(self._current)

    # Public API

    def push(self, title: str, detail: str,
             level: str = 'error',
             actions: list[tuple[str, callable]] | None = None) -> None:
        """Add an error to the queue. Shows immediately if bar is idle."""
        entry = {
            'title':   title,
            'detail':  detail,
            'level':   level,
            'actions': list(actions or []),
        }
        if self._current is None:
            self._show(entry)
        else:
            self._queue.append(entry)
            self._update_more_label()

    def dismiss_current(self) -> None:
        """Dismiss the current error; show the next one in the queue."""
        self._current = None
        self._clear_action_buttons()
        if self._queue:
            self._show(self._queue.popleft())
        else:
            self.setVisible(False)
        self._update_more_label()

    def clear(self) -> None:
        """Discard all queued errors and hide the bar."""
        self._queue.clear()
        self._current = None
        self._clear_action_buttons()
        self.setVisible(False)

    # Internal helpers (also called from tests)

    def _fire_action(self, callback: callable) -> None:
        """Execute the given callback then dismiss the current error."""
        callback()
        self.dismiss_current()

    def _show(self, entry: dict) -> None:
        self._current = entry
        # Resolve the token at display time so an applied theme also affects
        # errors that arrive after the settings page was left open.
        color = {
            'error': Colors.ERROR,
            'warning': Colors.WARNING,
        }.get(entry['level'], Colors.ERROR)

        self.setStyleSheet(f'''
            QFrame {{
                background-color: {Colors.SURFACE_1};
                border-top: 1px solid {color};
            }}
        ''')
        self._icon.setStyleSheet(
            f'font-size: {Fonts.SIZE_BODY}px; background: transparent; color: {color};'
        )
        self._title.setStyleSheet(
            f'font-size: {Fonts.SIZE_BODY}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: bold; letter-spacing: 1px;'
            f' background: transparent; color: {color};'
        )
        self._title.setText(entry['title'])
        self._detail.setText(entry['detail'])
        self._add_action_buttons(entry['actions'], color)
        self.setVisible(True)

    def _add_action_buttons(self, actions: list[tuple[str, callable]],
                            color: str) -> None:
        self._clear_action_buttons()
        for label, callback in actions[:2]:
            btn = QPushButton(label)
            btn.setStyleSheet(f'''
                QPushButton {{
                    background-color: transparent;
                    border: 1px solid {color};
                    color: {color};
                    font-size: {Fonts.SIZE_LABEL}px;
                    font-family: {Fonts.DISPLAY};
                    letter-spacing: {Fonts.TRACK_LABEL}px;
                    font-weight: bold;
                    padding: 3px 10px;
                }}
                QPushButton:hover {{
                    background-color: {color};
                    color: {Colors.TEXT};
                }}
            ''')
            cb = callback
            btn.clicked.connect(lambda _checked, c=cb: self._fire_action(c))
            # Insert before dismiss button (last widget)
            self._layout.insertWidget(self._layout.count() - 1, btn)
            self._action_buttons.append(btn)

    def _clear_action_buttons(self) -> None:
        for btn in self._action_buttons:
            self._layout.removeWidget(btn)
            btn.deleteLater()
        self._action_buttons.clear()

    def _update_more_label(self) -> None:
        n = len(self._queue)
        if n > 0:
            self._more_lbl.setText(f'+{n} MORE')
            self._more_lbl.setVisible(True)
        else:
            self._more_lbl.setVisible(False)
