import os
PRICE_LOG_LEVEL = os.getenv("PRICE_LOG_LEVEL", "WARNING").upper()

def clear_api_caches():
    """Invalidiert alle lru_caches (z.B. nach Neustart)."""
    try:
        KuCoinClientWrapper.get_symbol_price.cache_clear()
        KuCoinClientWrapper.get_symbol_min_order_size.cache_clear()
        KuCoinClientWrapper.get_candles.cache_clear()
        KuCoinClientWrapper.get_historical_candles.cache_clear()
        logger.info("✅ API-Caches wurden invalidiert.")
    except Exception as e:
        logger.warning(f"⚠️ Fehler beim Invalidieren der API-Caches: {e}")
import pandas as pd
from kucoin.client import Market, Trade, User as UserClient
from config.config import KUCOIN_API_KEY, KUCOIN_API_SECRET, KUCOIN_API_PASSPHRASE, KUCOIN_SANDBOX
import time
from core.logger_setup import setup_logger
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, TimeoutError
logger = setup_logger(__name__)

# Runtime Mode Resolution
from config.config import MODE as CONFIG_MODE
RUNTIME_MODE = os.getenv("MODE", CONFIG_MODE).upper()
logger.info(f"🔌 KuCoin API Wrapper gestartet im {RUNTIME_MODE}-Modus")

def safe_api_call(func, *args, retries=3, delay=1, timeout=3, **kwargs):
    """
    Führt einen API-Aufruf mit automatischen Wiederholungsversuchen (Exponential Backoff).
    Timeout und besseres Logging inkludiert.
    """
    for attempt in range(retries):
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(func, *args, **kwargs)
                return future.result(timeout=timeout)
        except TimeoutError as te:
            logger.warning(f"API-Timeout bei {func.__name__}: {te} (Versuch {attempt+1}/{retries})")
            time.sleep(delay * (2 ** attempt))
        except Exception as e:
            logger.warning(f"API-Fehler bei {func.__name__}: {e} (Versuch {attempt+1}/{retries})")
            time.sleep(delay * (2 ** attempt))
    from core.telegram_utils import send_telegram_message
    error_msg = f"❌ API-Aufruf fehlgeschlagen nach {retries} Versuchen: {func.__name__}"
    logger.error(error_msg)
    try:
        send_telegram_message(f"⚠️ {error_msg}", to_private=True, to_channel=False)
    except Exception as te:
        logger.warning(f"Telegram-Benachrichtigung fehlgeschlagen: {te}")
    raise

