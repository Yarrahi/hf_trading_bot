def get_dynamic_position_size(pair: str, risk_percent: float = 1.0, min_position: float = 1.0) -> float:
    """
    Berechnet eine dynamische Positionsgröße basierend auf verfügbarem Kapital und ATR.
    Stellt sicher, dass die Positionsgröße nicht 0 ist und mindestens `min_position` erreicht.
    """
    try:
        from strategies.atr import get_atr
        from core.kucoin_api import get_price

        base, quote = pair.split("-")
        available_capital = wallet_instance.get_balance(quote)
        current_price = get_price(pair)
        atr_value = get_atr(pair)

        if not available_capital or not current_price or not atr_value:
            log_info(f"⚠️ Ungültige Werte für dynamische Positionsberechnung: Kapital={available_capital}, Preis={current_price}, ATR={atr_value}")
            return 0.0

        risk_amount = available_capital * (risk_percent / 100)
        position_size = risk_amount / (atr_value if atr_value > 0 else current_price)

        # Mindestgröße sicherstellen
        position_size = max(position_size, min_position)
        return round(position_size, 6)
    except Exception as e:
        log_info(f"❌ Fehler bei der dynamischen Positionsgrößenberechnung: {e}")
        return 0.0
# --- BEGIN WALLET LIVE CODE ---
from core.kucoin_api import KuCoinClientWrapper, get_live_account_balances
from core.logger import log_info, log_debug
import time
import os

# Logging-Flag für Wallet-Übersicht
LOG_WALLET_OVERVIEW = os.getenv("LOG_WALLET_OVERVIEW", "True") == "True"

class Wallet:
    def __init__(self):
        self._account_cache = None
        self._cache_timestamp = 0
        self._cache_ttl = 10  # Cachezeit in Sekunden, z.B. 10 Sekunden
        self.api = KuCoinClientWrapper()

    def _get_accounts(self):
        now = time.time()
        if self._account_cache and (now - self._cache_timestamp) < self._cache_ttl:
            return self._account_cache
        try:
            accounts = get_live_account_balances()
            # Falls eine Liste zurückgegeben wird, in ein Dict konvertieren
            if isinstance(accounts, list):
                accounts = {entry.get('currency', 'UNKNOWN'): {
                    "available": float(entry.get('available', 0)),
                    "hold": float(entry.get('holds', 0)),
                    "balance": float(entry.get('balance', 0))
                } for entry in accounts}

            self._account_cache = accounts
            if LOG_WALLET_OVERVIEW:
                log_debug("📋 API-Kontenübersicht:")
                for currency, data in accounts.items():
                    log_debug(f"🔹 {currency}: {data['available']} verfügbar, {data['hold']} reserviert, Gesamt: {data['balance']}")
            self._cache_timestamp = now
            return accounts
        except Exception as e:
            log_info(f"❌ Fehler beim Abrufen der Kontenübersicht: {e}")
            return {}

    def get_balance(self, symbol="USDT"):
        try:
            accounts = self._get_accounts()
            data = accounts.get(symbol)
            if data:
                return float(data.get("available", 0))
            log_info(f"❌ Kein Eintrag für Währung {symbol} gefunden.")
            return 0.0
        except Exception as e:
            log_info(f"❌ Fehler beim Abrufen der Balance für {symbol}: {e}")
            return 0.0

    def load_balance(self):
        from config.config import get_pair_list
        pair = get_pair_list()[0]
        base, quote = pair.split("-")
        return {
            base: round(self.get_balance(base), 8),
            quote: round(self.get_balance(quote), 8)
        }

    def get_available_balance(self, coin: str) -> float:
        try:
            accounts = self._get_accounts()
            data = accounts.get(coin)
            return float(data.get("available", 0)) if data else 0.0
        except Exception as e:
            log_info(f"❌ Fehler beim Abrufen des verfügbaren Guthabens für {coin}: {e}")
            return 0.0
# --- END WALLET LIVE CODE ---

from config.config import get_env_var
from core.telegram_utils import send_telegram_message

wallet_instance = Wallet()

def calculate_position_size(pair: str, percent: float = 5.0):
    """
    Berechnet eine einfache Positionsgröße basierend auf Prozent des verfügbaren Kapitals und dem aktuellen Marktpreis.
    Nutzt die Preisfunktion aus core.kucoin_api für ein einheitliches Verhalten.
    """
    wallet = wallet_instance
    base, quote = pair.split("-")
    balance_quote = wallet.get_balance(quote)
    if balance_quote is None:
        return None

    from core.kucoin_api import get_price  # aktualisiert
    current_price = get_price(pair)
    if not current_price:
        return None

    quote_amount = balance_quote * (percent / 100)
    quantity = quote_amount / current_price
    return round(quantity, 6)

def notify_live_balance(balances=None):
    from config.config import get_pair_list

    wallet = wallet_instance
    for pair in get_pair_list():
        base, quote = pair.split("-")
        balance_base = wallet.get_balance(base) or 0.0
        balance_quote = wallet.get_balance(quote) or 0.0

        msg = f"💼 Live-Balance für {pair}:\n" \
              f"• {base}: {balance_base:.6f}\n" \
              f"• {quote}: {balance_quote:.2f}"

        if get_env_var("LOG_TO_CONSOLE", "True") == "True":
            from core.logger import log_info
            log_info(msg)
        if get_env_var("LOG_TO_TELEGRAM", "True") == "True":
            send_telegram_message(msg, to_private=True, to_channel=False)

def safe_update_balance(asset: str, amount: float, operation: str = "subtract"):
    if hasattr(wallet_instance, "update_balance"):
        wallet_instance.update_balance(asset, amount, operation)

def get_live_balance(asset_or_pair: str) -> dict:
    try:
        if "-" not in asset_or_pair:
            balance = wallet_instance.get_balance(asset_or_pair.upper())
            if not isinstance(balance, (int, float)):
                from core.logger import log_error
                log_error(f"⚠️ Balance für {asset_or_pair} hat ungültigen Typ: {type(balance)}")
                return {}
            return {"available": balance}

        base, quote = asset_or_pair.upper().split("-")
        balance_base = wallet_instance.get_balance(base)
        balance_quote = wallet_instance.get_balance(quote)

        if not isinstance(balance_base, (int, float)):
            from core.logger import log_error
            log_error(f"⚠️ Balance für {base} hat ungültigen Typ: {type(balance_base)}")
            balance_base = None
        if not isinstance(balance_quote, (int, float)):
            from core.logger import log_error
            log_error(f"⚠️ Balance für {quote} hat ungültigen Typ: {type(balance_quote)}")
            balance_quote = None

        if balance_base is None:
            from core.logger import log_error
            log_error(f"⚠️ Konnte Balance für {base} nicht abrufen.")
        if balance_quote is None:
            from core.logger import log_error
            log_error(f"⚠️ Konnte Balance für {quote} nicht abrufen.")

        if balance_base is None or balance_quote is None:
            return {}

        return {base: balance_base, quote: balance_quote}
    except Exception as e:
        from core.logger import log_error
        log_error(f"❌ Fehler in get_live_balance(): {e}")
        return {}

__all__ = [
    "Wallet", "wallet_instance", "safe_update_balance",
    "calculate_position_size", "notify_live_balance",
    "get_live_balance", "get_dynamic_position_size"
]
