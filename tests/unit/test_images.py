"""Image markers: how the writer asks for a picture and what becomes of it.

No image model is called here — this is the parsing and substitution that has
to be right before a single picture is paid for.
"""

from __future__ import annotations

from app.images import find_markers, replace_markers


def test_a_marker_is_read_as_a_prompt_and_alt_text():
    body = "Intro.\n\n[[IMAGE: a rooftop solar array at midday | آرایه خورشیدی روی پشت‌بام]]\n\nMore."
    requests = find_markers(body)
    assert len(requests) == 1
    assert requests[0].prompt == "a rooftop solar array at midday"
    assert requests[0].alt == "آرایه خورشیدی روی پشت‌بام"


def test_markers_are_found_in_the_order_they_appear():
    body = "[[IMAGE: first | one]]\ntext\n[[IMAGE: second | two]]"
    assert [r.prompt for r in find_markers(body)] == ["first", "second"]


def test_a_generated_image_replaces_its_marker():
    body = "Before.\n\n[[IMAGE: an inverter on a wall | اینورتر]]\n\nAfter."
    result = replace_markers(body, ["/uploads/x.png"])
    assert "![اینورتر](/uploads/x.png)" in result
    assert "[[IMAGE:" not in result


def test_a_failed_image_leaves_no_trace_in_the_article():
    """A marker published as literal text is worse than one picture fewer."""
    body = "Before.\n\n[[IMAGE: something | alt]]\n\nAfter."
    result = replace_markers(body, [None])
    assert "[[IMAGE:" not in result
    assert "Before." in result and "After." in result


def test_markers_beyond_what_was_generated_are_dropped():
    body = "[[IMAGE: a | one]]\n\n[[IMAGE: b | two]]\n\n[[IMAGE: c | three]]"
    result = replace_markers(body, ["/uploads/a.png"])
    assert result.count("![") == 1
    assert "[[IMAGE:" not in result


def test_an_article_with_no_markers_is_untouched():
    body = "## Heading\n\nJust text."
    assert replace_markers(body, []) == body


def test_removing_an_image_does_not_leave_a_hole_in_the_markdown():
    body = "One.\n\n[[IMAGE: x | y]]\n\nTwo."
    assert "\n\n\n" not in replace_markers(body, [None])
