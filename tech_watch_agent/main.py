"""Agent IA de veille technologique hebdomadaire (local).

Contrainte: données limitées aux 7 derniers jours, sources officielles/reconnues,
aucune info sans source. Sortie: console + output/veille.md (+ PDF optionnel).

Lancement:
- `python main.py`
- (optionnel) `python main.py --pdf` si pandoc est installé.

LLM local:
- Installer Ollama et démarrer le service.
- Choisir un modèle: `setx TECH_WATCH_OLLAMA_MODEL llama3.1`
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agents.classifier import classify_all
from agents.collector import collect_all
from agents.collector_usine_digitale import collect_usine_digitale
from agents.filter import dedupe_by_url, drop_empty_content, filter_last_7_days, persist_new, reduce_volume_per_category
from agents.summarizer import summarize_all
from config.sources import CATEGORIES, SOURCES


ROOT = Path(__file__).resolve().parent
DB_PATH = str(ROOT / "storage" / "history.db")
OUT_MD = ROOT / "output" / "veille.md"
OUT_PDF = ROOT / "output" / "veille.pdf"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
    )


def render_markdown(reports: list[str]) -> str:
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=7)).date().isoformat()
    end = now.date().isoformat()

    header = [
        "# Veille technologique — 7 derniers jours",
        "",
        f"Période: **{start}** → **{end}** (UTC)",
        "",
        "Sources: uniquement celles listées dans la configuration.",
        "",
    ]

    return "\n".join(header) + "\n\n".join(reports)


def try_build_pdf(md_path: Path, pdf_path: Path) -> None:
    """Optionnel: nécessite `pandoc` dans le PATH."""

    try:
        subprocess.run(
            ["pandoc", str(md_path), "-o", str(pdf_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        logging.info("PDF généré: %s", pdf_path)
    except FileNotFoundError:
        logging.warning("pandoc introuvable: PDF non généré")
    except subprocess.CalledProcessError as exc:
        logging.warning("Échec pandoc: %s", (exc.stderr or "").strip())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Agent local de veille (7 jours)")
    p.add_argument("--pdf", action="store_true", help="Génère output/veille.pdf (pandoc requis)")
    return p.parse_args()


def main() -> int:
    setup_logging()
    args = parse_args()

    os.makedirs(ROOT / "output", exist_ok=True)
    os.makedirs(ROOT / "storage", exist_ok=True)

    logging.info("Collecte: %d sources", len(SOURCES))
    collected = collect_all(SOURCES)
    # Usine Digitale (HTML, sans RSS)
    collected.extend(collect_usine_digitale(days=7))

    logging.info("Total collecté: %d", len(collected))

    # 1) Filtrage STRICT 7 jours (avant toute autre étape)
    last_week = filter_last_7_days(collected)

    # 2) Classification (déterministe par source + fallback)
    classified = classify_all(last_week)

    # 3) Contenu non vide
    classified = drop_empty_content(classified, min_len=1)

    # 3bis) Déduplication intra-run strictement par URL (ex: doublons source HTML)
    classified = dedupe_by_url(classified)

    # 4) Réduction forte du volume (max 15/catégorie)
    selected = reduce_volume_per_category(classified, per_category_max=15)

    # 5) Déduplication SQLite + persistance (sur le set retenu)
    new_items = persist_new(DB_PATH, selected)

    # 6) Synthèse: UNIQUEMENT sur les articles filtrés/sélectionnés (pas de recyclage DB)
    reports = summarize_all(CATEGORIES, selected)

    md = render_markdown([r.markdown for r in reports])
    OUT_MD.write_text(md, encoding="utf-8")

    logging.info("Veille générée: %s", OUT_MD)
    logging.info("Nouveaux items (vs historique): %d", len(new_items))

    # Affichage console clair (résumé counts)
    by_cat = {c: 0 for c in CATEGORIES}
    for a in selected:
        by_cat[a.category] = by_cat.get(a.category, 0) + 1

    print("\n=== Veille (7 derniers jours) ===")
    for c in CATEGORIES:
        print(f"- {c}: {by_cat.get(c, 0)} article(s)")
    print(f"- Total: {len(selected)} article(s)")
    print(f"- Sortie: {OUT_MD}")

    if args.pdf:
        try_build_pdf(OUT_MD, OUT_PDF)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
