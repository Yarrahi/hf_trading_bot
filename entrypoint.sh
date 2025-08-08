#!/bin/bash

# Arbeitsverzeichnisse
REPO_DIR="/app"
BACKUP_DIR="/app/data/server-backup"

# Git Remote URL aus Umgebungsvariable
GIT_REMOTE_URL="${BACKUP_GIT_REMOTE}"

# Prüfe, ob git installiert ist
if ! command -v git &> /dev/null
then
    echo "Warnung: git ist nicht installiert. Git-Befehle werden übersprungen."
else
    # Backup-Verzeichnis erstellen, falls nicht vorhanden
    mkdir -p "$BACKUP_DIR"
    cd "$BACKUP_DIR"

    if [ ! -d ".git" ]; then
      echo "Backup-Git Repo wird initialisiert..."
      git init
      git remote add origin "$GIT_REMOTE_URL"
      git fetch origin
      git checkout -b main origin/main || git checkout -b main
      if [ -z "$(ls -A)" ]; then
        touch .gitkeep
        git add .gitkeep
        git commit -m "Initial commit"
        git push -u origin main || echo "Git push konnte nicht durchgeführt werden."
      fi
    else
      echo "Backup-Git Repo bereits initialisiert."
      git remote set-url origin "$GIT_REMOTE_URL"
    fi

    if ! git rev-parse --verify main >/dev/null 2>&1; then
      echo "Kein Commit im main Branch, erstelle initialen Commit..."
      touch .gitkeep
      git add .gitkeep
      git commit -m "Initial commit"
      git push -u origin main || echo "Git push konnte nicht durchgeführt werden."
    fi

    git reset --hard
    git clean -fd
    git pull --rebase origin main || echo "Git pull konnte nicht durchgeführt werden."
fi

# Starte den Cron-Dienst und leite Logs um
service cron start
touch /var/log/cron.log
tail -F /var/log/cron.log &

# Starte den Trading-Bot im Hauptverzeichnis im Vordergrund
cd "$REPO_DIR"
exec python main.py
