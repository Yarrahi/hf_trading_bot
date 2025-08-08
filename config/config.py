import os
from dotenv import load_dotenv
from pathlib import Path
import json

__all__ = ["get_env_var", "get_symbol_config", "get_config"]
# Lade nur die Standard-.env für PAPER und LIVE Modus
dotenv_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=dotenv_path)

def get_env_var(key: str, default: str = None) -> str:
    return os.getenv(key, default)

KUCOIN_API_KEY = get_env_var("KUCOIN_API_KEY")
KUCOIN_API_SECRET = get_env_var("KUCOIN_API_SECRET")
KUCOIN_API_PASSPHRASE = get_env_var("KUCOIN_API_PASSPHRASE")

MODE = get_env_var("MODE", "PAPER").upper()

BASE_DIR = Path(__file__).resolve().parent.parent
LIVE_POSITIONS_FILE = BASE_DIR / "data" / "positions_live.json"
PAPER_POSITIONS_FILE = BASE_DIR / "data" / "positions_paper.json"

WALLET_FILE = BASE_DIR / "data/logs" / "wallet.json"

KUCOIN_SANDBOX = get_env_var("KUCOIN_SANDBOX", "False").lower() == "true"

required_keys = ["KUCOIN_API_KEY", "KUCOIN_API_SECRET", "KUCOIN_API_PASSPHRASE"]
missing = [key for key in required_keys if not get_env_var(key)]
if MODE == "LIVE" and missing:
    raise RuntimeError(f"❌ Fehlende Umgebungsvariablen für LIVE-Modus: {', '.join(missing)} – Bot wird gestoppt.")

def get_pair_list() -> list[str]:
    return get_env_var("PAIRS", "BTC-USDT").replace(" ", "").split(",")

# Multi-Symbol-Scanner Konfiguration (Phase 1) aus JSON-Datei laden
SYMBOL_CONFIG_PATH = BASE_DIR / "config" / "symbols.json"
with open(SYMBOL_CONFIG_PATH, "r") as f:
    SYMBOL_CONFIG = json.load(f)

SCAN_INTERVAL = 5  # Sekunden zwischen den Scans pro Symbol

def get_symbol_config():
    return SYMBOL_CONFIG

def get_config_int(key: str, default: int) -> int:
    return int(os.getenv(key, default))

from typing import Optional, List

def get_config_list(key: str, default: Optional[List[str]] = None) -> List[str]:
    value = os.getenv(key)
    if value is None:
        return default or []
    return [v.strip() for v in value.split(',') if v.strip()]

def get_trade_allocation(price: float, atr: float, symbol: str) -> dict:
    """
    Berechne die Trade-Allokation basierend auf Risiko, Preis und Mindestgrößen.
    Berücksichtigt symbolabhängige Mindest-Ordergrößen (sofern dynamisch verfügbar).
    """
    from core.logger import setup_logger
    logger = setup_logger(__name__)
    from core.wallet import wallet_instance
    import math

    use_atr = get_env_var("USE_ATR_SLTP", "False").lower() == "true"
    min_order_usdt = float(get_env_var("MIN_ORDER_VALUE_USDT", "5.0"))

    try:
        if MODE == "LIVE":
            capital_raw = wallet_instance.get_balance("USDT")
            capital = float(capital_raw.get("available", 0)) if isinstance(capital_raw, dict) else float(capital_raw or 0.0)
        else:
            capital = float(get_env_var("TRADE_CAPITAL_USDT", "1000"))

        risk_pct = float(get_env_var("POSITION_SIZE_PERCENT", "1.0"))
        risk_amount = capital * (risk_pct / 100)

        if use_atr:
            trade_risk = atr * 1.5 if atr and atr > 0 else max(0.005, price * 0.001)
            position_size = risk_amount / trade_risk
        else:
            position_size = risk_amount

        quantity = position_size / price
        usdt_value = quantity * price

        # Reduziere auf verfügbares Kapital
        if usdt_value > capital:
            quantity = capital / price
            usdt_value = quantity * price
            logger.warning(f"⚠️ Reduziere Ordergröße: benötigt {usdt_value:.2f} > verfügbar {capital:.2f}")

        # Dynamische Mindestordergröße pro Symbol (falls verfügbar)
        try:
            from core.kucoin_api import get_symbol_min_order
            min_order_usdt = get_symbol_min_order(symbol) or min_order_usdt
        except Exception:
            pass

        # Im LIVE-Modus prüfen wir Mindestwerte strenger
        if MODE == "LIVE":
            if usdt_value < min_order_usdt:
                logger.warning(f"❌ Orderwert ({usdt_value:.2f} USDT) < Mindestgröße ({min_order_usdt}) – Trade abgelehnt.")
                return {
                    "quantity": 0.0,
                    "capital": 0.0,
                    "is_trade_possible": False
                }

        logger.debug(f"✅ ALLOKATION: capital={capital:.2f}, risk_pct={risk_pct}, price={price:.6f}, quantity={quantity:.6f}, usdt_value={usdt_value:.2f}")
        return {
            "quantity": round(quantity, 6),
            "capital": round(usdt_value, 2),
            "is_trade_possible": True
        }

    except Exception as e:
        from core.logger import setup_logger
        setup_logger(__name__).warning(f"⚠️ Fehler bei get_trade_allocation(): {e}")
        return {"quantity": 0.0, "capital": 0.0, "is_trade_possible": False}

def get_config(key: str, default: Optional[str] = None) -> str:
    return os.getenv(key, default)

def get_config_dict() -> dict:
    return {
        "KUCOIN_API_KEY": KUCOIN_API_KEY,
        "KUCOIN_API_SECRET": KUCOIN_API_SECRET,
        "KUCOIN_API_PASSPHRASE": KUCOIN_API_PASSPHRASE,
        "MODE": MODE,
        "BASE_DIR": str(BASE_DIR),
        "WALLET_FILE": WALLET_FILE,
        "KUCOIN_SANDBOX": KUCOIN_SANDBOX,
        "PAIRS": get_pair_list(),
        "SCAN_INTERVAL": SCAN_INTERVAL,
    }
