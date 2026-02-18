"""Collecte des articles depuis les sources (RSS/Atom/HTML).

Objectif: se comporter comme un navigateur normal, extraire titre/date/résumé/lien.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable, Optional
from urllib.parse import urljoin
from urllib.parse import urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

from config.sources import Source


logger = logging.getLogger(__name__)


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Article:
    # Format normalisé requis
    title: str
    published_at: datetime
    content: str
    category: str
    source: str
    url: str

    uid: str  # stable id for DB dedup


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_entry_datetime(entry: object) -> Optional[datetime]:
    """Parse une date à partir d'un item feedparser.

    On privilégie `published_parsed` / `updated_parsed` (struct_time),
    puis `published` / `updated` (string).
    """

    for attr in ("published_parsed", "updated_parsed"):
        value = getattr(entry, attr, None)
        if value:
            try:
                return datetime(*value[:6], tzinfo=timezone.utc)
            except Exception:
                pass

    for attr in ("published", "updated"):
        value = getattr(entry, attr, None)
        if not value:
            continue
        # RFC822
        try:
            dt = parsedate_to_datetime(value)
            return _to_utc(dt)
        except Exception:
            pass

        # ISO 8601 (avec Z)
        try:
            iso = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            return _to_utc(dt)
        except Exception:
            pass

    return None


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    return " ".join(text.split())


def _make_uid(link: str, title: str) -> str:
    raw = (link or "") + "\n" + (title or "")
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def fetch_feed(source: Source, timeout_s: int = 25) -> bytes:
    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}

    def _request_with_retries(
        url: str,
        accept: str,
        connect_s: int,
        read_s: int,
        max_retries: int,
    ) -> requests.Response:
        # Performance/UX: retries max 1 => 2 tentatives (t0, t0.8)
        backoffs = [0.0, 0.8][: max_retries + 1]
        last_exc: Exception | None = None
        hdrs = {**headers, "Accept": accept}
        for attempt, delay in enumerate(backoffs):
            if delay:
                time.sleep(delay)
            try:
                return requests.get(url, headers=hdrs, timeout=(connect_s, read_s))
            except requests.RequestException as exc:
                last_exc = exc
                continue
        assert last_exc is not None
        raise last_exc

    def _get(url: str, max_retries: int) -> requests.Response:
        return _request_with_retries(url, accept="*/*", connect_s=3, read_s=timeout_s, max_retries=max_retries)

    def _get_html(url: str, max_retries: int) -> requests.Response:
        return _request_with_retries(
            url,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            connect_s=3,
            read_s=min(timeout_s, 12),
            max_retries=max_retries,
        )

    def _base(url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}/"

    def _parent(url: str) -> str:
        # Retire le dernier segment de chemin pour tenter discovery.
        p = urlparse(url)
        path = p.path or "/"
        if path.endswith("/"):
            path = path[:-1]
        parent = "/".join(path.split("/")[:-1]) + "/"
        if not parent.startswith("/"):
            parent = "/" + parent
        return f"{p.scheme}://{p.netloc}{parent}"

    def _discover_feed_urls(html_bytes: bytes, base_url: str) -> list[str]:
        soup = BeautifulSoup(html_bytes or b"", "html.parser")
        candidates: list[str] = []
        for link in soup.find_all("link"):
            rel = " ".join(link.get("rel", [])).lower()
            typ = (link.get("type") or "").lower()
            href = (link.get("href") or "").strip()
            if not href:
                continue
            if "alternate" not in rel:
                continue
            if "rss" in typ or "atom" in typ or typ in {"application/xml", "text/xml"}:
                candidates.append(urljoin(base_url, href))
        # Dédup en conservant l'ordre
        seen: set[str] = set()
        uniq: list[str] = []
        for c in candidates:
            if c not in seen:
                uniq.append(c)
                seen.add(c)
        return uniq

    tried: set[str] = set()

    def _try_discovery_from_html(html_url: str, max_retries: int) -> Optional[bytes]:
        if html_url in tried:
            return None
        tried.add(html_url)
        rh = _get_html(html_url, max_retries=max_retries)
        if not rh.ok:
            return None
        if "html" not in (rh.headers.get("Content-Type") or "").lower():
            return None
        for feed_url in _discover_feed_urls(rh.content, rh.url)[:5]:
            if feed_url in tried:
                continue
            tried.add(feed_url)
            rf = _get(feed_url, max_retries=max_retries)
            if rf.ok:
                return rf.content
        return None

    # 1) Essai direct (et variante avec slash)
    last_exc: Exception | None = None
    max_retries = 1

    for url in [source.url, source.url.rstrip("/") + "/"]:
        if url in tried:
            continue
        tried.add(url)
        try:
            r = _get(url, max_retries=max_retries)
            if r.ok:
                ct = (r.headers.get("Content-Type") or "").lower()
                if "html" not in ct:
                    return r.content
                discovered = _try_discovery_from_html(r.url, max_retries=max_retries)
                if discovered:
                    return discovered
            # Redirection explicite
            if r.status_code in {301, 302, 307, 308} and r.headers.get("Location"):
                loc = urljoin(r.url, r.headers["Location"])
                discovered = _try_discovery_from_html(loc, max_retries=max_retries)
                if discovered:
                    return discovered
        except Exception as exc:
            last_exc = exc

    # 2) Discovery depuis parent/racine (rapide)
    for html_url in [
        _parent(source.url),
        _base(source.url),
    ]:
        discovered = _try_discovery_from_html(html_url, max_retries=max_retries)
        if discovered:
            return discovered

    if last_exc:
        raise last_exc
    raise RuntimeError(f"Impossible de récupérer un feed pour {source.url}")


def fetch_feed_fast(source: Source, timeout_s: int = 7, max_retries: int = 0) -> bytes:
    """Fetch rapide d'un feed: pas de discovery HTML, timeouts stricts.

    Objectif: ne jamais bloquer le pipeline/UI sur une source instable.
    """

    headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
    last_exc: Exception | None = None

    # 2 tentatives max (URL et variante slash). Retries max 1 (mais 0 par défaut pour la perf UI).
    backoffs = [0.0, 0.8][: max_retries + 1]

    def _get_once(url: str) -> requests.Response:
        hdrs = {**headers, "Accept": "*/*"}
        exc: Exception | None = None
        for delay in backoffs:
            if delay:
                time.sleep(delay)
            try:
                return requests.get(url, headers=hdrs, timeout=(3, timeout_s))
            except requests.RequestException as e:
                exc = e
                continue
        assert exc is not None
        raise exc

    for url in [source.url, source.url.rstrip("/") + "/"]:
        try:
            r = _get_once(url)
            if not r.ok:
                last_exc = RuntimeError(f"HTTP {r.status_code}")
                continue
            ct = (r.headers.get("Content-Type") or "").lower()
            if "html" in ct:
                last_exc = RuntimeError("Content-Type HTML")
                continue
            return r.content
        except Exception as exc:
            last_exc = exc
            continue

    if last_exc:
        raise last_exc
    raise RuntimeError(f"Impossible de récupérer un feed pour {source.url}")


def collect_from_source(source: Source, timeout_s: int = 25) -> list[Article]:
    """Collecte les articles d'une source RSS/Atom.

    Note: si la date est absente, l'article est ignoré (contrainte "7 jours" + fiabilité).
    """

    def _html_fallback() -> list[Article] | None:
        if source.name == "ANSSI":
            logger.warning("RSS failed → fallback HTML used: %s", source.name)
            return collect_anssi_html(timeout_s=timeout_s)
        if source.name == "CISA":
            logger.warning("RSS failed → fallback HTML used: %s", source.name)
            return collect_cisa_html(timeout_s=timeout_s)
        return None

    try:
        content = fetch_feed(source, timeout_s=timeout_s)
    except Exception as exc:
        fb = _html_fallback()
        if fb is not None:
            return fb
        logger.warning("Source skipped (unreachable): %s (%s) — %s", source.name, source.url, exc)
        return []

    feed = feedparser.parse(content)
    # Certains sites renvoient de l'HTML ou un feed mal formé: on tente discovery si vide.
    if not getattr(feed, "entries", None):
        # fallback HTML pour sources critiques
        fb = _html_fallback()
        if fb is not None:
            return fb
        try:
            headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html,*/*"}
            r = requests.get(source.url, headers=headers, timeout=timeout_s)
            if r.ok and "html" in (r.headers.get("Content-Type") or "").lower():
                soup = BeautifulSoup(r.content, "html.parser")
                candidates = []
                for link in soup.find_all("link"):
                    rel = " ".join(link.get("rel", [])).lower()
                    typ = (link.get("type") or "").lower()
                    href = (link.get("href") or "").strip()
                    if not href:
                        continue
                    if "alternate" not in rel:
                        continue
                    if "rss" in typ or "atom" in typ or typ in {"application/xml", "text/xml"}:
                        candidates.append(urljoin(r.url, href))
                for u in candidates[:3]:
                    rf = requests.get(u, headers=headers, timeout=timeout_s)
                    if rf.ok:
                        feed = feedparser.parse(rf.content)
                        if getattr(feed, "entries", None):
                            break
        except Exception:
            pass
    articles: list[Article] = []

    for entry in getattr(feed, "entries", []) or []:
        title = (getattr(entry, "title", "") or "").strip()
        url = (getattr(entry, "link", "") or "").strip()

        published_at = _parse_entry_datetime(entry)
        if not published_at:
            continue

        # Extrait: summary/description/content
        excerpt_html = (
            getattr(entry, "summary", None)
            or getattr(entry, "description", None)
            or (getattr(entry, "content", [{}]) or [{}])[0].get("value")
            or ""
        )
        content_txt = _html_to_text(excerpt_html)
        if len(content_txt) > 2000:
            content_txt = content_txt[:2000].rstrip() + "…"

        if not title or not url:
            continue

        articles.append(
            Article(
                title=title,
                published_at=_to_utc(published_at),
                content=content_txt,
                category=source.category,
                source=source.name,
                url=url,
                uid=_make_uid(url, title),
            )
        )

    return articles


@dataclass(frozen=True)
class SourceCollectResult:
    source: Source
    articles: list[Article]
    ok: bool
    used_fallback_html: bool
    error: Optional[str] = None


def collect_from_source_detailed(source: Source, timeout_s: int = 7) -> SourceCollectResult:
    """Collecte une source et renvoie un statut exploitable côté UI.

    - timeouts stricts (<= 10s)
    - retries max 1 (gérés dans fetch_feed)
    - jamais d'exception propagée
    """

    def _html_fallback() -> list[Article] | None:
        if source.name == "ANSSI":
            logger.warning("RSS failed → fallback HTML used: %s", source.name)
            return collect_anssi_html(timeout_s=timeout_s)
        if source.name == "CISA":
            logger.warning("RSS failed → fallback HTML used: %s", source.name)
            return collect_cisa_html(timeout_s=timeout_s)
        return None

    try:
        content = fetch_feed_fast(source, timeout_s=timeout_s, max_retries=0)
    except Exception as exc:
        fb = _html_fallback()
        if fb is not None:
            return SourceCollectResult(source=source, articles=fb, ok=len(fb) > 0, used_fallback_html=True, error=None if fb else str(exc))
        return SourceCollectResult(source=source, articles=[], ok=False, used_fallback_html=False, error=str(exc))

    feed = feedparser.parse(content)
    if not getattr(feed, "entries", None):
        fb = _html_fallback()
        if fb is not None:
            return SourceCollectResult(source=source, articles=fb, ok=len(fb) > 0, used_fallback_html=True, error=None if fb else "Feed vide")
        return SourceCollectResult(source=source, articles=[], ok=False, used_fallback_html=False, error="Feed vide")

    # Réutilise la logique existante en parsing le feed
    # (copié minimalement depuis collect_from_source pour éviter de changer le pipeline)
    articles: list[Article] = []
    for entry in getattr(feed, "entries", []) or []:
        title = (getattr(entry, "title", "") or "").strip()
        url = (getattr(entry, "link", "") or "").strip()

        published_at = _parse_entry_datetime(entry)
        if not published_at:
            continue

        excerpt_html = (
            getattr(entry, "summary", None)
            or getattr(entry, "description", None)
            or (getattr(entry, "content", [{}]) or [{}])[0].get("value")
            or ""
        )
        content_txt = _html_to_text(excerpt_html)
        if len(content_txt) > 2000:
            content_txt = content_txt[:2000].rstrip() + "…"

        if not title or not url:
            continue

        articles.append(
            Article(
                title=title,
                published_at=_to_utc(published_at),
                content=content_txt,
                category=source.category,
                source=source.name,
                url=url,
                uid=_make_uid(url, title),
            )
        )

    ok = len(articles) > 0
    return SourceCollectResult(source=source, articles=articles, ok=ok, used_fallback_html=False, error=None if ok else "0 item")


def _parse_iso_or_none(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def collect_anssi_html(timeout_s: int = 10) -> list[Article]:
    """Fallback HTML ANSSI: https://www.ssi.gouv.fr/actualites/"""

    url = "https://www.ssi.gouv.fr/actualites/"
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
        "Referer": "https://www.ssi.gouv.fr/",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    try:
        r = requests.get(url, headers=headers, timeout=(3, timeout_s))
        if not r.ok:
            logger.warning("Source skipped (unreachable): ANSSI HTML %s", r.status_code)
            return []
    except Exception as exc:
        logger.warning("Source skipped (unreachable): ANSSI HTML — %s", exc)
        return []

    soup = BeautifulSoup(r.content, "html.parser")
    items: list[Article] = []

    for art in soup.find_all("article"):
        a = art.find("a", href=True)
        if not a:
            continue
        href = a["href"].strip()
        if not href:
            continue
        full = href if href.startswith("http") else urljoin(url, href)

        title = " ".join(a.get_text(" ", strip=True).split())
        if not title:
            h = art.find(["h2", "h3"])
            title = " ".join(h.get_text(" ", strip=True).split()) if h else ""
        if not title:
            continue

        dt: Optional[datetime] = None
        t = art.find("time")
        if t and t.get("datetime"):
            dt = _parse_iso_or_none(t.get("datetime"))
        if not dt and t:
            # fallback texte
            dt = None
        if not dt:
            continue

        if dt < cutoff:
            continue

        excerpt = ""
        p = art.find("p")
        if p:
            excerpt = " ".join(p.get_text(" ", strip=True).split())

        if not excerpt:
            excerpt = title

        items.append(
            Article(
                title=title,
                published_at=dt,
                content=excerpt[:2000],
                category="Cybersécurité",
                source="ANSSI",
                url=full,
                uid=_make_uid(full, title),
            )
        )

    return items


