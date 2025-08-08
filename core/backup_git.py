import os
import shutil
from datetime import datetime
import subprocess

# Backup-Quelle: Ordner mit Backup-Dateien
BACKUP_SOURCE_DIR = "data/backups"

# Backup-Ziel: Git-Backup-Repo-Ordner
BACKUP_REPO_DIR = "data/server-backup"


def run_cmd(cmd, cwd=BACKUP_REPO_DIR):
    result = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Command failed: {cmd}\nError: {result.stderr}")
    else:
        print(f"Command succeeded: {cmd}\nOutput: {result.stdout}")
    return result


def backup_files():
    if not os.path.exists(BACKUP_REPO_DIR):
        os.makedirs(BACKUP_REPO_DIR)

    # Kopiere alle Backup-Dateien aus data/backups ins Git-Backup-Verzeichnis
    for filename in os.listdir(BACKUP_SOURCE_DIR):
        src_path = os.path.join(BACKUP_SOURCE_DIR, filename)
        dst_path = os.path.join(BACKUP_REPO_DIR, filename)
        shutil.copy2(src_path, dst_path)
        print(f"Copied {src_path} to {dst_path}")

    # Git Befehle: add, commit, push
    run_cmd("git add .")
    commit_msg = f"Backup {datetime.utcnow().isoformat()}"
    commit_result = run_cmd(f'git commit -m "{commit_msg}"')

    if "nothing to commit" not in commit_result.stdout.lower():
        run_cmd("git push origin main")
    else:
        print("No changes to commit, skipping push.")


if __name__ == "__main__":
    backup_files()
