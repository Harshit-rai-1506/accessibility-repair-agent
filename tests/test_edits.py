"""Reading the model's JSON edits, applying them safely, and excerpting long pages (no browser needed)."""

import json

import pytest

from a11y_agent.edits import (
    Edit,
    EditError,
    FixProposal,
    ProposalError,
    apply_edits,
    describe_edits,
    focus_html,
    locate,
    parse_proposal,
    proposal_format,
)

GOOD = {"explanation": "Added alt text.", "edits": [{"find": '<img src="a.png">', "replace": '<img src="a.png" alt="A">'}]}


def test_parse_plain_and_fenced_json():
    assert parse_proposal(json.dumps(GOOD)).edits[0].replace == '<img src="a.png" alt="A">'
    fenced = "Here you go:\n```json\n" + json.dumps(GOOD) + "\n```"
    assert parse_proposal(fenced).explanation == "Added alt text."


@pytest.mark.parametrize("reply, message", [
    ("", "empty"),
    ("not json at all", "not valid JSON"),
    ('{"explanation": "x"}', "did not match"),
    ('{"explanation": "x", "edits": [], "extra": 1}', "did not match"),
    ('{"explanation": "x", "edits": []}', "no edits"),
])
def test_parse_errors_are_explained_to_the_model(reply, message):
    with pytest.raises(ProposalError, match=message):
        parse_proposal(reply)


