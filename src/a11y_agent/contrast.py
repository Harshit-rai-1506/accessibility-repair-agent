"""Colour contrast arithmetic: the WCAG formula, and colours that pass it.

Contrast is the most common violation in the benchmark (11 of the 40 dev
items, 35 of the 138 test items), and fixing it means finding a colour whose
contrast ratio against the background reaches 4.5:1 (AA) or 7:1 (AAA). That
is arithmetic on relative luminance, which a language model can only
approximate. So it is a tool: the model asks, this module computes.

    contrast_ratio("#767676", "#ffffff")          -> 4.54
    suggest_colors("#005ea2", "#f1f1f1", 7.0)     -> darker blue that reaches 7:1, ...

Formula (WCAG 2.x, "relative luminance" and "contrast ratio"):
    each sRGB channel c in [0, 1]: c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    L = 0.2126 R + 0.7152 G + 0.0722 B
    ratio = (L_lighter + 0.05) / (L_darker + 0.05), from 1:1 to 21:1
"""

from __future__ import annotations

import colorsys
import re

NAMED_COLORS = {
    "black": "#000000", "white": "#ffffff", "gray": "#808080", "grey": "#808080", "silver": "#c0c0c0",
    "red": "#ff0000", "maroon": "#800000", "orange": "#ffa500", "yellow": "#ffff00", "olive": "#808000",
    "lime": "#00ff00", "green": "#008000", "aqua": "#00ffff", "cyan": "#00ffff", "teal": "#008080",
    "blue": "#0000ff", "navy": "#000080", "fuchsia": "#ff00ff", "magenta": "#ff00ff", "purple": "#800080",
}

RGB = tuple[float, float, float]          # channels 0..255


class ColorError(ValueError):
    """A colour string we cannot read. The message is written for the model."""


def parse_color(text: str) -> tuple[RGB, float]:
    """Read "#abc", "#aabbcc", "#aabbccdd", "rgb(1, 2, 3)", "rgba(1, 2, 3, 0.5)" or a basic name.

    Returns ((r, g, b), alpha).
    """
    value = (text or "").strip().lower()
    value = NAMED_COLORS.get(value, value)
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) in (3, 4):
            digits = "".join(ch * 2 for ch in digits)
        if len(digits) in (6, 8) and re.fullmatch(r"[0-9a-f]+", digits):
            r, g, b = (int(digits[i:i + 2], 16) for i in (0, 2, 4))
            alpha = int(digits[6:8], 16) / 255 if len(digits) == 8 else 1.0
            return (r, g, b), alpha
    m = re.fullmatch(r"rgba?\(\s*([^)]*)\)", value)
    if m:
        parts = [p for p in re.split(r"[\s,/]+", m.group(1)) if p]
        if len(parts) in (3, 4):
            def channel(p: str) -> float:
                return float(p[:-1]) * 2.55 if p.endswith("%") else float(p)
            try:
                r, g, b = (min(max(channel(p), 0.0), 255.0) for p in parts[:3])
                alpha = 1.0
                if len(parts) == 4:
                    alpha = float(parts[3][:-1]) / 100 if parts[3].endswith("%") else float(parts[3])
                return (r, g, b), min(max(alpha, 0.0), 1.0)
            except ValueError:
                pass
    raise ColorError(f'Cannot read the colour "{text}". Use a hex value like "#1a2b3c" or "rgb(26, 43, 60)".')


def to_hex(rgb: RGB) -> str:
    return "#" + "".join(f"{round(min(max(c, 0), 255)):02x}" for c in rgb)


def blend(fg: RGB, alpha: float, bg: RGB) -> RGB:
    """The colour you see when ``fg`` with ``alpha`` is drawn over ``bg``."""
    return tuple(alpha * f + (1 - alpha) * b for f, b in zip(fg, bg))


