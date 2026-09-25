"""Viewer-only intensity palettes, independent of reception and notification rules."""

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Mapping

from eew_intensity import normalize_intensity


INTENSITY_CODES = ("0", "1", "2", "3", "4", "5-", "5+", "6-", "6+", "7")


@dataclass(frozen=True)
class IntensityTheme:
    key: str
    label: str
    colors: Mapping[str, str]
    unknown: str = "#707780"
    row_foreground: str = "#e6e6e6"
    badges: bool = True
    colored_rows: bool = False

    def __post_init__(self):
        colors = dict(self.colors)
        if set(colors) != set(INTENSITY_CODES):
            raise ValueError("An intensity theme must define every intensity category exactly once")
        for color in (*colors.values(), self.unknown, self.row_foreground):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ValueError(f"Invalid RGB color: {color}")
        object.__setattr__(self, "colors", MappingProxyType(colors))

    def color(self, value):
        return self.colors.get(normalize_intensity(value), self.unknown)

    def foreground(self, value):
        """Choose black/white badge text by sRGB contrast, including dark low levels."""
        color = self.color(value)
        rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        luminance = sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
        return "#000000" if luminance > 0.179 else "#ffffff"


# Discrete JQuake-style categories, not the continuous instrumental-intensity map.
# Visual references: https://jquake.net/images/live-ja.png and feature-2.png;
# https://jquake.hatenablog.com/entry/2020/12/07/200114 (high-intensity examples).
# These are a terminal adaptation of reference imagery, not a published official RGB standard.
JQUAKE = IntensityTheme("jquake", "JQuake", {
    "0": "#3c3c3c", "1": "#6e7878", "2": "#1e6ef0", "3": "#32b464",
    "4": "#ffe05d", "5-": "#ffaa00", "5+": "#fa7800",
    "6-": "#f00000", "6+": "#a00000", "7": "#800080",
})

# Preserve the original colored-text presentation and intensity 4+ colors.
# Intensity badges reuse the same soft colors; other fields retain colored text.
MUTED = IntensityTheme("muted", "Soft", {
    "0": "#a3a8b0", "1": "#b2b5cc", "2": "#96b7d8", "3": "#9fc4aa",
    "4": "#ffe066", "5-": "#ffaa00", "5+": "#ff7043",
    "6-": "#ff4040", "6+": "#ff66aa", "7": "#c77dff",
}, unknown="#9aa0a6", colored_rows=True)

THEMES = MappingProxyType({theme.key: theme for theme in (MUTED, JQUAKE)})
DEFAULT_THEME = "muted"


def get_theme(key=DEFAULT_THEME):
    """Resolve stable IDs for a future settings menu; reject typos explicitly."""
    try:
        return THEMES[key]
    except KeyError:
        raise ValueError(f"Unknown intensity theme: {key}") from None
