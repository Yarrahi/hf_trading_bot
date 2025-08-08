from core.logger import setup_logger
logger = setup_logger(__name__)
import pandas as pd

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    Berechnet den Average True Range (ATR) eines DataFrames.

    Args:
        df (pd.DataFrame): OHLCV-Daten mit Spalten ['high', 'low', 'close']
        period (int): ATR-Zeitraum

    Returns:
        float: aktueller ATR-Wert
    """
    # Debug: Typ und Beispielinhalt loggen
    logger.debug(f"ATR Input-Typ: {type(df)}")
    if isinstance(df, str):
        logger.debug(f"ATR Input (str) Beispiel: {df[:200]}")
    elif isinstance(df, list):
        logger.debug(f"ATR Input (list) Länge: {len(df)} Beispiel: {df[:2]}")

    try:
        if df is None or len(df) == 0:
            logger.warning(f"ATR-Berechnung abgebrochen: Leere oder None-Daten empfangen. Typ: {type(df)}, Länge: {0 if df is None else len(df)}")
            return 0.0
        if not isinstance(df, pd.DataFrame):
            if isinstance(df, str):
                import json
                try:
                    parsed = json.loads(df)
                    if isinstance(parsed, list) and all(isinstance(row, (list, tuple)) and len(row) >= 6 for row in parsed):
                        df = pd.DataFrame(parsed, columns=["timestamp", "open", "high", "low", "close", "volume"])
                    else:
                        logger.error(f"Ungültiges JSON-Format für ATR-Berechnung: {type(parsed)}")
                        return 0.0
                except Exception as json_e:
                    logger.error(f"Fehler beim Parsen von JSON für ATR-Berechnung: {json_e}. Input: {df[:200] if isinstance(df, str) else str(df)[:200]}")
                    return 0.0
            elif isinstance(df, list) and all(isinstance(row, (list, tuple)) and len(row) >= 6 for row in df):
                try:
                    df = pd.DataFrame(df, columns=["timestamp", "open", "high", "low", "close", "volume"])
                except Exception as e:
                    logger.error(f"Fehler beim Konvertieren von ATR-Daten in DataFrame: {e}. Input-Beispiel: {str(df)[:200]}")
                    return 0.0
            else:
                logger.error(f"Ungültiges Datenformat für ATR-Berechnung: {type(df)}")
                return 0.0
        if not all(col in df.columns for col in ["high", "low", "close"]):
            logger.error(f"ATR-Berechnung fehlgeschlagen: Fehlende Spalten in DataFrame. Vorhandene Spalten: {df.columns.tolist()}")
            return 0.0
        df = df[["high", "low", "close"]].apply(pd.to_numeric, errors="coerce").dropna()
    except Exception as e:
        logger.error(f"Fehler beim Vorbereiten der Kerzendaten für ATR: {e}")
        return 0.0

    if len(df) < period + 1:
        logger.warning(f"Zu wenige Datenpunkte ({len(df)}) für ATR-Berechnung mit Periode {period}.")
        return 0.0

    df = df.sort_index()  # sicherstellen, dass chronologisch
    high = df["high"]
    low = df["low"]
    close = df["close"]

    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr = tr.ewm(span=period, min_periods=period).mean()
    atr_value = round(atr.iloc[-1], 6) if not atr.isna().all() else 0.0

    logger.debug(f"ATR Endwert: {atr_value}")

    if atr_value == 0.0:
        logger.warning("ATR konnte nicht berechnet werden oder ist 0.")
    else:
        logger.debug(f"📊 ATR berechnet: {atr_value} basierend auf {len(df)} Candles.")
    return atr_value


# Utility-Funktion zur Berechnung von Stop-Loss und Take-Profit basierend auf ATR
def calculate_sl_tp(entry_price: float, atr: float, sl_multiplier: float = 1.5, tp_multiplier: float = 3.0) -> tuple:
    """
    Berechnet Stop-Loss (SL) und Take-Profit (TP) basierend auf ATR.

    Args:
        entry_price (float): Einstiegs-Preis
        atr (float): ATR-Wert
        sl_multiplier (float): Multiplikator für Stop-Loss
        tp_multiplier (float): Multiplikator für Take-Profit

    Returns:
        tuple: (stop_loss, take_profit)
    """
    if atr <= 0 or entry_price <= 0:
        stop_loss = round(entry_price * 0.98, 6)
        take_profit = round(entry_price * 1.04, 6)
        logger.warning(f"ATR ungültig – Fallback SL/TP gesetzt: SL={stop_loss}, TP={take_profit}")
        return stop_loss, take_profit

    stop_loss = round(entry_price - (atr * sl_multiplier), 6)
    take_profit = round(entry_price + (atr * tp_multiplier), 6)
    logger.info(f"🛑 SL berechnet: {stop_loss}, 🎯 TP berechnet: {take_profit}")
    return stop_loss, take_profit


# ATR direkt holen (Candles + ATR-Berechnung)
import os
from core.kucoin_api import KuCoinClientWrapper

# Konfigurierbare ATR-Parameter über Umgebungsvariablen
ATR_TIMEFRAME = os.getenv("ATR_TIMEFRAME", "1hour")
ATR_CANDLE_LIMIT = int(os.getenv("ATR_CANDLE_LIMIT", 100))

def get_atr(symbol: str, period: int = 14) -> float:
    """
    Holt historische Candle-Daten von KuCoin und berechnet den ATR für das gegebene Symbol.
    Timeframe und Candle-Anzahl sind über .env konfigurierbar.
    """
    try:
        client = KuCoinClientWrapper()
        candles = client.get_candles(symbol, interval=ATR_TIMEFRAME, limit=ATR_CANDLE_LIMIT)
        return calculate_atr(candles, period)
    except Exception as e:
        logger.error(f"Fehler beim Abrufen/Berechnen des ATR für {symbol}: {e}")
        return 0.0