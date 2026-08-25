"""The only door between the pipeline and the target site.

No agent touches the site's database. Everything goes through this HTTP client,
which means one place to authenticate, one place to log, and a site that can be
swapped by changing environment variables.

Expected endpoints, relative to `SITE_API_URL`:

    POST   /posts      create a draft (or a published post once trusted)
    GET    /products   catalogue entries the article may mention
    GET    /articles   what is already published, for internal links
    GET    /stats      per-article view counts (read by the weekly analyst)
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from .config import SiteConfig
from .render import markdown_to_html

logger = logging.getLogger(__name__)


class SiteClient:
    def __init__(self, config: SiteConfig, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.config.api_url.rstrip('/')}/{path.lstrip('/')}"

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Read from the site. A read failure degrades the run, never kills it."""
        if not self.config.configured:
            logger.warning("Site API is not configured; %s returns nothing.", path)
            return None
        try:
            response = httpx.get(
                self._url(path),
                params=params,
                headers=self._headers(),
                timeout=self.config.timeout_seconds,
                # Sites redirect for their own reasons — http to https, a
                # trailing slash. Not following turns a working endpoint into
                # silently missing data.
                follow_redirects=True,
                verify=self.config.verify_tls,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - any failure means "no data"
            logger.warning("Site API GET %s failed: %s", path, exc)
            return None

    def products(self, limit: int = 50) -> list[dict[str, Any]]:
        """Catalogue data the article may cite, straight from the site."""
        payload = self._get("/products", {"limit": limit})
        if isinstance(payload, dict):
            payload = payload.get("products") or payload.get("items") or []
        return payload if isinstance(payload, list) else []

    def published_articles(self, limit: int = 30) -> list[dict[str, Any]]:
        payload = self._get("/articles", {"limit": limit})
        if isinstance(payload, dict):
            payload = payload.get("articles") or payload.get("items") or []
        return payload if isinstance(payload, list) else []

    def taxonomy(self) -> dict[str, list[dict[str, Any]]]:
        """The categories and tags this site already uses.

        The writer picks from these rather than inventing its own: a pipeline
        coining new sections would quietly reorganise someone's navigation.
        """
        payload = self._get("/taxonomy")
        if not isinstance(payload, dict):
            return {"categories": [], "tags": []}
        return {
            "categories": payload.get("categories") or [],
            "tags": payload.get("tags") or [],
        }

    def stats(self, days: int = 30) -> list[dict[str, Any]]:
        payload = self._get("/stats", {"days": days})
        if isinstance(payload, dict):
            payload = payload.get("stats") or payload.get("items") or []
        return payload if isinstance(payload, list) else []

    def upload_image(
        self, data: bytes, filename: str, alt: str = ""
    ) -> str | None:
        """Put one picture in the site's media library, return its URL.

        Base64 in JSON rather than multipart: one content type for the whole
        API, and an article's worth of images is small enough that the encoding
        overhead is not worth a second code path.
        """
        if self.dry_run:
            logger.info("DRY_RUN: would upload image %s (%s bytes)", filename, len(data))
            return f"/uploads/dry-run-{filename}"
        if not self.config.configured:
            return None
        try:
            response = httpx.post(
                self._url("/media"),
                json={
                    "filename": filename,
                    "alt": alt,
                    "data_base64": base64.b64encode(data).decode("ascii"),
                },
                headers=self._headers(),
                timeout=max(self.config.timeout_seconds, 60.0),
                follow_redirects=True,
                verify=self.config.verify_tls,
            )
            response.raise_for_status()
            url = str(response.json().get("path") or "")
            return url or None
        except Exception as exc:  # noqa: BLE001 - an article beats an illustration
            logger.warning("Image upload failed for %s: %s", filename, exc)
            return None

    def create_post(
        self,
        *,
        title: str,
        slug: str,
        excerpt: str,
        body: str,
        status: str = "draft",
        featured_image: str = "",
        category: str = "",
        tags: list[str] | None = None,
        seo_title: str = "",
        meta_description: str = "",
        related_products: list[str] | None = None,
        related_solutions: list[str] | None = None,
        structured_data: list[dict[str, Any]] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """Send the article over. Returns (ok, remote id or error message).

        Both forms of the body travel: Markdown as written, and HTML ready to
        store. Sites keep one or the other, and converting here spares every
        adopter from adding a Markdown renderer of its own.

        `structured_data` is a list of JSON-LD blocks built in code from checked
        values. A site that ignores the field loses nothing; one that embeds
        each block in a `<script type="application/ld+json">` gets rich results
        it can trust.

        Unlike the reads above, a failure here is reported rather than
        swallowed: the article exists only in this pipeline until the site
        confirms it.
        """
        payload = {
            "title": title,
            "slug": slug,
            "excerpt": excerpt,
            "body": body,
            "body_html": markdown_to_html(body),
            "status": status,
            "featured_image": featured_image,
            "category": category,
            "tags": tags or [],
            "seo_title": seo_title,
            "meta_description": meta_description,
            "related_products": related_products or [],
            "related_solutions": related_solutions or [],
            "structured_data": structured_data or [],
            "meta": meta or {},
        }
        if self.dry_run:
            logger.info("DRY_RUN: would create %s post %r", status, title)
            return True, "dry-run"
        if not self.config.configured:
            return False, "SITE_API_URL / SITE_API_TOKEN are not set"
        try:
            response = httpx.post(
                self._url("/posts"),
                json=payload,
                headers=self._headers(),
                timeout=self.config.timeout_seconds,
                follow_redirects=True,
                verify=self.config.verify_tls,
            )
            response.raise_for_status()
            data = response.json() if response.content else {}
            remote_id = str(data.get("id") or data.get("slug") or slug)
            return True, remote_id
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            logger.error("Site API POST /posts failed: %s", exc)
            return False, str(exc)
