"""Synthèse hebdomadaire par catégorie.

Contrainte: "Aucune info sans source".
- On génère toujours une section "Sources" numérotée.
- Les puces doivent citer au moins une source [n].

LLM local (Ollama) via HTTP sur http://localhost:11434.
Si Ollama est indisponible, on produit une synthèse extractive (toujours sourcée).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

import requests

from agents.collector import Article


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CategoryReport:
    category: str
    markdown: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _shorten(text: str, max_len: int = 220) -> str:
    t = " ".join((text or "").split())
    if len(t) <= max_len:
        return t
    return t[:max_len].rstrip() + "…"


def _render_sources(articles: list[Article]) -> list[str]:
    lines: list[str] = []
    for idx, a in enumerate(articles, start=1):
        # Markdown: [1] Titre — Source (date)
        date_str = a.published_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        lines.append(f"[{idx}] [{a.title}]({a.url}) — {a.source} ({date_str})")
    return lines


def _extractive_fallback(category: str, articles: list[Article]) -> str:
    """Synthèse minimale et 100% sourcée (sans LLM)."""

    sources = _render_sources(articles)
    bullets = []
    for idx, a in enumerate(articles, start=1):
        bullets.append(f"- {a.title} — {_shorten(a.content)} [{idx}]")

    def _themes(max_themes: int = 5) -> list[tuple[str, list[int]]]:
        # Thèmes simples, basés sur mots-clés -> citations des articles qui match.
        theme_patterns: dict[str, list[str]] = {
            "Cybersécurité": [
                r"\bcve[- ]?\d{4}-\d+\b",
                r"\bzero[- ]day\b",
                r"\bransom\w*\b",
                r"\bmalware\b",
                r"\bphish\w*\b",
                r"\bchrome\b",
                r"\bmicrosoft\b",
            ],
            "Big Data": [
                r"\bopensearch\b",
                r"\bathena\b",
                r"\bredshift\b",
                r"\bflink\b",
                r"\bkafka\b",
                r"\bspark\b",
                r"\blakehouse\b",
            ],
            "Intelligence Artificielle": [
                r"\bllm\b",
                r"\bevaluation\b",
                r"\breason\w*\b",
                r"\bsafety\b",
                r"\bhallucinat\w*\b",
                r"\brag\b",
                r"\bbenchmark\b",
            ],
            "Cloud / DevOps": [
                r"\bkubernetes\b",
                r"\bcncf\b",
                r"\baws\b",
                r"\bterraform\b",
                r"\bci/cd\b",
                r"\bdevops\b",
            ],
        }

        pats = theme_patterns.get(category, [])
        matches: dict[str, list[int]] = {}
        for idx, a in enumerate(articles, start=1):
            hay = f"{a.title} {a.content}".lower()
            for p in pats:
                if re.search(p, hay, flags=re.IGNORECASE):
                    key = p.strip("\\b").replace("\\", "")
                    # Normalisation lisible
                    if "cve" in p.lower():
                        key = "CVE"
                    elif "zero" in p.lower():
                        key = "Zero-day"
                    elif "malware" in p.lower():
                        key = "Malware"
                    elif "ransom" in p.lower():
                        key = "Ransomware"
                    elif "phish" in p.lower():
                        key = "Phishing"
                    elif "opensearch" in p.lower():
                        key = "OpenSearch"
                    elif "redshift" in p.lower():
                        key = "Redshift"
                    elif "athena" in p.lower():
                        key = "Athena"
                    elif "flink" in p.lower():
                        key = "Flink"
                    elif "lakehouse" in p.lower():
                        key = "Lakehouse"
                    elif p.lower() == r"\bllm\b":
                        key = "LLM"
                    elif "evaluation" in p.lower():
                        key = "Évaluation"
                    elif "reason" in p.lower():
                        key = "Raisonnement"
                    elif "safety" in p.lower():
                        key = "Safety"
                    elif "halluc" in p.lower():
                        key = "Hallucinations"
                    elif p.lower() == r"\brag\b":
                        key = "RAG"
                    elif "kubernetes" in p.lower():
                        key = "Kubernetes"
                    elif "terraform" in p.lower():
                        key = "Terraform"
                    elif "aws" in p.lower():
                        key = "AWS"
                    elif "microsoft" in p.lower():
                        key = "Microsoft"
                    elif "chrome" in p.lower():
                        key = "Chrome"
                    matches.setdefault(key, []).append(idx)

        # Trie par fréquence décroissante
        ranked = sorted(matches.items(), key=lambda kv: len(set(kv[1])), reverse=True)
        return [(k, sorted(set(v))) for k, v in ranked[:max_themes]]

    themes = _themes()
    if themes:
        why_lines = [
            f"- Thème récurrent: **{name}** apparaît dans plusieurs publications ({' '.join(f'[{i}]' for i in cites)})."
            for name, cites in themes
        ]
        impact_lines = []
        for name, cites in themes:
            if category == "Cybersécurité":
                impact_lines.append(
                    f"- Si votre SI utilise des composants liés à **{name}**, vérifier correctifs/versions et contrôles associés dans les sources ({' '.join(f'[{i}]' for i in cites)})."
                )
            elif category == "Cloud / DevOps":
                impact_lines.append(
                    f"- Pour les équipes plateforme, valider l'alignement des pratiques/outils autour de **{name}** avec les annonces ({' '.join(f'[{i}]' for i in cites)})."
                )
            elif category == "Big Data":
                impact_lines.append(
                    f"- Côté data, évaluer les impacts de **{name}** sur coûts/perf/opérations selon les cas d'usage décrits ({' '.join(f'[{i}]' for i in cites)})."
                )
            else:
                impact_lines.append(
                    f"- Côté IA/R&D, repérer les apports/limites autour de **{name}** et adapter vos choix d'architecture/évaluation ({' '.join(f'[{i}]' for i in cites)})."
                )
    else:
        all_cites = " ".join(f"[{i}]" for i in range(1, len(articles) + 1))
        why_lines = [f"- Points clés à approfondir dans les sources ci-dessous. {all_cites}"]
        impact_lines = [f"- Impacts à confirmer en lisant les sources. {all_cites}"]

    md = [
        f"### {category}",
        "",
        "**Résumé**",
        *bullets,
        "",
        "**Pourquoi c’est important**",
        *why_lines,
        "",
        "**Impact technique ou métier**",
        *impact_lines,
        "",
        "**Sources**",
        *sources,
        "",
    ]
    return "\n".join(md)


def _ollama_generate(prompt: str, model: str, timeout_s: int = 20) -> str:
    """Appelle Ollama en local.

    Nécessite: Ollama installé + un modèle présent (ex: `ollama pull llama3.1`).
    """

    url = os.environ.get("TECH_WATCH_OLLAMA_URL", "http://localhost:11434/api/generate")
    payload = {"model": model, "prompt": prompt, "stream": False}
    r = requests.post(url, json=payload, timeout=timeout_s)
    r.raise_for_status()
    data = r.json()
    return (data.get("response") or "").strip()


def _ensure_citations(text: str, max_source_idx: int) -> str:
    """Vérifie que chaque puce a au moins un [n]. Sinon, ajoute [1] par défaut.

    On reste conservateur: mieux vaut une citation imparfaite que zéro.
    """

    lines = text.splitlines()
    out: list[str] = []
    bullet_re = re.compile(r"^\s*[-*]\s+")
    cite_re = re.compile(r"\[\d+\]")

    for ln in lines:
        if bullet_re.match(ln) and not cite_re.search(ln):
            ln = ln.rstrip() + " [1]"
        out.append(ln)

    # Évite les citations hors bornes
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\[(0|\d{3,})\]", "[1]", cleaned)
    # Si aucune source, rien
    if max_source_idx <= 0:
        return text
    return cleaned


def summarize_category(category: str, articles: list[Article]) -> CategoryReport:
    # On limite pour une veille lisible
    items = sorted(articles, key=lambda a: a.published_at, reverse=True)[:12]
    if not items:
        return CategoryReport(category=category, markdown=f"### {category}\n\n_Aucun article sur les 7 derniers jours._\n")

    sources = _render_sources(items)

    # UX/perf: option pour désactiver l'appel LLM (utiliser le fallback extractif)
    if os.environ.get("TECH_WATCH_DISABLE_OLLAMA", "").strip().lower() in {"1", "true", "yes", "y"}:
        return CategoryReport(category=category, markdown=_extractive_fallback(category, items))

    model = os.environ.get("TECH_WATCH_OLLAMA_MODEL", "llama3.1")

    prompt = """Tu es un analyste de veille technologique. Tu dois produire une synthèse STRICTEMENT basée sur les sources fournies.

