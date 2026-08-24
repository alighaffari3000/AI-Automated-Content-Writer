"""Markdown to HTML.

The writer produces Markdown because it is portable and easy to review. Most
sites store HTML, so the conversion happens here rather than in every site that
adopts this pipeline.
"""

from __future__ import annotations

import html

import markdown

_EXTENSIONS = [
    "extra",       # tables, fenced code, definition lists
    "sane_lists",
    "smarty",
]


def markdown_to_html(text: str) -> str:
    """Render the article body.

    Tags in the source are escaped before conversion rather than passed
    through. The body is machine-written and lands in a live site's database,
    so a tag there is far likelier to be a mistake than an intention — and this
    way it can never be an injection either.
    """
    return markdown.markdown(
        html.escape(text, quote=False), extensions=_EXTENSIONS, output_format="html"
    )