def relative_luminance(rgb: RGB) -> float:
    def linear(c: float) -> float:
        c = c / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(fg: RGB, bg: RGB) -> float:
    lighter, darker = sorted((relative_luminance(fg), relative_luminance(bg)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def contrast_ratio(foreground: str, background: str) -> float:
    """Contrast ratio of text colour over background colour, rounded to 2 decimals."""
    bg, bg_alpha = parse_color(background)
    if bg_alpha < 1:
        bg = blend(bg, bg_alpha, (255, 255, 255))      # an unknown backdrop: assume a white page
    fg, alpha = parse_color(foreground)
    return round(_ratio(blend(fg, alpha, bg), bg), 2)


def _with_lightness(rgb: RGB, lightness: float) -> RGB:
    h, _, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
    return tuple(c * 255 for c in colorsys.hls_to_rgb(h, lightness, s))


def _closest_passing(change: RGB, other: RGB, goal: float, darker: bool) -> str | None:
    """Move ``change`` darker (or lighter), keeping its hue and saturation, just far enough
    for its contrast against ``other`` to reach ``goal``. None if even black/white is not enough.

    Lightness changes luminance monotonically, so a binary search finds the
    smallest change that passes.
    """
    _, start, _ = colorsys.rgb_to_hls(*(c / 255 for c in change))
    end = 0.0 if darker else 1.0
    if _ratio(_with_lightness(change, end), other) < goal:
        return None
    if _ratio(change, other) >= goal:
        return to_hex(change)
    near, far = start, end                    # near fails, far passes
    for _ in range(40):
        mid = (near + far) / 2
        if _ratio(_with_lightness(change, mid), other) >= goal:
            far = mid
        else:
            near = mid
    candidate = to_hex(_with_lightness(change, far))
    # rounding to whole hex values can land just below the goal: step on until it passes
    rgb, _ = parse_color(candidate)
    step = -0.002 if darker else 0.002
    lightness = far
    while _ratio(rgb, other) < goal and 0.0 <= lightness <= 1.0:
        lightness += step
        rgb, _ = parse_color(to_hex(_with_lightness(change, lightness)))
    return to_hex(rgb)


def suggest_colors(foreground: str, background: str, target_ratio: float = 4.5) -> dict:
    """The current ratio and the smallest colour changes that reach ``target_ratio``.

    Suggests a new text colour (darker or lighter, same hue) and a new
    background colour, each the closest one that passes, with its ratio.
    A small margin above the target keeps rounding from failing the check.
    """
    if not 1.0 <= target_ratio <= 21.0:
        raise ColorError("target_ratio must be between 1 and 21 (WCAG uses 3, 4.5 and 7).")
    fg, alpha = parse_color(foreground)
    bg, bg_alpha = parse_color(background)
    if bg_alpha < 1:
        bg = blend(bg, bg_alpha, (255, 255, 255))
    seen_fg = blend(fg, alpha, bg)
    goal = target_ratio + 0.05
    current = round(_ratio(seen_fg, bg), 2)

    suggestions = []
    for darker in (True, False):
        color = _closest_passing(seen_fg, bg, goal, darker)
        if color:
            suggestions.append({"change": "text color", "color": color,
                                "ratio": round(_ratio(parse_color(color)[0], bg), 2)})
        color = _closest_passing(bg, seen_fg, goal, darker)
        if color:
            suggestions.append({"change": "background color", "color": color,
                                "ratio": round(_ratio(seen_fg, parse_color(color)[0]), 2)})

    def distance(s: dict) -> float:
        original = seen_fg if s["change"] == "text color" else bg
        new, _ = parse_color(s["color"])
        return sum((a - b) ** 2 for a, b in zip(original, new))
    suggestions.sort(key=distance)
    return {
        "foreground": to_hex(seen_fg), "background": to_hex(bg), "ratio": current,
        "target_ratio": target_ratio, "passes": current >= target_ratio,
        "suggestions": suggestions[:3],
        "note": ("Suggestions are ordered from the smallest visible change. Use one colour from them; "
                 "keep the other colour as it is."),
    }
