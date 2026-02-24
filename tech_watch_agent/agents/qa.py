"""Q&R (assistant) basé sur l’historique SQLite.

Objectif: répondre en français à partir des veilles stockées (table `articles`),
avec citations vers les sources.

- 100% local
- Utilise Ollama si disponible, sinon fallback extractif
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

import requests


@dataclass(frozen=True)
class QAArticle:
    title: str
    published_at: datetime
    excerpt: str
    source: str
    url: str
    category: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cutoff_start_of_day_utc(days: int) -> datetime:
    now = _utc_now().astimezone(timezone.utc)
    start_date = (now.date() - timedelta(days=int(days)))
    return datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)


def _shorten(text: str, max_len: int = 380) -> str:
    t = " ".join((text or "").split())
    if len(t) <= max_len:
        return t
    return t[:max_len].rstrip() + "…"


_STOPWORDS = {
    # FR
    "avec",
    "pour",
    "dans",
    "sur",
    "chez",
    "vous",
    "nous",
    "ils",
    "elles",
    "elle",
    "lui",
    "leurs",
    "notre",
    "votre",
    "mais",
    "donc",
    "or",
    "ni",
    "car",
    "que",
    "qui",
    "quoi",
    "dont",
    "où",
    "quand",
    "comment",
    "plus",
    "moins",
    "très",
    "tres",
    "tout",
    "tous",
    "toute",
    "toutes",
    "afin",
    "comme",
    "aussi",
    "ainsi",
    "être",
    "etre",
    "été",
    "ete",
    "avoir",
    "fait",
    "faits",
    "selon",
    "depuis",
    "entre",
    "vers",
    "sans",
    "sous",
    "cette",
    "ceci",
    "cela",
    "celui",
    "celle",
    "celles",
    "ceux",
    "les",
    "des",
    "une",
    "un",
    "du",
    "de",
    "la",
    "le",
    "et",
    "en",
    "au",
    "aux",
    "par",
    "se",
    "sa",
    "son",
    "ses",
    # EN/common glue
    "the",
    "and",
    "for",
    "with",
    "from",
    "into",
    "over",
    "under",
    "this",
    "that",
    "these",
    "those",
    "your",
    "our",
    "their",
    "new",
    "update",
    "release",
    "announces",
    "announced",
    "blog",
    "report",
}


def _looks_like_academic(a: QAArticle) -> bool:
    hay = f"{a.source} {a.title} {a.url}".lower()
    return "arxiv" in hay or "doi" in hay or "openreview" in hay


def _detect_category_preference(question: str) -> str | None:
    q = (question or "").lower()
    if any(k in q for k in ["cyber", "cybersécurité", "cybersecurite", "cve", "vuln", "ransom", "malware", "phishing"]):
        return "Cybersécurité"
    if any(k in q for k in ["big data", "kafka", "spark", "lakehouse", "etl", "elt", "warehouse"]):
        return "Big Data"
    if any(k in q for k in ["ia", "intelligence artificielle", "llm", "rag", "fine-tuning", "benchmark", "modèle"]):
        return "Intelligence Artificielle"
    if any(k in q for k in ["cloud", "devops", "kubernetes", "terraform", "ci/cd", "aws", "azure", "gcp"]):
        return "Cloud / DevOps"
    return None


def _wants_news(question: str) -> bool:
    q = (question or "").lower()
    return bool(re.search(r"\b(nouvelle|news|actu|actualité|actualite|info)\b", q))


def _tokenize(text: str) -> list[str]:
    words = (
        (text or "")
        .lower()
        .replace("’", "'")
        .replace("-", " ")
    )
    words = re.sub(r"[^a-z0-9àâçéèêëîïôûùüÿñæœ\s']", " ", words)
    toks = [w.strip("'") for w in words.split() if w.strip("'")]
    out: list[str] = []
    for t in toks:
        if len(t) < 3:
            continue
        if t in _STOPWORDS:
            continue
        out.append(t)
    return out


def _parse_iso(dt: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(dt).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def load_articles_for_qa(db_path: str, limit: int = 3000, days: int | None = None) -> list[QAArticle]:
    """Charge un historique d’articles (par défaut les plus récents).

    On limite pour éviter des réponses lentes si l’historique grossit beaucoup.
    """

    items: list[QAArticle] = []
    with sqlite3.connect(db_path) as conn:
        if days is not None and int(days) > 0:
            cutoff_iso = _cutoff_start_of_day_utc(int(days)).isoformat()
            cur = conn.execute(
                """
                SELECT title, excerpt, source_name, category, published_at, link
                FROM articles
                WHERE published_at >= ?
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (cutoff_iso, int(limit)),
            )
        else:
            cur = conn.execute(
                """
                SELECT title, excerpt, source_name, category, published_at, link
                FROM articles
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (int(limit),),
            )
        for title, excerpt, source_name, category, published_at, link in cur.fetchall():
            dt = _parse_iso(published_at)
            if not dt:
                continue
            items.append(
                QAArticle(
                    title=str(title or ""),
                    excerpt=str(excerpt or ""),
                    source=str(source_name or ""),
                    category=str(category or ""),
                    published_at=dt,
                    url=str(link or ""),
                )
            )
    return items


def retrieve(question: str, articles: list[QAArticle], k: int = 8) -> list[QAArticle]:
    q_tokens = set(_tokenize(question))
    if not q_tokens:
        return articles[:k]

    pref_cat = _detect_category_preference(question)
    wants_news = _wants_news(question)

    def score(a: QAArticle) -> float:
        text = f"{a.title} {a.excerpt} {a.source} {a.category}"
        a_tokens = _tokenize(text)
        if not a_tokens:
            return 0.0
        overlap = sum(1 for t in a_tokens if t in q_tokens)
        # boost pour mots très discriminants
        cve_boost = 2.5 if ("cve" in q_tokens and "cve" in text.lower()) else 0.0
        # léger boost récence
        age_days = max(0.0, (_utc_now() - a.published_at).total_seconds() / 86400.0)
        recency = max(0.0, 120.0 - age_days) / 120.0  # 0..1 sur ~4 mois
        cat_boost = 1.5 if (pref_cat and a.category == pref_cat) else 0.0
        academic_penalty = 2.0 if (wants_news and _looks_like_academic(a)) else 0.0
        return overlap + cve_boost + recency * 0.5 + cat_boost - academic_penalty

    ranked = sorted(articles, key=score, reverse=True)
    # filtre les scores nuls si possible
    top = [a for a in ranked if score(a) > 0.0][:k]
    return top if top else ranked[:k]


def _ollama_generate(prompt: str, model: str, timeout_s: int = 25) -> str:
    url = os.environ.get("TECH_WATCH_OLLAMA_URL", "http://localhost:11434/api/generate")
    payload = {"model": model, "prompt": prompt, "stream": False}
    r = requests.post(url, json=payload, timeout=timeout_s)
    r.raise_for_status()
    data = r.json()
    return (data.get("response") or "").strip()


def answer_question(db_path: str, question: str, max_sources: int = 8, days: int | None = None) -> dict:
    articles = load_articles_for_qa(db_path, days=days)

    pref_cat = _detect_category_preference(question)
    wants_news = _wants_news(question)

    # Cas UX: "donne-moi une nouvelle en cyber" -> 1 actu récente, plutôt non-académique.
    if wants_news:
        candidates = articles
        if pref_cat:
            candidates = [a for a in candidates if a.category == pref_cat]

        non_academic = [a for a in candidates if not _looks_like_academic(a)]
        if non_academic:
            candidates = non_academic

        if candidates:
            best = sorted(candidates, key=lambda a: a.published_at, reverse=True)[0]
            sources = [
                {
                    "idx": 1,
                    "title": best.title,
                    "url": best.url,
                    "source": best.source,
                    "category": best.category,
                    "published_at": best.published_at.isoformat(),
                    "excerpt": _shorten(best.excerpt, 360),
                }
            ]
            date_str = best.published_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
            answer = (
                f"Nouvelle récente ({date_str}) — {best.source}:\n"
                f"- {best.title} [1]\n"
                f"{_shorten(best.excerpt, 420)}\n"
                f"Lien: {best.url}"
            )
            return {"ok": True, "answer": answer, "sources": sources}

    picked = retrieve(question, articles, k=max_sources)

    sources = []
    for idx, a in enumerate(picked, start=1):
        sources.append(
            {
                "idx": idx,
                "title": a.title,
                "url": a.url,
                "source": a.source,
                "category": a.category,
                "published_at": a.published_at.isoformat(),
                "excerpt": _shorten(a.excerpt, 260),
            }
        )

    if not picked:
        return {
            "ok": True,
            "answer": "Je n’ai aucune veille enregistrée pour répondre.",
            "sources": [],
        }

    # UX/perf: possibilité de forcer le fallback (ex: mode fast)
    disable_ollama = os.environ.get("TECH_WATCH_DISABLE_OLLAMA", "").strip().lower() in {"1", "true", "yes", "y"}

    # Prompt strict: uniquement sources, en français, citations [n]
    context_lines = []
    for s in sources:
        date_str = ""
        try:
            d = _parse_iso(s["published_at"])
            date_str = d.strftime("%Y-%m-%d") if d else ""
        except Exception:
            date_str = ""
        context_lines.append(
            f"[{s['idx']}] {s['title']} — {s['source']} ({date_str})\nURL: {s['url']}\nExtrait: {s['excerpt']}"
        )

    prompt = (
        "Tu es un assistant de veille technologique. Réponds en FRANÇAIS.\n"
        "Règles STRICTES:\n"
        "- Utilise UNIQUEMENT les sources fournies.\n"
        "- Chaque affirmation importante doit citer au moins une source sous forme [n].\n"
        "- Si l’information n’est pas présente, dis clairement que tu ne sais pas.\n"
        "- Réponse courte et actionnable (5-12 lignes).\n\n"
        f"Question: {question}\n\n"
        "Sources:\n"
        + "\n\n".join(context_lines)
        + "\n"
    )

    if not disable_ollama:
        try:
            model = os.environ.get("TECH_WATCH_OLLAMA_MODEL", "llama3.1")
            ans = _ollama_generate(prompt, model=model, timeout_s=25)
            # Garde-fou: si aucune citation, on force un fallback sourcé
            if picked and "[" not in ans:
                raise RuntimeError("Réponse sans citations")
            return {"ok": True, "answer": ans, "sources": sources}
        except Exception:
            pass

    # Fallback extractif
    bullets = []
    for s in sources:
        bullets.append(f"- {s['title']} [{s['idx']}]\n  {s['excerpt']}")

    ans = (
        "Je n’ai pas de LLM local disponible pour répondre en langage naturel.\n"
        "Voici les publications les plus pertinentes pour ta question (à lire en priorité) :\n"
        + "\n".join(bullets)
    )

    return {"ok": True, "answer": ans, "sources": sources}
