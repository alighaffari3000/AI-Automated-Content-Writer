"""Runtime configuration.

Everything site-specific lives here and is read from the environment, so the
same pipeline can serve any site without code changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Models:
    """Model per role. Reviewers run every day, so they use the cheap tier."""

    worker: str = field(default_factory=lambda: _env("MODEL_WORKER", "gemini-3.7-flash"))
    author: str = field(default_factory=lambda: _env("MODEL_AUTHOR", "gemini-pro-latest"))


@dataclass(frozen=True)
class SiteConfig:
    """The target site the pipeline writes for."""

    name: str = field(default_factory=lambda: _env("SITE_NAME", "the site"))
    api_url: str = field(default_factory=lambda: _env("SITE_API_URL"))
    api_token: str = field(default_factory=lambda: _env("SITE_API_TOKEN"))
    timeout_seconds: float = field(
        default_factory=lambda: _env_float("SITE_API_TIMEOUT", 30.0)
    )
    # Off only for a site not yet on a real certificate — an IP address with a
    # self-signed one, typically during setup. It means the token travels
    # without a verified peer at the other end, so turn it back on the day the
    # site gets a domain.
    verify_tls: bool = field(
        default_factory=lambda: _env_bool("SITE_API_VERIFY_TLS", True)
    )

    @property
    def configured(self) -> bool:
        return bool(self.api_url and self.api_token)


@dataclass(frozen=True)
class ContentConfig:
    """What to write about, in which language, for whom."""

    language: str = field(default_factory=lambda: _env("CONTENT_LANGUAGE", "fa"))
    language_name: str = field(
        default_factory=lambda: _env("CONTENT_LANGUAGE_NAME", "Persian (Farsi)")
    )
    domain: str = field(
        default_factory=lambda: _env("CONTENT_DOMAIN", "the site's industry")
    )
    audience: str = field(
        default_factory=lambda: _env("CONTENT_AUDIENCE", "prospective customers")
    )
    tone: str = field(
        default_factory=lambda: _env("CONTENT_TONE", "professional, plain, factual")
    )
    min_words: int = field(default_factory=lambda: _env_int("CONTENT_MIN_WORDS", 700))
    max_words: int = field(default_factory=lambda: _env_int("CONTENT_MAX_WORDS", 1200))


@dataclass(frozen=True)
class QualityConfig:
    """The deterministic gate. Changing these changes what gets published."""

    max_revision_rounds: int = field(
        default_factory=lambda: _env_int("MAX_REVISION_ROUNDS", 3)
    )
    min_average_score: float = field(
        default_factory=lambda: _env_float("MIN_AVERAGE_SCORE", 8.0)
    )
    min_seo_score: float = field(default_factory=lambda: _env_float("MIN_SEO_SCORE", 7.0))


@dataclass(frozen=True)
class ImageConfig:
    """Pictures for the article: one lead image plus a few in the body."""

    enabled: bool = field(default_factory=lambda: _env_bool("IMAGES_ENABLED", True))
    model: str = field(default_factory=lambda: _env("IMAGE_MODEL", "gemini-3.1-flash-image"))
    style: str = field(
        default_factory=lambda: _env(
            "IMAGE_STYLE",
            "clean editorial photography, natural daylight, realistic equipment, "
            "uncluttered composition, muted professional colour palette",
        )
    )
    # A ceiling, not a target: each picture costs a call, and an article
    # wallpapered in generated images looks worse than one with two good ones.
    max_in_body: int = field(default_factory=lambda: _env_int("IMAGE_MAX_IN_BODY", 3))


@dataclass(frozen=True)
class NotifyConfig:
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)


@dataclass(frozen=True)
class Settings:
    models: Models = field(default_factory=Models)
    site: SiteConfig = field(default_factory=SiteConfig)
    content: ContentConfig = field(default_factory=ContentConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    images: ImageConfig = field(default_factory=ImageConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "data/pipeline.db"))
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", False))


settings = Settings()
