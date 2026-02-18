cd C:\Users\ilyes\OneDrive\Desktop\agent-veille
.\.venv\Scripts\Activate.ps1

# Lance le serveur UI
$proc = Start-Process -FilePath "uvicorn" -ArgumentList "tech_watch_agent.ui.server:app --host 127.0.0.1 --port 8501" -PassThru
Start-Sleep -Seconds 1
Start-Process "http://127.0.0.1:8501"

# Garde la fenêtre ouverte tant que le serveur tourne
Wait-Process -Id $proc.Id
