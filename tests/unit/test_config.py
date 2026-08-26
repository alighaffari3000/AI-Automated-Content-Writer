"""A copy of `.env.example` is not configuration.

The installer creates `/etc/content-writer/env` from the example file, so the
placeholders are what a server has until someone edits them. They look like
settings — `https://example.com/api/automation` is a valid URL — which is why
they are caught here rather than left for the first live run to discover.
"""

from __future__ import annotations

import pytest

from app import config


@pytest.fixture(autouse=True)
def _forget_placeholders(monkeypatch):
    """Start each test knowing only what the test itself sets.

    The record of unedited settings is module state, so it is saved and put
    back. The API keys have to be cleared as well: `unedited_settings` reads
    them straight from the environment rather than through this module, so
    without this these tests describe whichever keys the machine running them
    happens to have — and pass or fail accordingly.
    """
    seen = set(config._unedited)  # noqa: SLF001 - this module's own bookkeeping
    config._unedited.clear()  # noqa: SLF001
    for key in ("GEMINI_API_KEY", "IMAGE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    yield
    config._unedited.clear()  # noqa: SLF001
    config._unedited.update(seen)  # noqa: SLF001


def test_a_placeholder_url_reads_as_nothing(monkeypatch):
    monkeypatch.setenv("SITE_API_URL", "https://example.com/api/automation")
    monkeypatch.setenv("SITE_API_TOKEN", "generate-a-long-random-string")

    site = config.SiteConfig()

    assert site.api_url == ""
    assert site.api_token == ""
    assert not site.configured
    assert config.unedited_settings() == ("SITE_API_TOKEN", "SITE_API_URL")


def test_a_placeholder_falls_back_to_the_built_in_default(monkeypatch):
    monkeypatch.setenv("CONTENT_DOMAIN", "your industry, described in a phrase")
    monkeypatch.setenv("SITE_NAME", "Example Site")

    assert config.ContentConfig().domain == "the site's industry"
    assert config.SiteConfig().name == "the site"


def test_a_real_value_is_left_alone(monkeypatch):
    monkeypatch.setenv("SITE_API_URL", "https://solar.example.ir/api/automation")
    monkeypatch.setenv("SITE_API_TOKEN", "b7c1f0e2a9")

    site = config.SiteConfig()

    assert site.configured
    assert config.unedited_settings() == ()


def test_the_model_key_is_checked_even_though_nothing_here_reads_it(monkeypatch):
    """ADK reads GEMINI_API_KEY straight from the environment, not from here."""
    monkeypatch.setenv("GEMINI_API_KEY", "your-api-key-here")

    assert "GEMINI_API_KEY" in config.unedited_settings()
