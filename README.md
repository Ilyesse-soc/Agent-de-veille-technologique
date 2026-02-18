# Agent-de-veille-technologique

Agent local de veille technologique (Python) avec interface "terminal databoard".

- Collecte RSS/HTML (sources officielles)
- Filtrage strict sur les 7 derniers jours
- Déduplication (intra-run + SQLite inter-run)
- Classement: Cybersécurité / Intelligence Artificielle / Cloud / Big Data
- Sortie: `tech_watch_agent/output/veille.md`
- UI web locale: FastAPI + page HTML/CSS/JS (sans libs)

## Prérequis

- Windows
- Python 3.10+ (recommandé: 3.12)

## Installation (local)

Depuis le dossier du projet:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r tech_watch_agent\requirements.txt
```

## Lancer l’interface (UI)

Option 1 (simple):

```powershell
powershell -ExecutionPolicy Bypass -File "./launch_ui.ps1"
```

- L’UI démarre sur `http://127.0.0.1:8501`
- Clique **Générer la veille**
- Le fichier est généré ici: `tech_watch_agent/output/veille.md`

## Créer un raccourci sur le bureau (Windows)

1) Clic droit sur le Bureau → **Nouveau** → **Raccourci**
2) Dans **Emplacement de l’élément**, mets **exactement** (en adaptant le chemin du projet):

```
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -ExecutionPolicy Bypass -File "C:\Le\bon\chemin\du\projet\agent-veille\launch_ui.ps1"
```

3) Donne un nom au raccourci (ex: `Agent de veille (UI)`) → Terminer

Notes:
- Remplace `C:\Le\bon\chemin\du\projet\agent-veille\` par TON chemin réel.
- Si ton projet est dans OneDrive/Desktop, un exemple typique:

```
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -ExecutionPolicy Bypass -File "C:\Users\ilyes\OneDrive\Desktop\agent-veille\launch_ui.ps1"
```

## Lancer en ligne de commande (sans UI)

```powershell
.\.venv\Scripts\Activate.ps1
python tech_watch_agent\main.py
```

## Publier sur GitHub (pousser le projet dans ton repo)

Dans PowerShell, depuis le dossier du projet `agent-veille`:

```powershell
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/Ilyesse-soc/Agent-de-veille-technologique.git
git push -u origin main
```

Si `git push` te demande une authentification:
- utilise GitHub CLI (`gh auth login`) ou un **Personal Access Token** (PAT) si nécessaire.