def test_schema_is_strict():
    """Groq's strict mode needs every field required and no extra properties."""
    fmt = proposal_format()
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    schema = fmt["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"explanation", "edits"}
    assert "$defs" not in json.dumps(schema) and "$ref" not in json.dumps(schema)   # written out in place
    edit = schema["properties"]["edits"]["items"]
    assert edit["additionalProperties"] is False and set(edit["required"]) == {"find", "replace", "replace_all"}
    assert "default" not in edit["properties"]["replace_all"]


def test_apply_exact_and_sequential():
    html = "<div><img src=a.png><img src=b.png></div>"
    edits = [Edit(find="<img src=a.png>", replace='<img src=a.png alt="A">'),
             Edit(find="<img src=b.png>", replace='<img src=b.png alt="B">')]
    assert apply_edits(html, edits) == '<div><img src=a.png alt="A"><img src=b.png alt="B"></div>'


def test_apply_tolerates_whitespace_differences():
    html = '<a href="/x">\n    <img src="i.png">\n</a>'
    out = apply_edits(html, [Edit(find='<a href="/x"> <img src="i.png"> </a>',
                                  replace='<a href="/x" aria-label="Home"><img src="i.png" alt=""></a>')])
    assert out == '<a href="/x" aria-label="Home"><img src="i.png" alt=""></a>'


def test_apply_is_all_or_nothing():
    html = "<p>one</p><p>two</p>"
    with pytest.raises(EditError) as error:
        apply_edits(html, [Edit(find="<p>one</p>", replace="<p>1</p>"), Edit(find="<p>three</p>", replace="x")])
    assert "Edit 2" in str(error.value) and "not found" in str(error.value)


def test_apply_rejects_ambiguous_and_empty_find():
    with pytest.raises(EditError, match="occurs 2 times"):
        apply_edits("<li>a</li><li>a</li>", [Edit(find="<li>a</li>", replace="<li>b</li>")])
    with pytest.raises(EditError, match="empty"):
        apply_edits("<p>x</p>", [Edit(find="  ", replace="y")])


def test_describe_edits_shortens():
    text = describe_edits([{"find": "a" * 300, "replace": "b"}], limit=20)
    assert len(text) < 60 and "…" in text


def test_locate_uses_tag_then_attribute():
    html = "<body>\n<p>intro</p>\n<a class='nav' href='/very/long/path'><span>x</span></a></body>"
    # axe serialises with double quotes, so the exact text is not in the source; the href value is
    assert locate(html, '<a class="nav" href="/very/long/path"><span>x</span></a>') == html.index("<a class=")
    assert locate(html, "<p>intro</p>") == html.index("<p>intro</p>")
    assert locate(html, '<div id="missing-element">') is None


def test_focus_returns_short_html_unchanged():
    assert focus_html("<p>short</p>", [], 1000) == ("<p>short</p>", False)


def test_focus_keeps_structure_and_failing_elements():
    filler = "".join(f"<p>paragraph number {i} with some filler text</p>\n" for i in range(800))
    html = "<html><head><title>T</title></head><body>\n" + filler + '<img src="target.png">\n' + filler + "</body></html>"
    text, excerpted = focus_html(html, ['<img src="target.png">'], 8000)
    assert excerpted
    assert len(text) < 12000
    assert text.startswith("<html><head><title>T</title>")       # document start
    assert text.rstrip().endswith("</body></html>")             # document end
    assert '<img src="target.png">' in text                      # the failing element, with context
    assert "characters not shown" in text
    # every shown piece is real source text, so "find" strings copied from it will apply
    for piece in text.split("\n<!-- "):
        chunk = piece.split(" -->\n", 1)[-1]
        assert chunk.strip() == "" or chunk.split("\n")[0] in html


def test_proposal_model_round_trip():
    p = FixProposal.model_validate(GOOD)
    assert parse_proposal(p.model_dump_json()) == p


def test_replace_all_changes_every_copy():
    html = '<img src="a.png">, <img src="a.png">'
    with pytest.raises(EditError, match="set replace_all to true"):
        apply_edits(html, [Edit(find='<img src="a.png">', replace='<img src="a.png" alt="Logo">')])
    fixed = apply_edits(html, [Edit(find='<img src="a.png">', replace='<img src="a.png" alt="Logo">', replace_all=True)])
    assert fixed == '<img src="a.png" alt="Logo">, <img src="a.png" alt="Logo">'


def test_start_and_end_anchors_wrap_a_fragment():
    fixed = apply_edits('<div><img src="a.png"></div>', [Edit(find="<<start>>", replace="<main>"),
                                                          Edit(find="<<end>>", replace="</main>")])
    assert fixed == '<main><div><img src="a.png"></div></main>'
    with pytest.raises(EditError, match="<<end>>"):                # the hint when a guessed closing tag is missing
        apply_edits('<html lang="en">', [Edit(find="</body>", replace="<main></main></body>")])


@pytest.mark.parametrize("page, wrapped", [
    # dev items 131, 135, 140-142: the snippet is only an <html> tag
    ('<html lang="en">', '<html lang="en"><main><h1>T</h1></main>'),
    # dev items 121, 122: a whole document
    ("<!doctype html><html><head><title>T</title></head><body><p>x</p></body></html>",
     "<!doctype html><html><head><title>T</title></head><body><main><h1>T</h1><p>x</p></main></body></html>"),
    ("<html><head><title>T</title></head><p>x</p></html>", "<html><head><title>T</title></head><main><h1>T</h1><p>x</p></main></html>"),
])
def test_anchors_insert_inside_the_page_content(page, wrapped):
    """"<<start>>" meant "before <html>" until Phase 6: invalid HTML that axe-core accepted."""
    edits = [Edit(find="<<start>>", replace="<main><h1>T</h1>"), Edit(find="<<end>>", replace="</main>")]
    assert apply_edits(page, edits) == wrapped


@pytest.mark.parametrize("typo", ["<<start>", "<</end>>", "<<end>", "<start>>"])
def test_a_mistyped_anchor_never_reaches_the_page(typo):
    """Accepted by axe-core in the dev runs (items 122, 142): a stray "<<start>" or "<</end>>" in the page."""
    assert apply_edits("<div>x</div>", [Edit(find="<<end>>", replace=f"</section>{typo}")]) == "<div>x</div></section>"
    assert apply_edits("<div>x</div>", [Edit(find="<<start>>", replace=f"{typo}<section>")]) == "<section><div>x</div>"


def test_replace_all_is_optional_when_parsing():
    p = parse_proposal(json.dumps({"explanation": "x", "edits": [{"find": "a", "replace": "b"}]}))
    assert p.edits[0].replace_all is False
    assert "every copy" in describe_edits([{"find": "a", "replace": "b", "replace_all": True}])
