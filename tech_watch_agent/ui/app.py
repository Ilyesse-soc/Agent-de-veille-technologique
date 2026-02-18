"""Interface Streamlit locale.

Contraintes:
- Ne modifie pas le pipeline existant (main.py).
- Appelle le pipeline via une fonction Python (pas de subprocess).
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Fix PYTHONPATH (Streamlit exécute depuis un autre CWD)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 1) Ajout explicite de la racine (demandé)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
# 2) Ajout du dossier `tech_watch_agent/` pour résoudre `import agents.*`
TECH_WATCH_ROOT = PROJECT_ROOT / "tech_watch_agent"
if str(TECH_WATCH_ROOT) not in sys.path:
    sys.path.insert(0, str(TECH_WATCH_ROOT))


st = importlib.import_module("streamlit")


ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "main.py"
MD_PATH = ROOT / "output" / "veille.md"


@dataclass(frozen=True)
class RunSummary:
    cybers: int = 0
    bigdata: int = 0
    ia: int = 0
    cloud: int = 0
    total: int = 0
    raw_log: str = ""


def _load_main_module() -> object:
    spec = importlib.util.spec_from_file_location("tech_watch_agent_main", MAIN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Impossible de charger {MAIN_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parse_summary(log_text: str) -> RunSummary:
    # Attend les lignes imprimées par main.py:
    # - Cybersécurité: 15 article(s)
    patterns = {
        "Cybersécurité": "cybers",
        "Big Data": "bigdata",
        "Intelligence Artificielle": "ia",
        "Cloud / DevOps": "cloud",
        "Total": "total",
    }

    values = {"cybers": 0, "bigdata": 0, "ia": 0, "cloud": 0, "total": 0}
    for line in (log_text or "").splitlines():
        line = line.strip()
        m = re.match(r"^-\s*(.+?):\s*(\d+)\s+article\(s\)\s*$", line)
        if not m:
            continue
        label = m.group(1)
        n = int(m.group(2))
        if label in patterns:
            values[patterns[label]] = n

    return RunSummary(**values, raw_log=log_text)


def run_pipeline() -> RunSummary:
    if not MAIN_PATH.exists():
        raise FileNotFoundError(f"main.py introuvable: {MAIN_PATH}")

    main_mod = _load_main_module()
    if not hasattr(main_mod, "main"):
        raise RuntimeError("La fonction main() est introuvable dans main.py")

    buf = io.StringIO()
    # Empêche argparse de consommer les args Streamlit
    old_argv = sys.argv[:]
    sys.argv = [str(MAIN_PATH)]
    try:
        with contextlib.redirect_stdout(buf):
            # main() renvoie un code int; en cas d'exception, Streamlit affichera l'erreur
            main_mod.main()
    finally:
        sys.argv = old_argv

    return _parse_summary(buf.getvalue())


def open_veille_md() -> None:
    if not MD_PATH.exists():
        st.warning("Le fichier veille.md n’existe pas encore.")
        return
    try:
        os.startfile(str(MD_PATH))  # Windows
    except Exception as exc:
        st.error(f"Impossible d’ouvrir veille.md: {exc}")


st.set_page_config(page_title="Agent de veille technologique", layout="centered")
st.title("Agent de veille technologique")
st.caption("Génération locale d’une veille (7 derniers jours) via le pipeline Python existant.")

if "last_summary" not in st.session_state:
    st.session_state.last_summary = None

st.divider()

actions_left, actions_right = st.columns([3, 2])
with actions_left:
    generate_clicked = st.button("Générer la veille", type="primary", use_container_width=True)
with actions_right:
    st.button("Ouvrir le fichier veille.md", on_click=open_veille_md, use_container_width=True)

if generate_clicked:
    with st.spinner("Génération en cours…"):
        try:
            summary = run_pipeline()
            st.session_state.last_summary = summary
            st.success("Veille générée.")
        except Exception as exc:
            st.session_state.last_summary = None
            st.error(f"Échec du pipeline: {exc}")

summary: Optional[RunSummary] = st.session_state.last_summary
if summary:
    st.subheader("Résumé du dernier run")
    a, b, c, d, e = st.columns(5)
    a.metric("Cybersécurité", summary.cybers)
    b.metric("Big Data", summary.bigdata)
    c.metric("IA", summary.ia)
    d.metric("Cloud", summary.cloud)
    e.metric("Total", summary.total)

    tabs = st.tabs(["Aperçu", "Log"])
    with tabs[0]:
        if MD_PATH.exists():
            st.markdown(MD_PATH.read_text(encoding="utf-8"), unsafe_allow_html=False)
        else:
            st.info("Le fichier veille.md n’est pas encore disponible.")
    with tabs[1]:
        st.text(summary.raw_log)