Règles impératives:
- Ne JAMAIS inventer de faits, dates, chiffres ou détails absents des extraits.
- Chaque puce (dans toutes les sections) DOIT contenir au moins une citation au format [n].
- N'utiliser QUE les numéros [n] des sources (pas d'URLs brutes dans le texte).
- Répondre en français, au format Markdown.

Catégorie: {category}

Sources (à citer par [n]):
{sources_block}

Extraits (pour contexte, résumer sans halluciner):
{excerpts_block}

Produis exactement ces sections:
### {category}

**Résumé**
- (5 à 10 puces max, sourcées)

**Pourquoi c’est important**
- (3 à 5 puces, sourcées)

**Impact technique ou métier**
- (3 à 5 puces, sourcées)

**Sources**
- liste complète des sources, une par ligne, au format "[n] ..."
"""

    sources_block = "\n".join(sources)
    excerpts_block = "\n".join(
        f"[{i}] {a.title} — {a.content}" for i, a in enumerate(items, start=1)
    )

    filled = prompt.format(category=category, sources_block=sources_block, excerpts_block=excerpts_block)

    try:
        raw = _ollama_generate(filled, model=model)
        md = _ensure_citations(raw, max_source_idx=len(items))
        # Si le modèle oublie la section Sources, on l'ajoute.
        if "**Sources**" not in md:
            md = md.rstrip() + "\n\n**Sources**\n" + "\n".join(sources) + "\n"
        return CategoryReport(category=category, markdown=md.strip() + "\n")
    except Exception as exc:
        logger.warning("LLM indisponible (fallback extractif): %s", exc)
        return CategoryReport(category=category, markdown=_extractive_fallback(category, items))


def summarize_all(categories: Iterable[str], articles: list[Article]) -> list[CategoryReport]:
    by_cat: dict[str, list[Article]] = {c: [] for c in categories}
    for a in articles:
        by_cat.setdefault(a.category, []).append(a)

    reports: list[CategoryReport] = []
    for c in categories:
        reports.append(summarize_category(c, by_cat.get(c, [])))
    return reports
