"""The Markdown the writer produces has to survive the trip into a website."""

from __future__ import annotations

from app.render import markdown_to_html


def test_headings_and_emphasis_become_html():
    html = markdown_to_html("## A heading\n\nSome **bold** text.")
    assert "<h2>A heading</h2>" in html
    assert "<strong>bold</strong>" in html


def test_right_to_left_text_passes_through_intact():
    html = markdown_to_html("## عنوان\n\nمتن فارسی.")
    assert "عنوان" in html
    assert "متن فارسی." in html


def test_tags_in_the_source_are_shown_not_executed():
    """Machine-written text ends up in a live database; a stray tag stays text."""
    html = markdown_to_html("Before <script>alert(1)</script> after.")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_lists_and_tables_survive():
    html = markdown_to_html("- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<li>one</li>" in html
    assert "<table>" in html
