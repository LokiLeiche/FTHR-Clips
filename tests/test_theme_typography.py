from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from core.theme_manager import ThemeManager, normalize_font_scale
from ui.app_style import configure_qt_for_linux_ui
from ui.customize_page import CustomizePage
from ui.style import Colors, Fonts


@pytest.fixture(autouse=True)
def _reset_theme_singleton():
    ThemeManager._instance = None
    yield
    ThemeManager._instance = None
    Fonts.configure('Oswald', 'Oswald')


def test_theme_export_import_round_trips_fonts(monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    theme = ThemeManager()
    source = tmp_path / 'MyFont.ttf'
    source.write_bytes(b'test font payload')
    theme.set_custom_font(['My Font'], source)
    theme.set_font('display', 'My Font')
    theme.set_font('body', 'Oswald')
    theme.save()
    archive = tmp_path / 'theme.zip'

    assert theme.export_theme(archive)
    theme.reset_fonts()
    theme.save()
    assert theme.import_theme(archive)
    assert theme.get_fonts() == {
        'display': 'My Font',
        'body': 'Oswald',
    }
    assert theme.get_custom_font_paths()['My Font'].read_bytes() == b'test font payload'


def test_customize_page_only_exposes_oswald_until_fonts_are_imported(
        qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    ThemeManager._instance = None
    page = CustomizePage()
    qtbot.addWidget(page)

    assert set(page._font_combos) == {'display', 'body'}
    for combo in page._font_combos.values():
        assert [combo.itemData(i) for i in range(combo.count())] == ['Oswald']


def test_customize_font_dropdowns_use_a_raised_surface(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    ThemeManager._instance = None
    page = CustomizePage()
    qtbot.addWidget(page)

    for combo in page._font_combos.values():
        assert f'background-color: {Colors.SURFACE_3}' in combo.styleSheet()


@pytest.mark.parametrize('value', ['bad', '', 0, -1, float('nan'), float('inf')])
def test_font_scale_invalid_values_fall_back_to_one(value):
    assert normalize_font_scale(value) == 1.0


def test_font_scale_loads_and_configures_qt_startup(monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(ThemeManager, '_instance', None)
    theme = ThemeManager()
    theme.set_font_scale(1.35)
    theme.save()

    configure_qt_for_linux_ui()

    assert ThemeManager().get_font_scale() == pytest.approx(1.35)
    assert os.environ['QT_SCALE_FACTOR'] == '1.35'


def test_customize_scale_warning_tracks_pending_restart(
        qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setenv('QT_SCALE_FACTOR', '1')
    monkeypatch.setattr(ThemeManager, '_instance', None)
    page = CustomizePage()
    qtbot.addWidget(page)

    assert not page.font_scale_restart_required()
    assert page._font_scale_restart_hint.isHidden()
    assert page._apply_warning.isHidden()

    page._font_scale_input.setText('1.25')
    page._on_font_scale_changed()

    assert page.font_scale_restart_required()
    assert not page._font_scale_restart_hint.isHidden()
    assert not page._apply_warning.isHidden()


def test_customize_scale_warning_updates_while_typing(
        qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setenv('QT_SCALE_FACTOR', '1')
    monkeypatch.setattr(ThemeManager, '_instance', None)
    page = CustomizePage()
    qtbot.addWidget(page)

    page._font_scale_input.setText('1.25')

    assert not page._font_scale_restart_hint.isHidden()
    assert page._theme.get_font_scale() == pytest.approx(1.0)


def test_apply_theme_persists_new_scale(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setenv('QT_SCALE_FACTOR', '1')
    monkeypatch.setattr(ThemeManager, '_instance', None)
    page = CustomizePage()
    qtbot.addWidget(page)
    page._font_scale_input.setText('1.4')
    page._on_font_scale_changed()
    page._on_apply()

    ThemeManager._instance = None
    assert ThemeManager().get_font_scale() == pytest.approx(1.4)


def test_theme_apply_restart_gate_changes_only_for_scale(
        monkeypatch, tmp_path):
    from main import _SettingsPage

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(ThemeManager, '_instance', None)
    fake = SimpleNamespace(_startup_font_scale=1.0)
    theme = ThemeManager()

    theme.set_color('ACCENT', '#abcdef')
    assert not _SettingsPage._font_scale_changed_since_startup(fake)

    theme.set_font_scale(1.25)
    assert _SettingsPage._font_scale_changed_since_startup(fake)


def test_restart_application_passes_saved_scale_to_new_process(
        monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication
    from main import _SettingsPage

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(ThemeManager, '_instance', None)
    theme = ThemeManager()
    theme.set_font_scale(1.5)
    theme.save()
    monkeypatch.setattr(QApplication, 'quit', lambda: None)
    page = SimpleNamespace()

    _SettingsPage._restart_application(page)

    assert page._restart_environment['QT_SCALE_FACTOR'] == '1.5'
    assert page._restart_command
