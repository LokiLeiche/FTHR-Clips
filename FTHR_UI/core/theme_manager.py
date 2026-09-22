"""Persist color, typography, icon, and sound themes under ~/.fthr/theme/.

Merge defaults when loading older themes. Regenerate QSS on Apply to avoid
restyling the widget tree on every color-picker change.
"""
from __future__ import annotations

import json
import math
import shutil
import zipfile
from pathlib import Path
from typing import Optional


# Default color tokens — mirrors style.py Colors class at its defaults.
# Keys match the attribute names on Colors exactly.
DEFAULT_COLORS: dict[str, str] = {
    'BG':           '#000000',
    'SURFACE_1':    '#0a0a0a',
    'SURFACE_2':    '#111111',
    'SURFACE_3':    '#1c1c1c',
    'SHELL_BG':     '#000000',
    'SHELL_BG_2':   '#0a0a0a',
    'SHELL_DIVIDER': '#222222',
    'CARD_BG':      '#0a0a0a',
    'CARD_BG_HI':   '#111111',
    'CARD_BORDER':  '#222222',
    'HAIRLINE':     '#111111',
    'BORDER':       '#222222',
    'BORDER_HI':    '#333333',
    'TEXT':         '#ffffff',
    'TEXT_DIM':     '#888888',
    'TEXT_MUTED':   '#555555',
    'TEXT_GHOST':   '#222222',
    'ACCENT':       '#00ffaa',
    'ACCENT_DIM':   '#00aa72',
    'ACCENT_SOFT':  '#0a2218',
    'ERROR':        '#cc0000',
    'ERROR_SOFT':   '#240606',
    'WARNING':      '#E8871A',
    'SUCCESS':      '#00aa00',
    'DELETE':       '#cc0000',
}


def normalize_font_scale(value) -> float:
    try:
        scale = float(value)
    except (TypeError, ValueError):
        # User-edited theme values are untrusted; invalid input uses safe UI scale.
        return 1.0
    return scale if math.isfinite(scale) and scale > 0 else 1.0

# Every semantic color consumed by the application is exposed here and in the
# Customize page. Keeping this inventory beside the persisted defaults makes
# adding a new themed surface an explicit, reviewable change instead of a
# silent hard-coded exception.
CUSTOMIZABLE_COLOR_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ('Surfaces', (
        ('BG', 'Background'),
        ('SURFACE_1', 'Surface 1'),
        ('SURFACE_2', 'Surface 2'),
        ('SURFACE_3', 'Surface 3'),
        ('SHELL_BG', 'Shell BG'),
        ('SHELL_BG_2', 'Shell Secondary'),
        ('SHELL_DIVIDER', 'Shell Line'),
    )),
    ('Accent', (
        ('ACCENT', 'Accent'),
        ('ACCENT_DIM', 'Accent Dim'),
        ('ACCENT_SOFT', 'Accent Soft'),
    )),
    ('Text', (
        ('TEXT', 'Primary'),
        ('TEXT_DIM', 'Secondary'),
        ('TEXT_MUTED', 'Muted'),
        ('TEXT_GHOST', 'Ghost'),
    )),
    ('Borders', (
        ('BORDER', 'Border'),
        ('BORDER_HI', 'Border Light'),
        ('HAIRLINE', 'Hairline'),
    )),
    ('Cards', (
        ('CARD_BG', 'Card BG'),
        ('CARD_BG_HI', 'Card Hover'),
        ('CARD_BORDER', 'Card Border'),
    )),
    ('Status', (
        ('ERROR', 'Error'),
        ('ERROR_SOFT', 'Error Soft'),
        ('WARNING', 'Warning'),
        ('SUCCESS', 'Success'),
        ('DELETE', 'Delete Button'),
    )),
)

# Icons that can be customized (filename -> display label)
CUSTOMIZABLE_ICONS: dict[str, str] = {
    'favicon.ico':          'App Icon',
    'clip.png':             'Clip Tab',
    'sound.png':            'Audio Tab',
    'visuals.png':          'Visuals Tab',
    'settings(general).png': 'General Tab',
    'updates.png':          'Updates Tab',
    'personalize.png':      'Customize Tab',
    'performance.png':      'Performance Tab',
    'home.png':             'Home Button',
    'refresh.png':          'Refresh Button',
    'play.png':             'Play Button',
    'pause.png':            'Pause Button',
    'close.png':            'Close Button',
    'minimize.png':         'Minimize Button',
    'maximize.png':         'Maximize Button',
    'shutdown.png':         'Shutdown Button',
    'dropdown.png':         'Dropdown Arrow',
}