class KuCoinClientWrapper:
    def __init__(self):
        self.market = Market()
        self.trade = Trade(
            KUCOIN_API_KEY,
            KUCOIN_API_SECRET,
            KUCOIN_API_PASSPHRASE,
            KUCOIN_SANDBOX
        )
        self.user = UserClient(
            KUCOIN_API_KEY,
            KUCOIN_API_SECRET,
            KUCOIN_API_PASSPHRASE,
            KUCOIN_SANDBOX
        )

    def get_account_list(self):
        if RUNTIME_MODE != "LIVE":
            logger.info("ℹ️ PAPER-Mode: get_account_list übersprungen – gebe leere Liste zurück.")
            return []
        return safe_api_call(self.user.get_account_list)

    def get_open_positions(self, symbols: list = None):
        """
        Gibt eine Liste offener Positionen (Balance > 0) für die angegebenen Symbole zurück.
        """
        try:
            accounts = self.get_account_list()
            positions = []
            for acc in accounts:
                if acc['type'] == 'trade' and float(acc['balance']) > 0:
                    symbol_currency = acc['currency']
                    if symbol_currency == "USDT":
                        continue
                    if symbols and all(not s.startswith(symbol_currency) for s in symbols):
                        continue
                    amount = float(acc['balance'])
                    # Hole den aktuellen Preis (gegen USDT)
                    market_symbol = f"{symbol_currency}-USDT"
                    price = self.get_symbol_price(market_symbol)
                    positions.append({
                        "symbol": market_symbol,
                        "amount": amount,
                        "entry_price": price,
                        "current_price": price
                    })
            return positions
        except Exception as e:
            logger.info(f"⚠️ Fehler beim Abrufen offener Positionen: {e}")
            return []

    def get_account_overview(self, currency: str = None):
        """
        Ruft eine Übersicht des Spot-Kontos von KuCoin über die REST-API ab.
        Gibt ein Dict zurück (currency -> Werte).
        """
        if RUNTIME_MODE != "LIVE":
            logger.info("ℹ️ PAPER-Mode: get_account_overview übersprungen – gebe leeres Dict zurück.")
            return {}
        try:
            endpoint = "/api/v1/accounts"
            params = {"type": "trade"}
            if currency:
                params["currency"] = currency
            raw = self.user._request('GET', endpoint, params=params)

            balances = {}
            if isinstance(raw, list):
                for acc in raw:
                    balances[acc['currency']] = {
                        "available": float(acc.get('available', 0)),
                        "hold": float(acc.get('holds', 0)),
                        "balance": float(acc.get('balance', 0))
                    }
            elif isinstance(raw, dict) and 'data' in raw:
                for acc in raw['data']:
                    balances[acc['currency']] = {
                        "available": float(acc.get('available', 0)),
                        "hold": float(acc.get('holds', 0)),
                        "balance": float(acc.get('balance', 0))
                    }
            return balances
        except Exception as e:
            logger.info(f"⚠️ Fehler beim Abrufen der Kontoübersicht: {e}")
            return {}

    @lru_cache(maxsize=128)
    def get_symbol_price(self, symbol: str):
        """Returns latest market price for the trading pair with WebSocket fallback (cached)."""
        if PRICE_LOG_LEVEL == "DEBUG":
            logger.debug(f"Abrufen des aktuellen Preises für {symbol} (REST).")
        elif PRICE_LOG_LEVEL == "INFO":
            logger.info(f"Abrufen des aktuellen Preises für {symbol} (REST).")
        for _ in range(3):
            try:
                ticker = self.market.get_ticker(symbol=symbol)
                if PRICE_LOG_LEVEL == "DEBUG":
                    logger.debug(f"Preis von REST für {symbol}: {ticker['price']}")
                elif PRICE_LOG_LEVEL == "INFO":
                    logger.info(f"Preis von REST für {symbol}: {ticker['price']}")
                return float(ticker['price'])
            except Exception as e:
                if PRICE_LOG_LEVEL in ("DEBUG", "INFO"):
                    logger.info(f"⚠️ Fehler beim Abrufen des Preises für {symbol} (REST): {e}")
                # Versuche Fallback: Hole letzten bekannten WebSocket-Preis
                try:
                    from core.kucoin_api import get_last_ws_price as ws_fallback_price
                    ws_price = ws_fallback_price(symbol)
                    if ws_price:
                        if PRICE_LOG_LEVEL == "DEBUG":
                            logger.info(f"⚠️ Verwende WebSocket-Fallback-Preis für {symbol}: {ws_price}")
                        return ws_price
                except Exception as we:
                    if PRICE_LOG_LEVEL == "DEBUG":
                        logger.warning(f"⚠️ WebSocket-Fallback nicht verfügbar für {symbol}: {we}")
                time.sleep(1)
        return 0.0

    def create_market_order(self, symbol, side, size, funds=None):
        if RUNTIME_MODE != "LIVE":
            logger.info(f"ℹ️ PAPER-Mode: create_market_order({symbol}, {side}, size={size}, funds={funds}) übersprungen – kein Live-Call.")
            return {"orderId": "paper-skip", "symbol": symbol, "side": side, "size": size, "funds": funds}
        return self.trade.create_market_order(symbol=symbol, side=side, size=size, funds=funds)

    @lru_cache(maxsize=128)
    def get_symbol_min_order_size(self, symbol: str):
        try:
            symbol_data = safe_api_call(Market().get_symbol_list)
            for item in symbol_data:
                if item["symbol"] == symbol:
                    return float(item["baseMinSize"])
            return None
        except Exception as e:
            logger.info(f"⚠️ Fehler beim Abrufen der Mindestbestellmenge für {symbol}: {e}")
            return None

    def get_trade_fee(self, symbol: str):
        from functools import lru_cache

        @lru_cache(maxsize=128)
        def cached_trade_fee(symbol):
            return safe_api_call(self.trade.get_trade_fee, symbol=symbol)
        try:
            fee = cached_trade_fee(symbol)
            return float(fee['makerFeeRate']), float(fee['takerFeeRate'])
        except Exception as e:
            logger.info(f"⚠️ Fehler beim Abrufen der Handelsgebühren für {symbol}: {e}")
            return 0.0, 0.0

    def get_account_balance(self, currency: str):
        for _ in range(3):
            try:
                accounts = self.get_account_list()
                for account in accounts:
                    if account['currency'] == currency and account['type'] == 'trade':
                        return float(account['available'])
                return 0.0
            except Exception as e:
                logger.info(f"⚠️ Fehler beim Abrufen des Kontostands für {currency}: {e}")
                time.sleep(1)
        return 0.0

    def get_orders(self, symbol: str, status="active"):
        """
        Gibt offene (aktive) Orders für ein Symbol zurück.
        """
        if RUNTIME_MODE != "LIVE":
            logger.info("ℹ️ PAPER-Mode: get_orders übersprungen – gebe leere Liste zurück.")
            return []
        try:
            return safe_api_call(
                self.trade.get_order_list,
                symbol=symbol,
                status=status,
                startAt=int(time.time()) - 86400  # letzte 24 Stunden
            )
        except Exception as e:
            logger.info(f"⚠️ Fehler beim Abrufen der offenen Orders für {symbol}: {e}")
            return []


    @lru_cache(maxsize=128)
    def get_candles(self, symbol: str, interval: str = "1min", limit: int = 50) -> pd.DataFrame:
        """
        Holt OHLCV-Kerzen von KuCoin als DataFrame.
        Intervall-Beispiele: 1min, 5min, 1hour
        """
        try:
            raw = safe_api_call(self.market.get_kline, symbol=symbol, kline_type=interval)
            if not raw or len(raw) == 0:
                logger.warning(f"⚠️ Keine Candle-Daten empfangen für {symbol} (Intervall: {interval}).")
                return pd.DataFrame()

            df = pd.DataFrame(raw, columns=[
                "timestamp", "open", "close", "high", "low", "volume", "turnover"
            ])

            df["timestamp"] = pd.to_datetime(pd.to_numeric(df["timestamp"]), unit='ms')
            df = df.astype({
                "open": float,
                "close": float,
                "high": float,
                "low": float,
                "volume": float
            })

            df = df.sort_values("timestamp").reset_index(drop=True)
            return df.tail(limit).copy()
        except Exception as e:
            logger.warning(f"⚠️ Fehler beim Abrufen der Candle-Daten für {symbol}: {e}")
            return pd.DataFrame()

    @lru_cache(maxsize=32)
    def get_historical_candles(self, symbol: str, interval: str = "1min", start: int = None, end: int = None) -> pd.DataFrame:
        """
        Holt historische OHLCV-Daten in Batches (z.B. 30 Tage 1min) und gibt einen DataFrame zurück.
        start und end sind UNIX-Timestamps in Sekunden.
        """
        try:
            all_data = []
            batch_limit = 1500  # KuCoin max candles per request
            if not end:
                end = int(time.time())
            if not start:
                # Standard: 30 Tage zurück
                start = end - 30 * 24 * 60 * 60

            current_end = end
            while current_end > start:
                current_start = max(start, current_end - batch_limit * 60)  # Schrittgröße
                raw = safe_api_call(
                    self.market.get_kline,
                    symbol=symbol,
                    kline_type=interval,
                    startAt=current_start,
                    endAt=current_end
                )
                if not raw:
                    break
                all_data.extend(raw)
                current_end = current_start - 1
                time.sleep(0.2)  # Rate-Limit-Schonung

            if not all_data:
                logger.warning(f"⚠️ Keine historischen Candle-Daten für {symbol}.")
                return pd.DataFrame()

            df = pd.DataFrame(all_data, columns=[
                "timestamp", "open", "close", "high", "low", "volume", "turnover"
            ])
            df["timestamp"] = pd.to_datetime(pd.to_numeric(df["timestamp"]), unit='ms')
            df = df.astype({
                "open": float,
                "close": float,
                "high": float,
                "low": float,
                "volume": float
            })
            df = df.sort_values("timestamp").reset_index(drop=True)
            return df.copy()
        except Exception as e:
            logger.error(f"❌ Fehler beim Abrufen historischer Candles für {symbol}: {e}")
            return pd.DataFrame()

    def get_live_account_balances(self):
        """
        Gibt alle Spot-Balances in einem strukturierten Dict zurück (currency -> Werte).
        """
        try:
            accounts = self.get_account_list()
            balances = {}
            for acc in accounts:
                if acc['type'] == 'trade':
                    balances[acc['currency']] = {
                        "available": float(acc.get('available', 0)),
                        "hold": float(acc.get('holds', 0)),
                        "balance": float(acc.get('balance', 0))
                    }
            return balances
        except Exception as e:
            logger.info(f"⚠️ Fehler beim Abrufen der Account-Balances: {e}")
            return {}

kucoin_client = KuCoinClientWrapper()

# Direkter Export der Funktion
def get_open_positions(symbols: list = None):
    return kucoin_client.get_open_positions(symbols)

def get_live_account_balances():
    return kucoin_client.get_live_account_balances()


# Utility-Funktion für aktuellen Preis
def get_price(symbol: str) -> float:
    """
    Holt den aktuellen Marktpreis für ein Symbol.
    """
    return kucoin_client.get_symbol_price(symbol)

# Neue Utility-Funktion: get_symbol_price
def get_symbol_price(symbol: str) -> float:
    """
    Liefert den aktuellen Preis für ein Symbol (z.B. ADA-USDT).
    """
    return kucoin_client.get_symbol_price(symbol)

# Utility: Zugriff auf letzten WebSocket-Preis
def get_last_ws_price(symbol: str) -> float:
    """
    Gibt den letzten bekannten WebSocket-Preis für ein Symbol zurück.
    """
    try:
        from strategies.realtime_engine import get_last_ws_price
        return get_last_ws_price(symbol)
    except:
        return 0.0

# Beim Modulimport Cache invalidieren
clear_api_caches()