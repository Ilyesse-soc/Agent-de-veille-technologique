"""Mini serveur local pour l’UI (FastAPI).

- Sert index.html sur `/`
- Expose:
  - POST /api/run : lance le pipeline existant (réutilise les fonctions)
  - GET  /api/open : ouvre veille.md (Windows)

Contrainte: ne pas casser le pipeline existant. On réutilise les mêmes fonctions
(collect/filter/dedupe/reduce/summarize) et on génère output/veille.md.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response


# PYTHONPATH: rendre importable `tech_watch_agent/` et `agents/`
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TECH_WATCH_ROOT = PROJECT_ROOT / "tech_watch_agent"
for p in (str(PROJECT_ROOT), str(TECH_WATCH_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


from config.sources import CATEGORIES, SOURCES  # noqa: E402
from agents.collector import Article, collect_from_source_detailed  # noqa: E402
from agents.collector_usine_digitale import collect_usine_digitale  # noqa: E402
from agents.filter import (  # noqa: E402
    dedupe_by_url,
    drop_empty_content,
    filter_last_7_days,
    load_recent_articles,
    persist_new,
    reduce_volume_per_category,
)
from agents.classifier import classify_all  # noqa: E402
from agents.summarizer import summarize_all  # noqa: E402
from agents.qa import answer_question  # noqa: E402


ROOT = TECH_WATCH_ROOT
INDEX_PATH = ROOT / "ui" / "index.html"
OUT_MD = ROOT / "output" / "veille.md"
DB_PATH = str(ROOT / "storage" / "history.db")


app = FastAPI(title="Tech Watch UI", version="1.0")


@dataclass(frozen=True)
class SourceStatus:
    name: str
    url: str
    ok: bool
    items: int
    used_fallback_html: bool = False
    error: Optional[str] = None


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def run_pipeline_return_payload(mode: str = "fast") -> dict[str, Any]:
    """Run pipeline et renvoie un payload JSON (stats + articles + chemin md)."""

    t0 = time.perf_counter()

    os.makedirs(ROOT / "output", exist_ok=True)
    os.makedirs(ROOT / "storage", exist_ok=True)

    mode = (mode or "fast").strip().lower()

    # UX cible 5s: par défaut, on reconstruit depuis SQLite (pas de réseau)
    if mode not in {"fast", "full"}:
        mode = "fast"

    statuses: list[SourceStatus] = []

    if mode == "full":
        # Performance/UX: timeouts stricts (6–10s) + retries max 1 (collector.py)
        timeout_s = 7
        collected: list[Article] = []

        for src in SOURCES:
            result = collect_from_source_detailed(src, timeout_s=timeout_s)
            statuses.append(
                SourceStatus(
                    name=src.name,
                    url=src.url,
                    ok=bool(result.ok),
                    items=len(result.articles),
                    used_fallback_html=bool(result.used_fallback_html),
                    error=result.error,
                )
            )
            collected.extend(result.articles)

        try:
            collected.extend(collect_usine_digitale(days=7, per_section_limit=6, timeout_s=timeout_s))
        except Exception as exc:
            statuses.append(
                SourceStatus(
                    name="Usine Digitale",
                    url="https://www.usine-digitale.fr",
                    ok=False,
                    items=0,
                    used_fallback_html=True,
                    error=str(exc),
                )
            )

        last_week = filter_last_7_days(collected)
        # Dédup SQLite inter-run (inchangé)
        # (on persist après sélection)
    else:
        # fast: charge depuis cache SQLite et reconstruit la veille
        last_week = load_recent_articles(DB_PATH, days=7)
        statuses.append(
            SourceStatus(
                name="Cache SQLite",
                url="",
                ok=True,
                items=len(last_week),
                used_fallback_html=False,
                error=None,
            )
        )

    # Classification + contenu non vide
    classified = classify_all(last_week)
    classified = drop_empty_content(classified, min_len=1)

    # Dédup intra-run strictement par URL
    after_dedup = dedupe_by_url(classified)

    # Réduction volume (max 15/catégorie)
    selected = reduce_volume_per_category(after_dedup, per_category_max=15)

    # Dédup SQLite inter-run (inchangé)
    # En mode fast, ça ne change rien mais reste safe.
    persist_new(DB_PATH, selected)

    # UX/perf: mode fast = pas d'appel LLM
    if mode == "fast":
        os.environ["TECH_WATCH_DISABLE_OLLAMA"] = "1"

    # Génération du markdown (réutilise la fonction de main.py si possible)
    try:
        import main as main_mod  # type: ignore

        reports = summarize_all(CATEGORIES, selected)
        md = main_mod.render_markdown([r.markdown for r in reports])
    except Exception:
        # Fallback minimal: concat des sections
        reports = summarize_all(CATEGORIES, selected)
        md = "\n\n".join([r.markdown for r in reports])

    OUT_MD.write_text(md, encoding="utf-8")

    elapsed = time.perf_counter() - t0

    # Stats
    sources_ok = sum(1 for s in statuses if s.ok)
    sources_error = sum(1 for s in statuses if not s.ok)

    payload = {
        "stats": {
            "sources_ok": sources_ok,
            "sources_error": sources_error,
            "articles_collected_7d": len(last_week),
            "articles_after_dedup": len(after_dedup),
            "total_final": len(selected),
            "elapsed_s": elapsed,
        },
        "sources": [asdict(s) for s in statuses],
        # Ne pas exposer de chemin local (OPSEC). On garde un identifiant neutre.
        "veille_md": "veille.md",
        "articles": [
            {
                "title": a.title,
                "published_at": _iso(a.published_at),
                "content": a.content,
                "category": a.category,
                "source": a.source,
                "url": a.url,
            }
            for a in sorted(selected, key=lambda x: x.published_at, reverse=True)
        ],
    }

    return payload


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(INDEX_PATH.read_text(encoding="utf-8"))


@app.post("/api/run")
def api_run(mode: str = Query(default="fast", description="fast|full")) -> JSONResponse:
    payload = run_pipeline_return_payload(mode=mode)
    return JSONResponse(payload)


@app.get("/api/open")
def api_open() -> JSONResponse:
    if not OUT_MD.exists():
        return JSONResponse({"ok": False, "error": "veille.md introuvable"}, status_code=404)
    try:
        os.startfile(str(OUT_MD))  # Windows
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.post("/api/ask")
def api_ask(body: dict[str, Any]) -> JSONResponse:
    question = str((body or {}).get("question") or "").strip()
    if not question:
        return JSONResponse({"ok": False, "error": "Question vide"}, status_code=400)

    try:
        payload = answer_question(DB_PATH, question=question, max_sources=8)
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@app.get("/api/pdf")
def api_pdf(request: Request) -> Response:
    """Génère un PDF de l’interface et le renvoie en téléchargement.

    Implémentation: rendu headless Chromium via Playwright.
    Fallback côté client possible (window.print) si Playwright n’est pas installé.
    """

    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": "Génération PDF indisponible (Playwright non installé).",
                "details": str(exc),
            },
            status_code=501,
        )

    base = str(request.base_url).rstrip("/")
    target_url = f"{base}/"

    pdf_bytes: bytes
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        page.goto(target_url, wait_until="networkidle", timeout=20_000)
        pdf_bytes = page.pdf(
            format="A4",
            print_background=True,
            margin={"top": "12mm", "bottom": "12mm", "left": "12mm", "right": "12mm"},
        )
        browser.close()

    stamp = datetime.now().strftime("%Y-%m-%d")
    filename = f"veille-interface-{stamp}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
