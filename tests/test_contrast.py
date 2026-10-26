"""The contrast calculator: WCAG formula, colour parsing, and suggestions that really pass."""

import asyncio

import pytest

from a11y_agent.contrast import ColorError, contrast_ratio, parse_color, suggest_colors


@pytest.mark.parametrize("fg, bg, expected", [
    ("#000000", "#ffffff", 21.0),
    ("#ffffff", "#ffffff", 1.0),
    ("#767676", "#ffffff", 4.54),        # the classic "lightest grey that passes AA on white"
    ("#005ea2", "#f1f1f1", 5.95),        # benchmark item accessguru-004, as axe-core reports it
    ("white", "black", 21.0),
])
def test_known_ratios(fg, bg, expected):
    assert contrast_ratio(fg, bg) == expected


def test_parses_common_formats():
    assert parse_color("#abc") == ((170, 187, 204), 1.0)
    assert parse_color("rgb(10, 20, 30)") == ((10, 20, 30), 1.0)
    assert parse_color("rgba(0 0 0 / 50%)")[1] == 0.5
    assert parse_color("#00000080")[1] == pytest.approx(0.5, abs=0.01)
    with pytest.raises(ColorError, match="Cannot read"):
        parse_color("not-a-colour")


def test_transparent_text_is_blended_with_the_background():
    assert contrast_ratio("rgba(0, 0, 0, 0.5)", "#ffffff") < contrast_ratio("#000000", "#ffffff")


@pytest.mark.parametrize("fg, bg, target", [("#005ea2", "#f1f1f1", 7.0), ("#999999", "#ffffff", 4.5),
                                            ("#ff6600", "#ffffff", 3.0), ("#999999", "#333333", 7.0)])
def test_suggestions_reach_the_target(fg, bg, target):
    report = suggest_colors(fg, bg, target)
    assert not report["passes"] and report["suggestions"]
    for s in report["suggestions"]:
        new_fg, new_bg = (s["color"], bg) if s["change"] == "text color" else (fg, s["color"])
        assert contrast_ratio(new_fg, new_bg) >= target


def test_already_passing_colours_are_left_alone():
    report = suggest_colors("#000000", "#ffffff", 4.5)
    assert report["passes"] and report["suggestions"][0]["color"] in ("#000000", "#ffffff")


def test_bad_target_is_explained():
    with pytest.raises(ColorError, match="between 1 and 21"):
        suggest_colors("#000", "#fff", 40)


@pytest.mark.browser
@pytest.mark.data
def test_axe_core_agrees_with_the_suggested_colour():
    """The real proof: apply the suggestion and scan with axe-core."""
    from a11y_agent.scanner import Scanner

    template = ('<!doctype html><html lang="en"><head><title>T</title></head><body><main><h1>T</h1>'
                '<p style="color: {fg}; background: #f1f1f1">Here is how you know</p></main></body></html>')
    color = suggest_colors("#005ea2", "#f1f1f1", 7.0)["suggestions"][0]["color"]

    async def scan(fg):
        async with Scanner() as scanner:
            return (await scanner.scan_html(template.format(fg=fg))).rule_ids
    assert "color-contrast-enhanced" in asyncio.run(scan("#005ea2"))
    assert "color-contrast-enhanced" not in asyncio.run(scan(color))