# Sounds that can be customized (key -> display label)
CUSTOMIZABLE_SOUNDS: dict[str, str] = {
    'clip_captured':       'Clip Captured',
    'screenshot_captured': 'Screenshot Captured',
    'error':               'Error',
    'startup':             'Startup',
    'upload_successful':   'Upload Successful',
    'upload_failed':       'Upload Failed',
}

# Supported formats
SUPPORTED_IMAGE_FORMATS = ('.png', '.jpg', '.jpeg', '.bmp', '.ico', '.svg')
SUPPORTED_SOUND_FORMATS = ('.mp3', '.wav', '.ogg', '.flac', '.m4a', '.wma', '.aac')
SUPPORTED_FONT_FORMATS = ('.ttf', '.otf')

# Default icon tint — signature teal, applied to all default (non-imported) icons
DEFAULT_ICON_TINT = '#00ffaa'

# Default capture card colors
DEFAULT_CAPTURE_CARD_COLORS: dict[str, str] = {
    'CAPTURE_CARD_BG':             '#000000',
    'CAPTURE_CARD_ACCENT':         '#ffffff',
    'CAPTURE_CARD_TEXT':            '#ffffff',
    'CAPTURE_CARD_DIVIDER':        '#222222',
    'CAPTURE_CARD_STATS_DIM':      '#666666',
    'CAPTURE_CARD_PROGRESS_TRACK': '#111111',
    'CAPTURE_CARD_PROGRESS_FILL':  '#ffffff',
}

DEFAULT_FONTS: dict[str, str] = {
    'display': 'Oswald',
    'body': 'Oswald',
}


