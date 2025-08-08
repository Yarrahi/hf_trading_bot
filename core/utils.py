import os
import logging
import json
from typing import Optional

def get_env_variable(key: str, default=None, cast_type=str):
    """Liest eine Umgebungsvariable und castet sie in den gewünschten Typ."""
    value = os.getenv(key, default)
    try:
        return cast_type(value)
    except (ValueError, TypeError):
        logging.warning(f"⚠️ Konnte Umgebungsvariable {key} nicht in {cast_type.__name__} umwandeln.")
        return default

def format_symbol(symbol: str) -> str:
    """Normalisiert KuCoin-Symbole (z.B. btc-usdt → BTC-USDT)."""
    return symbol.upper()

def format_quantity(qty: float, decimals: int = 6) -> str:
    """Formatiert eine float-Menge als string mit bestimmter Genauigkeit."""
    return f"{qty:.{decimals}f}"

price_cache = {}  # Simpler global cache für zuletzt bekannte Preise pro Symbol

def update_price_cache(symbol: str, price: float):
    price_cache[symbol] = price

def get_cached_price(symbol: str) -> Optional[float]:
    return price_cache.get(symbol)

def ensure_directory(path: str):
    """Erstellt ein Verzeichnis, falls es nicht existiert."""
    if not os.path.exists(path):
        os.makedirs(path)

import json

def load_json_file(filepath, default=None):
    if not os.path.exists(filepath):
        return default if default is not None else {}
    with open(filepath, 'r') as f:
        return json.load(f)

def load_json_dict_file(filepath):
    """Lädt ein JSON-Dictionary aus Datei. Gibt leeres Dict zurück, wenn Datei fehlt oder ungültig ist."""
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def append_to_order_history(order_data: dict, file_path: str = "data/order_history.json", fee: float = None):
    """Hängt einen einzelnen Order-Eintrag an order_history.json an, inkl. Timestamp und optional berechnetem Fee."""
    from datetime import datetime

    ensure_directory(os.path.dirname(file_path))
    history = load_json_file(file_path, default=[])
    if not isinstance(history, list):
        history = []

    # Fee automatisch berechnen, falls nicht übergeben und Infos vorhanden
    if fee is None:
        if 'filled_amount' in order_data and 'taker_fee_rate' in order_data:
            try:
                fee = float(order_data['filled_amount']) * float(order_data['taker_fee_rate'])
            except (ValueError, TypeError):
                fee = 0.0
        else:
            fee = 0.0

    order_data["fee"] = round(fee, 8)
    order_data["timestamp"] = datetime.utcnow().isoformat()

    # Logging bei Bedarf
    if not get_env_variable("SILENT_MODE", "False", cast_type=str).lower() == "true":
        print(f"📘 Speichere Order-History nach {file_path}: {order_data}")

    history.append(order_data)
    with open(file_path, 'w') as f:
        json.dump(history, f, indent=4)

def save_json_file(filepath: str, data: dict):
    if not isinstance(filepath, (str, bytes, os.PathLike)):
        logging.warning(f"⚠️ Ungültiger Dateipfad übergeben an save_json_file: {filepath} ({type(filepath)})")
        return  # Don't proceed if filepath is not valid

    ensure_directory(os.path.dirname(filepath))
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=4)


# --- Neue Funktion: append_to_json_file ---
def append_to_json_file(filepath, new_entry):
    data = load_json_file(filepath, default=[])

    from core.logger import log_warning, log_info

    if isinstance(data, dict):
        log_warning(f"⚠️ Erwartete Liste in {filepath}, aber erhalten: dict. Ersetze durch leere Liste.")
        data = []
    elif not isinstance(data, list):
        log_warning(f"⚠️ Unerwarteter Datentyp in {filepath}. Ersetze durch leere Liste.")
        data = []

    # Prüfe auf Duplikate anhand der ID
    new_id = new_entry.get("id")
    if new_id and any(entry.get("id") == new_id for entry in data):
        log_info(f"⚠️ Duplikat mit ID {new_id} in {filepath} erkannt – Eintrag wird übersprungen.")
        return

    data.append(new_entry)
    save_json_file(filepath, data)