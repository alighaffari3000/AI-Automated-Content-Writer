"""Illustrating the article.

An article without pictures reads as unfinished, so the writer says where an
image belongs and what it should show, and this module makes them — after the
draft has passed the gate, never before. Generating on every revision round
would mean paying for pictures that get thrown away.

This is the one stage that may run somewhere other than Gemini, and the reason
is what each provider leaves behind. Imagen writes SynthID into the pixels --
an invisible watermark built to survive cropping and re-encoding -- while
OpenAI-shaped providers attach C2PA metadata that any resize strips. Three
providers therefore means three shapes of request: Google's own client,
OpenAI's images route, and OpenRouter, which returns pictures through chat
completions because it has no images route at all. The differences stop at
this module's edge.

What comes back is raw bytes; putting them somewhere the site can serve is the
site client's job, not this one's.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from dataclasses import dataclass

import httpx
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


# How long to wait for a picture. Generous, because image models are slow, and
# bounded, because the article is finished by the time this runs and must not
# be held hostage to an illustration.
IMAGE_TIMEOUT = 120.0


class ImageGenerator:
    """One picture at a time, from whichever provider is configured.

    A picture is worth having but never worth losing a finished article over,
    so every failure below is logged and swallowed. That is also why an
    unconfigured provider costs the run its illustrations rather than its
    article -- and why `check` reports one before a run rather than after.
    """

    def __init__(self, config: ImageConfig, api_key: str | None = None) -> None:
        self.config = config
        self._key = api_key or config.api_key
        self._client = None
        if config.is_gemini:
            self._client = genai.Client(api_key=self._key) if self._key else genai.Client()

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
        """One image, or None."""
        try:
            if self.config.is_gemini:
                return self._generate_gemini(request)
            if self.config.provider == "openrouter":
                return self._generate_openrouter(request)
            return self._generate_openai(request)
        except Exception as exc:  # noqa: BLE001 - an article beats an illustration
            logger.warning("Image generation failed for %r: %s", request.alt, exc)
            return None

    # ------------------------------------------------------------- backends

    def _generate_gemini(self, request: ImageRequest) -> GeneratedImage | None:
        response = self._client.models.generate_content(
            model=self.config.model,
            contents=self._full_prompt(request),
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )
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

    def _generate_openai(self, request: ImageRequest) -> GeneratedImage | None:
        """OpenAI's images route, which most compatible resellers implement."""
        payload = self._post(
            "/images/generations",
            {
                "model": self.config.model,
                "prompt": self._full_prompt(request),
                "n": 1,
                "response_format": "b64_json",
            },
        )
        for item in payload.get("data") or []:
            image = self._decode(item.get("b64_json"), request)
            if image:
                return image
            # Some of them hand back a link instead of the bytes.
            url = item.get("url")
            if url:
                fetched = httpx.get(url, timeout=IMAGE_TIMEOUT)
                fetched.raise_for_status()
                return GeneratedImage(
                    request=request,
                    data=fetched.content,
                    mime_type=fetched.headers.get("content-type", "image/png"),
                )
        logger.warning("Image model returned no picture for %r", request.alt)
        return None

    def _generate_openrouter(self, request: ImageRequest) -> GeneratedImage | None:
        """OpenRouter has no images route: pictures come back from a chat call."""
        payload = self._post(
            "/chat/completions",
            {
                "model": self.config.model,
                "modalities": ["image", "text"],
                "messages": [{"role": "user", "content": self._full_prompt(request)}],
            },
        )
        for choice in payload.get("choices") or []:
            for image in (choice.get("message") or {}).get("images") or []:
                url = ((image or {}).get("image_url") or {}).get("url", "")
                decoded = self._from_data_uri(url, request)
                if decoded:
                    return decoded
        logger.warning("Image model returned no picture for %r", request.alt)
        return None

    # -------------------------------------------------------------- plumbing

    def _post(self, path: str, body: dict) -> dict:
        endpoint = self.config.endpoint
        if not endpoint:
            raise ValueError(
                f"No API address for IMAGE_PROVIDER={self.config.provider!r}; "
                "set IMAGE_BASE_URL, or turn IMAGES_ENABLED off."
            )
        response = httpx.post(
            f"{endpoint.rstrip('/')}{path}",
            json=body,
            headers={"Authorization": f"Bearer {self._key}"},
            timeout=IMAGE_TIMEOUT,
        )
        response.raise_for_status()
        return response.json()

    def _decode(
        self, encoded: str | None, request: ImageRequest, mime: str = "image/png"
    ) -> GeneratedImage | None:
        if not encoded:
            return None
        try:
            data = base64.b64decode(encoded)
        except (binascii.Error, ValueError):
            return None
        return GeneratedImage(request=request, data=data, mime_type=mime)

    def _from_data_uri(self, uri: str, request: ImageRequest) -> GeneratedImage | None:
        """`data:image/png;base64,...` into bytes, or None if it is not that."""
        if not uri.startswith("data:") or ";base64," not in uri:
            return None
        header, _, encoded = uri.partition(";base64,")
        return self._decode(encoded, request, header[len("data:") :] or "image/png")