class ThemeManager:
    """Singleton manager for UI theme customization."""

    _instance: Optional['ThemeManager'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._theme_dir = Path.home() / '.fthr' / 'theme'
        self._icons_dir = self._theme_dir / 'icons'
        self._sounds_dir = self._theme_dir / 'sounds'
        self._fonts_dir = self._theme_dir / 'fonts'
        self._config_file = self._theme_dir / 'theme.json'

        self._theme_dir.mkdir(parents=True, exist_ok=True)
        self._icons_dir.mkdir(exist_ok=True)
        self._sounds_dir.mkdir(exist_ok=True)
        self._fonts_dir.mkdir(exist_ok=True)

        self._data = self._load()

    # Persistence ──────��

    def _load(self) -> dict:
        default = {
            'colors': dict(DEFAULT_COLORS),
            'icons': {},   # filename -> custom path (relative to icons_dir)
            'sounds': {},  # key -> custom path (relative to sounds_dir)
            'icon_tints': {'_global': DEFAULT_ICON_TINT},
            'capture_card': dict(DEFAULT_CAPTURE_CARD_COLORS),
            'fonts': dict(DEFAULT_FONTS),
            'font_files': {},  # registered family -> file in fonts_dir
        }
        if not self._config_file.exists():
            return default
        try:
            with open(self._config_file, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            # Merge with defaults for forward-compatibility
            merged = dict(default)
            if 'colors' in loaded:
                merged['colors'] = {**DEFAULT_COLORS, **loaded['colors']}
            if 'icons' in loaded:
                merged['icons'] = loaded['icons']
            if 'sounds' in loaded:
                merged['sounds'] = loaded['sounds']
            if 'icon_tints' in loaded:
                merged['icon_tints'] = loaded['icon_tints']
            if 'capture_card' in loaded:
                merged['capture_card'] = {**DEFAULT_CAPTURE_CARD_COLORS,
                                          **loaded['capture_card']}
            if 'fonts' in loaded:
                merged['fonts'] = {**DEFAULT_FONTS, **loaded['fonts']}
            if 'font_scale' in loaded:
                merged['font_scale'] = loaded['font_scale']
            if 'font_files' in loaded:
                merged['font_files'] = loaded['font_files']

            # System-font menus were retired. Preserve only Oswald and font
            # families that are backed by an imported theme file; old choices
            # such as Arial/DejaVu otherwise migrate cleanly to the standard.
            imported = set(merged['font_files'])
            for role in DEFAULT_FONTS:
                family = str(merged['fonts'].get(role, 'Oswald'))
                if family != 'Oswald' and family not in imported:
                    merged['fonts'][role] = 'Oswald'
            return merged
        except Exception as e:
            print(f'[Theme] Load failed: {e}')
            return default

    def save(self):
        import os
        self._theme_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._config_file.with_suffix('.json.tmp')
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, indent=2)
            os.replace(str(tmp), str(self._config_file))
        except Exception as e:
            print(f'[Theme] Save failed: {e}')
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # Color access

    def get_color(self, token: str) -> str:
        # The '#ff00ff' fallback is deliberate: if you ever see screaming magenta
        # in the UI, it means a token name got typo'd somewhere. Loud on purpose.
        return self._data['colors'].get(token, DEFAULT_COLORS.get(token, '#ff00ff'))

    def set_color(self, token: str, hex_value: str):
        self._data['colors'][token] = hex_value

    def get_all_colors(self) -> dict[str, str]:
        return dict(self._data['colors'])

    def reset_color(self, token: str):
        if token in DEFAULT_COLORS:
            self._data['colors'][token] = DEFAULT_COLORS[token]

    def reset_all_colors(self):
        self._data['colors'] = dict(DEFAULT_COLORS)

    def is_color_default(self, token: str) -> bool:
        return self._data['colors'].get(token) == DEFAULT_COLORS.get(token)

    # Icon access

    def get_custom_icon_path(self, filename: str) -> Optional[Path]:
        rel = self._data['icons'].get(filename)
        if rel:
            full = self._icons_dir / rel
            if full.exists():
                return full
        return None

    def set_custom_icon(self, filename: str, source_path: Path) -> Path:
        """Copy an icon file into the theme directory. Returns the stored path."""
        ext = source_path.suffix.lower()
        dest_name = Path(filename).stem + ext
        dest = self._icons_dir / dest_name
        shutil.copy2(source_path, dest)
        self._data['icons'][filename] = dest_name
        return dest

    def remove_custom_icon(self, filename: str):
        rel = self._data['icons'].pop(filename, None)
        if rel:
            full = self._icons_dir / rel
            if full.exists():
                full.unlink(missing_ok=True)

    # Sound access

    def get_custom_sound_path(self, key: str) -> Optional[Path]:
        rel = self._data['sounds'].get(key)
        if rel:
            full = self._sounds_dir / rel
            if full.exists():
                return full
        return None

    def set_custom_sound(self, key: str, source_path: Path) -> Path:
        """Copy a sound file into the theme directory. Returns the stored path."""
        dest_name = f'{key}{source_path.suffix.lower()}'
        dest = self._sounds_dir / dest_name
        shutil.copy2(source_path, dest)
        self._data['sounds'][key] = dest_name
        return dest

    def remove_custom_sound(self, key: str):
        rel = self._data['sounds'].pop(key, None)
        if rel:
            full = self._sounds_dir / rel
            if full.exists():
                full.unlink(missing_ok=True)

    # Icon tint access

    def get_icon_tint(self, filename: str) -> str:
        tints = self._data.get('icon_tints', {})
        return tints.get(filename, tints.get('_global', DEFAULT_ICON_TINT))

    def set_icon_tint(self, filename: str, hex_color: str):
        self._data.setdefault('icon_tints', {'_global': DEFAULT_ICON_TINT})
        self._data['icon_tints'][filename] = hex_color

    def remove_icon_tint(self, filename: str):
        self._data.get('icon_tints', {}).pop(filename, None)

    def get_global_icon_tint(self) -> str:
        return self._data.get('icon_tints', {}).get('_global', DEFAULT_ICON_TINT)

    def set_global_icon_tint(self, hex_color: str):
        self._data.setdefault('icon_tints', {})
        self._data['icon_tints']['_global'] = hex_color

    def has_icon_tint_override(self, filename: str) -> bool:
        return filename in self._data.get('icon_tints', {}) and filename != '_global'

    def reset_all_icon_tints(self):
        self._data['icon_tints'] = {'_global': DEFAULT_ICON_TINT}

    # Capture card color access

    def get_capture_card_color(self, key: str) -> str:
        return self._data.get('capture_card', {}).get(
            key, DEFAULT_CAPTURE_CARD_COLORS.get(key, '#ffffff'))

    def set_capture_card_color(self, key: str, hex_color: str):
        self._data.setdefault('capture_card', dict(DEFAULT_CAPTURE_CARD_COLORS))
        self._data['capture_card'][key] = hex_color

    def get_all_capture_card_colors(self) -> dict[str, str]:
        return {**DEFAULT_CAPTURE_CARD_COLORS, **self._data.get('capture_card', {})}

    def reset_capture_card_colors(self):
        self._data['capture_card'] = dict(DEFAULT_CAPTURE_CARD_COLORS)

    # Typography access

    def get_font(self, role: str) -> str:
        return str(self._data.get('fonts', {}).get(
            role, DEFAULT_FONTS.get(role, '')))

    def get_fonts(self) -> dict[str, str]:
        return {**DEFAULT_FONTS, **self._data.get('fonts', {})}

    def set_font(self, role: str, family: str) -> None:
        if role not in DEFAULT_FONTS:
            raise ValueError(f'Unknown font role: {role}')
        clean = str(family or '').replace('"', '').strip()
        self._data.setdefault('fonts', dict(DEFAULT_FONTS))
        self._data['fonts'][role] = clean or DEFAULT_FONTS[role]

    def reset_fonts(self) -> None:
        self._data['fonts'] = dict(DEFAULT_FONTS)
        self._data.pop('font_scale', None)

    def get_font_scale(self) -> float:
        """Return the saved scale, or *1.0* when no valid value exists."""
        return normalize_font_scale(self._data.get('font_scale', 1.0))

    def set_font_scale(self, value) -> None:
        self._data['font_scale'] = normalize_font_scale(value)

    def set_custom_font(self, families: list[str], source_path: Path) -> Path:
        """Copy one user font into the theme and register its family names."""
        source_path = Path(source_path)
        if source_path.suffix.lower() not in SUPPORTED_FONT_FORMATS:
            raise ValueError('Only .ttf and .otf font files are supported')
        clean_families = [str(name).strip() for name in families if str(name).strip()]
        if not clean_families:
            raise ValueError('The font does not expose a usable family name')
        dest = self._fonts_dir / source_path.name
        shutil.copy2(source_path, dest)
        files = self._data.setdefault('font_files', {})
        for family in clean_families:
            files[family] = dest.name
        return dest

    def get_custom_font_paths(self) -> dict[str, Path]:
        """Return existing imported family paths, excluding stale entries."""
        result: dict[str, Path] = {}
        for family, rel_name in self._data.get('font_files', {}).items():
            path = self._fonts_dir / Path(str(rel_name)).name
            if path.is_file():
                result[str(family)] = path
        return result

    # Export / Import

    def export_theme(self, dest_zip: Path) -> bool:
        """Bundle the entire theme (colors, fonts, icons, and sounds) into a ZIP."""
        try:
            with zipfile.ZipFile(dest_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
                # Write config
                zf.writestr('theme.json', json.dumps(self._data, indent=2))
                # Write custom icons
                for rel_name in self._data['icons'].values():
                    icon_path = self._icons_dir / rel_name
                    if icon_path.exists():
                        zf.write(icon_path, f'icons/{rel_name}')
                # Write custom sounds
                for rel_name in self._data['sounds'].values():
                    sound_path = self._sounds_dir / rel_name
                    if sound_path.exists():
                        zf.write(sound_path, f'sounds/{rel_name}')
                # Multiple family names can point at one font file. Store each
                # physical file once while preserving the family mapping in
                # theme.json.
                font_names = set(self._data.get('font_files', {}).values())
                for rel_name in font_names:
                    font_path = self._fonts_dir / Path(str(rel_name)).name
                    if font_path.exists():
                        zf.write(font_path, f'fonts/{font_path.name}')
            return True
        except Exception as e:
            print(f'[Theme] Export failed: {e}')
            return False

    def import_theme(self, zip_path: Path) -> bool:
        """Full override — extract ZIP into theme directory, replacing everything.

        Note "replacing everything": we wipe the existing custom icons/sounds
        first. Importing a theme is a clean slate, not a merge — otherwise you'd
        accumulate orphaned files from every theme you ever tried. This is fine.
        """
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                # Gatekeep: no theme.json, no dice. Stops someone importing a
                # random zip of cat photos and wondering why nothing happened.
                names = zf.namelist()
                if 'theme.json' not in names:
                    print('[Theme] Invalid theme ZIP: missing theme.json')
                    return False

                # Validate EVERYTHING before touching the current theme.
                # Wiping first meant a corrupt theme.json destroyed the
                # user's existing icons/sounds and then failed the import.
                config_data = json.loads(zf.read('theme.json'))
                if not isinstance(config_data, dict):
                    print('[Theme] Invalid theme.json: not an object')
                    return False
                for key in (
                        'colors', 'icons', 'sounds', 'icon_tints',
                        'capture_card', 'fonts', 'font_files'):
                    if key in config_data and not isinstance(config_data[key], dict):
                        print(f'[Theme] Invalid theme.json: "{key}" is not an object')
                        return False
                assets = {}   # dest Path -> bytes, read before any deletion
                for name in names:
                    if name.startswith('icons/') and len(name) > 6:
                        assets[self._icons_dir / Path(name).name] = zf.read(name)
                    elif name.startswith('sounds/') and len(name) > 7:
                        assets[self._sounds_dir / Path(name).name] = zf.read(name)
                    elif name.startswith('fonts/') and len(name) > 6:
                        assets[self._fonts_dir / Path(name).name] = zf.read(name)

                # Clear existing custom assets
                for f in self._icons_dir.iterdir():
                    if f.is_file():
                        f.unlink(missing_ok=True)
                for f in self._sounds_dir.iterdir():
                    if f.is_file():
                        f.unlink(missing_ok=True)
                for f in self._fonts_dir.iterdir():
                    if f.is_file():
                        f.unlink(missing_ok=True)

                # Write the new assets
                for dest, data in assets.items():
                    dest.write_bytes(data)

                self._data = {
                    'colors': {**DEFAULT_COLORS, **config_data.get('colors', {})},
                    'icons': config_data.get('icons', {}),
                    'sounds': config_data.get('sounds', {}),
                    'icon_tints': config_data.get('icon_tints',
                                                  {'_global': DEFAULT_ICON_TINT}),
                    'capture_card': {**DEFAULT_CAPTURE_CARD_COLORS,
                                     **config_data.get('capture_card', {})},
                    'fonts': {**DEFAULT_FONTS, **config_data.get('fonts', {})},
                    'font_files': config_data.get('font_files', {}),
                }
                if 'font_scale' in config_data:
                    self._data['font_scale'] = config_data['font_scale']
                imported = set(self._data['font_files'])
                for role in DEFAULT_FONTS:
                    family = str(self._data['fonts'].get(role, 'Oswald'))
                    if family != 'Oswald' and family not in imported:
                        self._data['fonts'][role] = 'Oswald'
                self.save()
            return True
        except Exception as e:
            print(f'[Theme] Import failed: {e}')
            return False

    # Utility

    @property
    def icons_dir(self) -> Path:
        return self._icons_dir

    @property
    def sounds_dir(self) -> Path:
        return self._sounds_dir

    @property
    def fonts_dir(self) -> Path:
        return self._fonts_dir

    def has_any_customization(self) -> bool:
        if (self._data['icons'] or self._data['sounds']
                or self._data.get('font_files')):
            return True
        if self._data['colors'] != DEFAULT_COLORS:
            return True
        tints = self._data.get('icon_tints', {})
        if tints != {'_global': DEFAULT_ICON_TINT}:
            return True
        if self._data.get('capture_card', {}) != DEFAULT_CAPTURE_CARD_COLORS:
            return True
        if self._data.get('fonts', {}) != DEFAULT_FONTS:
            return True
        if self.get_font_scale() != 1.0:
            return True
        return False
