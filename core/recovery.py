# Backup-Datei täglich (mit Datum im Namen, überschreibt an jedem Tag)
def backup_file_daily(source_file: str, backup_dir: str = "data/backups/"):
    """
    Erstellt ein tägliches Backup der Datei im Backup-Ordner mit Datum im Dateinamen.
    Überschreibt die Datei des aktuellen Tages.
    """
    import shutil
    import os
    from datetime import datetime

    os.makedirs(backup_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    filename = os.path.basename(source_file)
    backup_name = f"{os.path.splitext(filename)[0]}_{date_str}.json"
    backup_path = os.path.join(backup_dir, backup_name)
    try:
        shutil.copy(source_file, backup_path)
        print(f"✅ Backup erstellt: {backup_path}")
    except Exception as e:
        print(f"❌ Fehler beim Backup von {source_file}: {e}")
import shutil
from datetime import datetime
from core.kucoin_api import get_open_positions  # muss implementiert sein
from core.utils import save_json_file
import os
import json
from dotenv import load_dotenv
from config.config import LIVE_POSITIONS_FILE
from core.telegram_utils import send_safe_message

load_dotenv()

TRADES_FILE = os.getenv("ORDER_HISTORY_FILE", "data/order_history.json")


# Backup-Funktion für eine Datei mit Zeitstempel
def backup_file(file_path: str, backup_dir: str = "data/backups/"):
    """
    Erstellt eine zeitgestempelte Kopie einer Datei für Recovery-Zwecke.
    """
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(backup_dir, f"{os.path.basename(file_path)}_{timestamp}.bak")
    try:
        shutil.copy(file_path, backup_path)
        return backup_path
    except Exception as e:
        print(f"❌ Fehler beim Backup von {file_path}: {e}")
        return None


# Positionen wiederherstellen (LIVE oder aus Backup)
def restore_positions(mode: str = "LIVE") -> dict:
    """
    Lädt aktuelle Positionen aus Backup oder KuCoin (für LIVE).
    Im LIVE-Modus werden nur vom Bot eröffnete Positionen berücksichtigt.
    Gibt immer ein Dict zurück, um `.items()`-Fehler zu vermeiden.
    """
    try:
        if mode.upper() == "LIVE":
            # Versuche, offene Positionen von KuCoin zu laden
            positions_data = get_open_positions()
            if positions_data:
                return positions_data
            else:
                # Fallback: Lade Positionen aus Datei
                data = load_json_file(LIVE_POSITIONS_FILE)
                # Falls Liste, in Dict umwandeln
                if isinstance(data, list):
                    data = {f"pos_{i}": p for i, p in enumerate(data) if isinstance(p, dict)}
                return data
        else:
            data = load_json_file(LIVE_POSITIONS_FILE)
            # Falls Liste, in Dict umwandeln
            if isinstance(data, list):
                data = {f"pos_{i}": p for i, p in enumerate(data) if isinstance(p, dict)}
            return data
    except Exception as e:
        print(f"❌ Fehler beim Laden von Live-Positionen: {e}")
        return {}

# Trade-Log wiederherstellen
def restore_trades() -> dict:
    """
    Lädt das Trade-Log für Recovery.
    Gibt immer ein Dict zurück (Key = Trade-ID), um konsistente Verarbeitung zu gewährleisten.
    """
    trades = load_json_file(TRADES_FILE)
    if isinstance(trades, list):
        trades = {str(trade.get("id", f"trade_{i}")): trade for i, trade in enumerate(trades) if isinstance(trade, dict)}
    return trades if isinstance(trades, dict) else {}

# Automatisches Backup nach jedem Trade
def auto_backup():
    """
    Erstellt tägliche Backups von Trades und Positionen (überschreibt täglich).
    """
    backup_file_daily(LIVE_POSITIONS_FILE)  # für positions_live.json jetzt daily backup
    backup_file_daily(TRADES_FILE)          # für order_history.json daily backup (bleibt so)


def load_json_file(filepath: str) -> dict:
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Fehler beim Laden von {filepath}: {e}")
        return {}

def validate_trade_log() -> bool:
    trades = restore_trades()
    if isinstance(trades, dict):
        return all("pair" in trade for trade in trades.values())
    return False


def validate_positions() -> bool:
    positions = restore_positions()
    if isinstance(positions, dict):
        return all("entry_price" in val and "amount" in val for val in positions.values())
    return False


def run_recovery_check():
    valid_trades = validate_trade_log()
    valid_positions = validate_positions()

    if not valid_trades:
        print("⚠️ Trade-Log ist ungültig oder beschädigt.")
    if not valid_positions:
        print("⚠️ Positionsdaten sind ungültig oder beschädigt.")

    if valid_trades and valid_positions:
        print("✅ Recovery-Check bestanden.")

def check_backup_health(days: int = 1) -> bool:
    """
    Prüft, ob in den letzten X Tagen ein Backup in data/backups/ existiert.
    """
    backup_dir = "data/backups/"
    if not os.path.exists(backup_dir):
        return False
    cutoff = datetime.now().timestamp() - (days * 86400)
    for f in os.listdir(backup_dir):
        path = os.path.join(backup_dir, f)
        if os.path.isfile(path) and os.path.getmtime(path) >= cutoff:
            return True
    return False

def send_backup_warning_if_needed():
    """
    Sendet eine Telegram-Warnung, wenn kein aktuelles Backup vorhanden ist.
    """
    if not check_backup_health():
        warning_msg = "⚠️ Backup-Warnung: Es wurde in den letzten 24 Stunden kein Backup erstellt!"
        send_safe_message(message=warning_msg, to_private=True, to_channel=False, parse_mode=None)
        send_safe_message(message=warning_msg, to_private=False, to_channel=True, parse_mode=None)
def save_account_overview(balances, file_path: str = "data/account_overview.json"):
    """
    Speichert die vollständige Kontoübersicht (z. B. für Reporting).
    Stellt sicher, dass die Balances als Dict gespeichert werden.
    """
    try:
        if isinstance(balances, list):
            balances = {entry.get('currency', 'UNKNOWN'): {
                "available": float(entry.get('available', 0)),
                "hold": float(entry.get('holds', 0)),
                "balance": float(entry.get('balance', 0))
            } for entry in balances}
        save_json_file(file_path, balances)
    except Exception as e:
        print(f"❌ Fehler beim Speichern der Kontoübersicht: {e}")