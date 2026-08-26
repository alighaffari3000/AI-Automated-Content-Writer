"""Which service draws the pictures, and how each of them hands them back.

Three shapes of request behind one `generate()`, because the providers do not
agree on what an image API looks like: Google has its own client, OpenAI has
an images route, and OpenRouter has neither and returns pictures inside a chat
completion. What they must agree on is the failure — a picture is never worth
losing a finished article over, so nothing here may raise.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from app.config import ImageConfig
from app.images import ImageGenerator, ImageRequest

PIXEL = b"\x89PNG\r\n\x1a\n" + b"pretend this is a picture"
ENCODED = base64.b64encode(PIXEL).decode()
REQUEST = ImageRequest(prompt="an inverter on a wall", alt="اینورتر")


def config(**kwargs) -> ImageConfig:
    defaults = {"provider": "openai", "api_key": "k", "base_url": "", "model": "gpt-image-1"}
    return ImageConfig(**{**defaults, **kwargs})


def posting(monkeypatch, response):
    """Capture the one request the generator makes, and answer it."""
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, headers=headers)
        return response(url)

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


# ------------------------------------------------------------- OpenAI-shaped


def test_an_openai_provider_is_asked_for_bytes_at_its_images_route(monkeypatch):
    seen = posting(
        monkeypatch,
        lambda url: httpx.Response(
            200, json={"data": [{"b64_json": ENCODED}]}, request=httpx.Request("POST", url)
        ),
    )

    image = ImageGenerator(config()).generate(REQUEST)

    assert image.data == PIXEL
    assert seen["url"] == "https://api.openai.com/v1/images/generations"
    assert seen["body"]["model"] == "gpt-image-1"
    assert seen["body"]["response_format"] == "b64_json"
    assert seen["headers"]["Authorization"] == "Bearer k"


def test_a_reseller_is_reached_at_its_own_address(monkeypatch):
    seen = posting(
        monkeypatch,
        lambda url: httpx.Response(
            200, json={"data": [{"b64_json": ENCODED}]}, request=httpx.Request("POST", url)
        ),
    )

    ImageGenerator(config(provider="avalai")).generate(REQUEST)

    assert seen["url"].startswith("https://api.avalai.ir/v1/")


def test_an_explicit_address_wins_over_the_built_in_one(monkeypatch):
    seen = posting(
        monkeypatch,
        lambda url: httpx.Response(
            200, json={"data": [{"b64_json": ENCODED}]}, request=httpx.Request("POST", url)
        ),
    )

    ImageGenerator(config(provider="openai", base_url="https://gw.test/v1/")).generate(REQUEST)

    assert seen["url"] == "https://gw.test/v1/images/generations"


def test_a_provider_that_returns_a_link_is_followed(monkeypatch):
    """Some of them hand back a URL where the bytes were expected."""
    posting(
        monkeypatch,
        lambda url: httpx.Response(
            200,
            json={"data": [{"url": "https://cdn.test/x.png"}]},
            request=httpx.Request("POST", url),
        ),
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, timeout=None: httpx.Response(
            200,
            content=PIXEL,
            headers={"content-type": "image/webp"},
            request=httpx.Request("GET", url),
        ),
    )

    image = ImageGenerator(config()).generate(REQUEST)

    assert image.data == PIXEL
    assert image.extension == ".webp"


# ----------------------------------------------------------------- OpenRouter


def test_openrouter_is_asked_through_a_chat_call(monkeypatch):
    """It has no images route at all; the picture comes back as a data URI."""
    seen = posting(
        monkeypatch,
        lambda url: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "images": [
                                {"image_url": {"url": f"data:image/png;base64,{ENCODED}"}}
                            ]
                        }
                    }
                ]
            },
            request=httpx.Request("POST", url),
        ),
    )

    image = ImageGenerator(config(provider="openrouter")).generate(REQUEST)

    assert image.data == PIXEL
    assert seen["url"].endswith("/chat/completions")
    assert seen["body"]["modalities"] == ["image", "text"]


# ------------------------------------------------------------- failing safely


@pytest.mark.parametrize(
    "answer",
    [
        lambda url: httpx.Response(429, json={}, request=httpx.Request("POST", url)),
        lambda url: httpx.Response(200, json={"data": []}, request=httpx.Request("POST", url)),
        lambda url: httpx.Response(
            200, json={"data": [{"b64_json": "not base64 at all!!"}]},
            request=httpx.Request("POST", url),
        ),
    ],
)
def test_nothing_here_raises(monkeypatch, answer):
    """An article without a picture publishes; an exception here would not."""
    posting(monkeypatch, answer)

    assert ImageGenerator(config()).generate(REQUEST) is None


def test_a_provider_with_no_address_fails_quietly(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("should not have got as far as a request")

    monkeypatch.setattr(httpx, "post", refuse)

    assert ImageGenerator(config(provider="some-new-gateway")).generate(REQUEST) is None


# ------------------------------------------------------- what `check` reports


def test_an_unconfigured_provider_is_visible_before_a_run():
    """Every failure above is swallowed, so unless `check` says it, nothing does."""
    assert not config(api_key="").configured
    assert not config(provider="some-new-gateway").configured
    assert config(provider="gemini").configured
    assert config().configured


def test_pictures_turned_off_is_not_a_misconfiguration():
    assert config(enabled=False, api_key="").configured
