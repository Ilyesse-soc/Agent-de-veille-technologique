"""Filtrage temporel (7 jours) + déduplication via SQLite.

Contrainte: EXCLURE tout article > 7 jours. Éliminer les doublons (SQLite).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from agents.collector import Article
from agents.classifier import technical_keyword_score


logger = logging.getLogger(__name__)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL,
    link TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    category TEXT NOT NULL,
    published_at TEXT NOT NULL,
    collected_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cutoff_start_of_day_utc(days: int) -> datetime:
    """Cutoff inclusif basé sur le jour (pas l'heure).

    Exemple: si on est lundi (peu importe l'heure), `days=7` inclut tout le lundi
    précédent + aujourd'hui.
    """

    now = _utc_now().astimezone(timezone.utc)
    start_date = (now.date() - timedelta(days=days))
    return datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)


def init_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def filter_last_7_days(articles: list[Article]) -> list[Article]:
    cutoff = _cutoff_start_of_day_utc(days=7)
    kept = [a for a in articles if a.published_at >= cutoff]
    logger.info("Filtre 7 jours: %d -> %d", len(articles), len(kept))
    return kept


def drop_empty_content(articles: list[Article], min_len: int = 1) -> list[Article]:
    kept = [a for a in articles if (a.content or "").strip() and len((a.content or "").strip()) >= min_len]
    logger.info("Filtre contenu non vide: %d -> %d", len(articles), len(kept))
    return kept


def dedupe_by_url(articles: list[Article]) -> list[Article]:
    """Déduplication intra-run: conserve le premier article rencontré par URL.

    Comparaison strictement sur `article.url`.
    """

    seen: set[str] = set()
    deduped: list[Article] = []
    for a in articles:
        if a.url in seen:
            continue
        seen.add(a.url)
        deduped.append(a)

    logger.info("Dédup intra-run (url): %d -> %d", len(articles), len(deduped))
    return deduped


def dedupe_by_url(articles: list[Article]) -> list[Article]:
    """Déduplication intra-run stricte par URL.

    Conserve le premier article rencontré pour chaque `url`.
    """

    seen: set[str] = set()
    deduped: list[Article] = []

    for a in articles:
        if a.url in seen:
            continue
        seen.add(a.url)
        deduped.append(a)

    logger.info("Dédup intra-run (url): %d -> %d", len(articles), len(deduped))
    return deduped


def reduce_volume_per_category(
    articles: list[Article],
    per_category_max: int = 15,
) -> list[Article]:
    """Réduit fortement le volume: max N articles par catégorie.

    Priorités:
    - récence
    - présence de mots-clés techniques forts
    - contenu non vide (à appliquer avant via drop_empty_content)
    """

    now = _utc_now()

    def score(a: Article) -> float:
        age_h = max(0.0, (now - a.published_at).total_seconds() / 3600.0)
        recency = max(0.0, 168.0 - age_h) / 168.0  # 0..1 sur 7 jours
        kw = technical_keyword_score(a.category, f"{a.title} {a.content}")
        # pondération: mots-clés dominants, puis récence
        return kw * 3.0 + recency

    by_cat: dict[str, list[Article]] = {}
    for a in articles:
        by_cat.setdefault(a.category, []).append(a)

    reduced: list[Article] = []
    for cat, items in by_cat.items():
        items_sorted = sorted(items, key=lambda x: (score(x), x.published_at), reverse=True)
        reduced.extend(items_sorted[:per_category_max])

    # Stable-ish ordering overall: newest first
    reduced = sorted(reduced, key=lambda a: a.published_at, reverse=True)
    logger.info("Réduction volume: %d -> %d (max %d/catégorie)", len(articles), len(reduced), per_category_max)
    return reduced


def is_known(conn: sqlite3.Connection, link: str) -> bool:
    cur = conn.execute("SELECT 1 FROM articles WHERE link = ? LIMIT 1", (link,))
    return cur.fetchone() is not None


def persist_new(db_path: str, articles: list[Article]) -> list[Article]:
    """Insère les articles inconnus; renvoie la liste des nouveaux."""

    init_db(db_path)
    now = _utc_now()

    new_items: list[Article] = []
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        for a in articles:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO articles(uid, link, title, excerpt, source_name, source_url, category, published_at, collected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    a.uid,
                    a.url,
                    a.title,
                    a.content,
                    a.source,
                    "",  # source_url non stockée dans le format normalisé
                    a.category,
                    _iso(a.published_at),
                    _iso(now),
                ),
            )
            if cur.rowcount:
                new_items.append(a)
        conn.commit()

    logger.info("Dédup SQLite: %d nouveaux", len(new_items))
    return new_items


def load_recent_articles(db_path: str, days: int = 7) -> list[Article]:
    """Charge les articles récents depuis SQLite.

    Objectif: permettre une génération très rapide (UI) sans collecte réseau.
    """

    init_db(db_path)
    cutoff = _cutoff_start_of_day_utc(days=days)
    cutoff_iso = _iso(cutoff)

    items: list[Article] = []
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            """
            SELECT uid, link, title, excerpt, source_name, category, published_at
            FROM articles
            WHERE published_at >= ?
            ORDER BY published_at DESC
            """,
            (cutoff_iso,),
        )
        for uid, link, title, excerpt, source_name, category, published_at in cur.fetchall():
            try:
                dt = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
                dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            items.append(
                Article(
                    title=title,
                    published_at=dt,
                    content=excerpt,
                    category=category,
                    source=source_name,
                    url=link,
                    uid=uid,
                )
            )

    # Filtrage de sécurité (au cas où)
    return filter_last_7_days(items)