def collect_cisa_html(timeout_s: int = 10) -> list[Article]:
    """Fallback HTML CISA: https://www.cisa.gov/news-events/news"""

    url = "https://www.cisa.gov/news-events/news"
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.cisa.gov/",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    try:
        r = requests.get(url, headers=headers, timeout=(3, timeout_s))
        if not r.ok:
            logger.warning("Source skipped (unreachable): CISA HTML %s", r.status_code)
            return []
    except Exception as exc:
        logger.warning("Source skipped (unreachable): CISA HTML — %s", exc)
        return []

    soup = BeautifulSoup(r.content, "html.parser")
    items: list[Article] = []

    # Heuristique: cartes / listes contenant <time> + lien
    for block in soup.find_all(["article", "div", "li"]):
        t = block.find("time")
        a = block.find("a", href=True)
        if not (t and a):
            continue
        dt = None
        if t.get("datetime"):
            dt = _parse_iso_or_none(t.get("datetime"))
        if not dt:
            # parfois date en texte (ex: Feb 16, 2026)
            try:
                dt = parsedate_to_datetime(t.get_text(" ", strip=True)).astimezone(timezone.utc)
            except Exception:
                dt = None
        if not dt:
            continue
        if dt < cutoff:
            continue

        href = a["href"].strip()
        if not href:
            continue
        full = href if href.startswith("http") else urljoin(url, href)

        title = " ".join(a.get_text(" ", strip=True).split())
        if not title:
            continue

        excerpt = ""
        p = block.find("p")
        if p:
            excerpt = " ".join(p.get_text(" ", strip=True).split())
        if not excerpt:
            excerpt = title

        items.append(
            Article(
                title=title,
                published_at=dt,
                content=excerpt[:2000],
                category="Cybersécurité",
                source="CISA",
                url=full,
                uid=_make_uid(full, title),
            )
        )

    return items


def collect_all(sources: Iterable[Source], timeout_s: int = 10) -> list[Article]:
    all_items: list[Article] = []
    for src in sources:
        # Timeouts stricts (UX/perf): 6–10s
        src_timeout = min(10, timeout_s)
        try:
            items = collect_from_source(src, timeout_s=src_timeout)
        except Exception as exc:
            # Ne jamais bloquer le pipeline
            logger.warning("Source skipped (unreachable): %s (%s) — %s", src.name, src.url, exc)
            items = []
        logger.info("Source %-22s -> %d items", src.name, len(items))
        all_items.extend(items)
    return all_items
