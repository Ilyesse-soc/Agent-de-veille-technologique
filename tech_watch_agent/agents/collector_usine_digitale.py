"""Collecteur HTML dédié: Usine Digitale (sans RSS).

Contraintes:
- Extraction directe (scraping léger et respectueux).
- Cible sections: IA, Cybersécurité, Cloud, Big Tech, Robotique.
- Doit produire le format Article normalisé.
- Doit limiter aux articles réellement publiés sur les 7 derniers jours.

Note: le filtrage strict 7 jours est appliqué globalement dans main.py, mais
ce module fait aussi un pré-filtrage pour éviter de parcourir des archives.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

from agents.collector import Article, DEFAULT_USER_AGENT


logger = logging.getLogger(__name__)


USINE_BASE = "https://www.usine-digitale.fr"

# Sections demandées (HTML)
SECTIONS: dict[str, str] = {
    "Intelligence artificielle": f"{USINE_BASE}/intelligence-artificielle/",
    "Cybersécurité": f"{USINE_BASE}/cybersecurite/",
    "Cloud": f"{USINE_BASE}/cloud/",
    "Big Tech": f"{USINE_BASE}/big-tech/",
    "Robotique": f"{USINE_BASE}/robotique/",
}

# Mapping vers les 4 catégories de l’agent
SECTION_TO_CATEGORY: dict[str, str] = {
    "Intelligence artificielle": "Intelligence Artificielle",
    "Cybersécurité": "Cybersécurité",
    "Cloud": "Cloud / DevOps",
    "Big Tech": "Cloud / DevOps",
    "Robotique": "Intelligence Artificielle",
}


_FR_MONTHS = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _make_uid(url: str, title: str) -> str:
    raw = (url or "") + "\n" + (title or "")
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    return " ".join(text.split())


def _parse_datetime_from_meta(soup: BeautifulSoup) -> Optional[datetime]:
    # Meta OpenGraph/Article
    for prop in ("article:published_time", "og:published_time", "article:modified_time"):
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            value = tag["content"].strip().replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(value)
                return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue

    # <time datetime="...">
    t = soup.find("time")
    if t:
        dt_attr = (t.get("datetime") or "").strip()
        if dt_attr:
            try:
                dt = datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
                return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass

    return None


def _parse_french_date(text: str) -> Optional[datetime]:
    """Parse des formats comme: '17 février 2026' (optionnellement avec heure)."""

    t = " ".join((text or "").strip().lower().split())
    if not t:
        return None

    # Ex: "17 février 2026" ou "17 février 2026 à 08h30"
    parts = t.replace("à", " ").replace("h", ":").split()
    # Cherche motif jour mois année
    try:
        day = int(parts[0])
        month = _FR_MONTHS.get(parts[1])
        year = int(parts[2])
        if not month:
            return None
        hour = 0
        minute = 0
        # Optionnel: heure sous forme 08:30
        for p in parts[3:]:
            if ":" in p:
                hh, mm = p.split(":", 1)
                if hh.isdigit() and mm.isdigit():
                    hour = int(hh)
                    minute = int(mm)
                    break
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except Exception:
        return None


def _extract_content(soup: BeautifulSoup, max_len: int = 2000) -> str:
    # Chapeau/intro
    for cls in ("chapo", "article-chapo", "standfirst", "article__intro", "intro"):
        node = soup.find(class_=lambda c: isinstance(c, str) and cls in c)
        if node:
            txt = _html_to_text(str(node))
            if txt:
                return txt[:max_len].rstrip() + ("…" if len(txt) > max_len else "")

    # Meta description
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        txt = _html_to_text(meta["content"])
        if txt:
            return txt[:max_len].rstrip() + ("…" if len(txt) > max_len else "")

    # 1-3 premiers paragraphes
    body = soup.find(attrs={"itemprop": "articleBody"}) or soup.find("article") or soup
    paras = [p.get_text(" ", strip=True) for p in body.find_all("p")]
    paras = [" ".join(p.split()) for p in paras if p and len(p.strip()) > 40]
    txt = " ".join(paras[:3]).strip()
    if len(txt) > max_len:
        txt = txt[:max_len].rstrip() + "…"
    return txt


def _extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        return " ".join(h1.get_text(" ", strip=True).split())
    title = soup.find("title")
    if title:
        return " ".join(title.get_text(" ", strip=True).split())
    return ""


def _collect_article_urls_from_section(html: bytes, base_url: str) -> list[str]:
    soup = BeautifulSoup(html or b"", "html.parser")

    urls: list[str] = []
    seen: set[str] = set()

    # Heuristique: liens dans des <article>
    for art in soup.find_all("article"):
        a = art.find("a", href=True)
        if not a:
            continue
        href = a["href"].strip()
        if not href:
            continue
        if href.startswith("/"):
            href = USINE_BASE + href
        if not href.startswith(USINE_BASE):
            continue
        # évite pages de section
        if href.rstrip("/") == base_url.rstrip("/"):
            continue
        if href not in seen:
            seen.add(href)
            urls.append(href)

    # Fallback: liens typiques contenant /article/
    if not urls:
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href:
                continue
            if href.startswith("/"):
                href = USINE_BASE + href
            if not href.startswith(USINE_BASE):
                continue
            if "/article/" in href and href not in seen:
                seen.add(href)
                urls.append(href)

    return urls


def collect_usine_digitale(days: int = 7, per_section_limit: int = 8, timeout_s: int = 7) -> list[Article]:
    """Collecte HTML Usine Digitale.

    - days: fenêtre temporelle (pré-filtrage)
    - per_section_limit: limite de pages article consultées par rubrique
    - timeout_s: timeout strict (<= 10s)
    """

    cutoff = _utc_now() - timedelta(days=days)
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Referer": USINE_BASE + "/",
        "Connection": "keep-alive",
    }

    session = requests.Session()
    session.headers.update(headers)

    # Warmup léger (sans bloquer)
    try:
        session.get(USINE_BASE + "/", timeout=(3, min(timeout_s, 6)))
    except Exception:
        pass

    collected: list[Article] = []

    for section_name, section_url in SECTIONS.items():
        try:
            r = session.get(section_url, timeout=(3, timeout_s))
            if not r.ok:
                logger.warning("Usine Digitale section KO %s: %s", section_url, r.status_code)
                continue
        except Exception as exc:
            logger.warning("Usine Digitale section erreur %s: %s", section_url, exc)
            continue

        urls = _collect_article_urls_from_section(r.content, section_url)
        urls = urls[:per_section_limit]

        for url in urls:
            # Throttle minimal (respectueux) sans exploser le temps total.
            time.sleep(0.08)
            try:
                ar = session.get(url, timeout=(3, timeout_s))
                if not ar.ok:
                    continue
            except Exception:
                continue

            soup = BeautifulSoup(ar.content, "html.parser")
            title = _extract_title(soup)
            if not title:
                continue

            published_at = _parse_datetime_from_meta(soup)
            if not published_at:
                # Essai date visible (souvent affichée)
                time_node = soup.find("time")
                if time_node:
                    published_at = _parse_french_date(time_node.get_text(" ", strip=True))
            if not published_at:
                continue

            published_at = published_at.astimezone(timezone.utc) if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
            if published_at < cutoff:
                # On ne conserve pas les archives
                continue

            content = _extract_content(soup)
            if not (content or "").strip():
                continue

            category = SECTION_TO_CATEGORY.get(section_name, "Cloud / DevOps")

            collected.append(
                Article(
                    title=title,
                    published_at=published_at,
                    content=content,
                    category=category,
                    source="Usine Digitale",
                    url=url,
                    uid=_make_uid(url, title),
                )
            )

    logger.info("Usine Digitale: %d articles (pré-filtrés %d jours)", len(collected), days)
    return collected
