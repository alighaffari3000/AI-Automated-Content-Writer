"""Illustrating the article.

An article without pictures reads as unfinished, so the writer says where an
image belongs and what it should show, and this module makes them — after the
draft has passed the gate, never before. Generating on every revision round
would mean paying for pictures that get thrown away.

Images are produced by the same provider as the text, with the same key. What
comes back is raw bytes; putting them somewhere the site can serve is the site
client's job, not this one's.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from google import genai
from google.genai import types

from .config import ImageConfig

logger = logging.getLogger(__name__)

# The writer marks a spot in the Markdown like this:
#     [[IMAGE: what the picture shows | the alt text]]
IMAGE_MARKER = re.compile(r"\[\[IMAGE:\s*(?P<prompt>[^|\]]+?)\s*\|\s*(?P<alt>[^\]]+?)\s*\]\]")


@dataclass
class ImageRequest:
    prompt: str
    alt: str


@dataclass
class GeneratedImage:
    request: ImageRequest
    data: bytes
    mime_type: str

    @property
    def extension(self) -> str:
        return {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
            self.mime_type, ".png"
        )


def find_markers(body: str) -> list[ImageRequest]:
    """Every image the writer asked for, in the order they appear."""
    return [
        ImageRequest(prompt=m.group("prompt").strip(), alt=m.group("alt").strip())
        for m in IMAGE_MARKER.finditer(body)
    ]


def replace_markers(body: str, urls: list[str | None]) -> str:
    """Swap each marker for the picture, or remove it if none was made.

    A marker left in the text would be published as literal `[[IMAGE: ...]]`,
    which is worse than an article with one picture fewer.
    """
    remaining = list(urls)

    def swap(match: re.Match[str]) -> str:
        url = remaining.pop(0) if remaining else None
        if not url:
            return ""
        alt = match.group("alt").strip().replace("]", "")
        return f"![{alt}]({url})"

    return re.sub(r"\n{3,}", "\n\n", IMAGE_MARKER.sub(swap, body))


class ImageGenerator:
    def __init__(self, config: ImageConfig, api_key: str | None = None) -> None:
        self.config = config
        self._client = genai.Client(api_key=api_key) if api_key else genai.Client()

    def _full_prompt(self, request: ImageRequest) -> str:
        return (
            f"{request.prompt.strip()}\n\n"
            f"Style: {self.config.style}\n"
            "No text, no words, no letters, no logos, no watermarks anywhere in "
            "the image. Nothing that looks like a stock-photo caption. "
            "Photographically plausible: real equipment, correct proportions, "
            "no invented hardware."
        )

    def generate(self, request: ImageRequest) -> GeneratedImage | None:
        """One image, or None.

        A picture is worth having but never worth losing a finished article
        over, so every failure here is logged and swallowed.
        """
        try:
            response = self._client.models.generate_content(
                model=self.config.model,
                contents=self._full_prompt(request),
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                ),
            )
        except Exception as exc:  # noqa: BLE001 - an article beats an illustration
            logger.warning("Image generation failed for %r: %s", request.alt, exc)
            return None

        for candidate in response.candidates or []:
            for part in candidate.content.parts or []:
                blob = getattr(part, "inline_data", None)
                if blob and blob.data:
                    return GeneratedImage(
                        request=request,
                        data=blob.data,
                        mime_type=blob.mime_type or "image/png",
                    )

        logger.warning("Image model returned no picture for %r", request.alt)
        return None
