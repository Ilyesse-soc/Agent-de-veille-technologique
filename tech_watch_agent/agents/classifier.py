"""Classification: chaque article doit être classé dans UNE seule catégorie.

Par défaut, on fait confiance à la catégorie de la source (config). En secours,
une classification simple par mots-clés.
"""

from __future__ import annotations

import re
from dataclasses import replace

from agents.collector import Article


CATEGORIES = [
    "Cybersécurité",
    "Big Data",
    "Intelligence Artificielle",
    "Cloud / DevOps",
]


_KEYWORDS = {
    "Cybersécurité": [
        r"\bcve[- ]?\d{4}-\d+\b",
        r"\bexploit\b",
        r"\bransom\w*\b",
        r"\bmalware\b",
        r"\bphish\w*\b",
        r"\bzero[- ]day\b",
        r"\bbreach\b",
        r"\bvuln\w*\b",
    ],
    "Big Data": [
        r"\bspark\b",
        r"\bkafka\b",
        r"\bdelta\b",
        r"\blakehouse\b",
        r"\bwarehouse\b",
        r"\b(etl|elt)\b",
        r"\bdatabricks\b",
        r"\bbig data\b",
    ],
    "Intelligence Artificielle": [
        r"\bllm\b",
        r"\btransformer\b",
        r"\brag\b",
        r"\bfine[- ]tuning\b",
        r"\breinforcement\b",
        r"\b(arxiv|paper|benchmark)\b",
        r"\bgpt\b",
    ],
    "Cloud / DevOps": [
        r"\bkubernetes\b",
        r"\bhelm\b",
        r"\bterraform\b",
        r"\baws\b",
        r"\bgcp\b",
        r"\bazure\b",
        r"\bci/cd\b",
        r"\bdevops\b",
    ],
}


def technical_keyword_score(category: str, text: str) -> int:
    """Score simple (0..N) basé sur des mots-clés techniques forts."""

    pats = _KEYWORDS.get(category, [])
    if not pats:
        return 0
    t = (text or "")
    score = 0
    for p in pats:
        if re.search(p, t, flags=re.IGNORECASE):
            score += 1
    return score


def classify_one(article: Article) -> Article:
    # Source catégorisée = classification déterministe
    if article.category in CATEGORIES:
        return article

    text = f"{article.title} {article.content}".lower()
    scores: dict[str, int] = {c: 0 for c in CATEGORIES}

    for category, patterns in _KEYWORDS.items():
        for p in patterns:
            if re.search(p, text, flags=re.IGNORECASE):
                scores[category] += 1

    best = max(scores.items(), key=lambda kv: kv[1])
    chosen = best[0] if best[1] > 0 else "Cloud / DevOps"
    return replace(article, category=chosen)


def classify_all(articles: list[Article]) -> list[Article]:
    return [classify_one(a) for a in articles]
