# Hinweis: get_config() gibt ein Dict zurück, daher Zugriff per .get("KEY")
import logging
import os
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from config.config import get_config
import json
from datetime import datetime
import threading

# Logger unterstützt jetzt dynamische Level (LOG_LEVEL) und Telegram-Error-Handler

# Load environment variables
load_dotenv()
TICKER_LOG_LEVEL = get_config("LOG_TICKER_LEVEL", "INFO").upper()
LOG_TICKER_ENABLED = get_config("LOG_TICKER_ENABLED", "True") == "True"
LOG_LEVEL = get_config("LOG_LEVEL", "INFO").upper()

# Additional block log configuration
LOG_RSI_ENABLED = get_config("LOG_RSI_ENABLED", "True") == "True"
LOG_TREND_ENABLED = get_config("LOG_TREND_ENABLED", "True") == "True"
LOG_POSITION_ENABLED = get_config("LOG_POSITION_ENABLED", "True") == "True"

LOG_TO_TELEGRAM = get_config("LOG_TO_TELEGRAM", "False") == "True"

# Unterstützung für SILENT_MODE (unterdrückt Info- und Trade-Logs)
SILENT_MODE = get_config("SILENT_MODE", "False") == "True"

LOG_FILE = "data/logs/bot.log"
ERROR_LOG_FILE = "data/logs/error.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
os.makedirs(os.path.dirname(ERROR_LOG_FILE), exist_ok=True)

LOG_TO_CONSOLE = get_config("LOG_TO_CONSOLE", "True") == "True"
LOG_TO_FILE = get_config("LOG_TO_FILE", "True") == "True"
LOG_TRADES = get_config("LOG_TRADES", "True") == "True"

# Basis-Logger-Konfiguration
logger = logging.getLogger("trading_bot")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

# Verhindere doppelte Handler-Initialisierung
if logger.hasHandlers():
    logger.handlers.clear()

# Formatter
formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")


# Utility-Funktion: Logger mit Namen aufsetzen
def setup_logger(name: str) -> logging.Logger:
    """
    Erstellt einen benannten Logger mit Standard-Formatter und optionalem File/Console-Handler.
    """
    custom_logger = logging.getLogger(name)
    if not custom_logger.hasHandlers():
        custom_logger.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        handler.setFormatter(formatter)
        custom_logger.addHandler(handler)
    return custom_logger

def log_error(message: str):
    logger.error(message)

# Konsole
if LOG_TO_CONSOLE:
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

#
# Datei
if LOG_TO_FILE:
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=3)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    error_handler = RotatingFileHandler(ERROR_LOG_FILE, maxBytes=5_000_000, backupCount=3)
    error_handler.setFormatter(formatter)
    error_handler.setLevel(logging.ERROR)
    logger.addHandler(error_handler)

if LOG_TO_TELEGRAM:
    from core.telegram_logger import TelegramHandler
    telegram_handler = TelegramHandler()
    telegram_handler.setLevel(logging.ERROR)
    telegram_handler.setFormatter(formatter)
    logger.addHandler(telegram_handler)



def log_trade_to_json(order):
    """
    Speichert einen Trade als JSON-Zeile in data/order_history.json.
    """
    if not order:
        return

    from config.config import MODE
    json_log_path = "data/order_history.json"
    os.makedirs(os.path.dirname(json_log_path), exist_ok=True)

    # Duplikatprüfung: nicht erneut speichern, wenn identische Order bereits existiert
    if os.path.exists(json_log_path):
        with open(json_log_path, "r") as f:
            if any(order.get("info") and order.get("info") in line for line in f):
                return

    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "pair": order.get("symbol"),
        "side": order.get("side"),
        "price": order.get("price"),
        "quantity": order.get("quantity"),
        "type": order.get("type"),
        "mode": MODE,
        "info": order.get("info", ""),
    }

    with open(json_log_path, "a") as f:
        json.dump(entry, f)
        f.write("\n")


# Loggt eine Info-Nachricht
def log_info(message):
    if not SILENT_MODE:
        logger.info(message)

import time
from collections import defaultdict

_last_price_log_time = 0
_live_prices = defaultdict(str)
_logged_prices = defaultdict(str)

def log_price(symbol, price):
    global _last_price_log_time
    if not LOG_TICKER_ENABLED:
        return
    _live_prices[symbol] = price

    now = time.time()
    if TICKER_LOG_LEVEL == "DEBUG":
        if now - _last_price_log_time >= 5:
            price_string = " | ".join(f"{sym}: {_live_prices[sym]}" for sym in sorted(_live_prices.keys()))
            log_func = getattr(logger, TICKER_LOG_LEVEL.lower(), logger.info)
            log_func(f"📈 Live-Prices (5s): {price_string}")
            _last_price_log_time = now

import threading

class TickerLogger:
    def __init__(self, interval=5):
        self.interval = interval
        self.ticker_data = {}
        self.last_log_time = time.time()
        self.lock = threading.Lock()

    def log(self, symbol, price):
        with self.lock:
            self.ticker_data[symbol] = price
            now = time.time()
            if now - self.last_log_time >= self.interval:
                self._flush()
                self.last_log_time = now

    def _flush(self):
        if not LOG_TICKER_ENABLED:
            return
        if self.ticker_data:
            tickers = " | ".join([f"{s}: {p}" for s, p in self.ticker_data.items()])
            log_func = getattr(logger, TICKER_LOG_LEVEL.lower(), logger.info)
            log_func(f"📈 Live-Ticker: {tickers}")
            self.ticker_data.clear()

ticker_logger = TickerLogger()


log = logger

# Logging-Aktivität bestätigen und Propagation deaktivieren
logger.propagate = False
logger.info("✅ Logger erfolgreich initialisiert.")

# Utility-Funktion zum sauberen Loggen von Warnungen aus anderen Modulen
def log_warning(message: str):
    logger.warning(message)

# Ergänzung: log_debug-Funktion
def log_debug(message: str):
    if not SILENT_MODE:
        logger.debug(message)

_last_logged = {}

def log_with_interval(key: str, message: str, level=logging.INFO, interval=60):
    """
    Loggt eine Nachricht nur, wenn der letzte Log länger als 'interval' Sekunden her ist.
    """
    import time
    now = time.time()
    last = _last_logged.get(key, 0)
    if now - last >= interval:
        logger.log(level, message)
        _last_logged[key] = now