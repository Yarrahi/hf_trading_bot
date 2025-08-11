# IMPULSE ENGINE - Realtime
import os
import pandas as pd
import collections
import time
import json
import math
import logging
from datetime import datetime
from core.logger import log
from core.position import PositionManager
from core.telegram_utils import notify_live_balance, send_telegram_message
from core.kucoin_api import kucoin_client
from core.order import record_order, send_order_prepared
from core.wallet import get_dynamic_position_size, calculate_position_size
from core.paper_wallet import PaperWallet
from core.paper_order import PaperOrderHandler
from strategies.atr import calculate_atr

# ENV-Konfiguration
ENGINE_LOOP_INTERVAL = float(os.getenv("ENGINE_LOOP_INTERVAL", 10))
TRADE_QUANTITY = float(os.getenv("TRADE_QUANTITY", 0.01))
TAKER_FEE = float(os.getenv("TAKER_FEE", 0.001))
MIN_PROFIT_MARGIN = float(os.getenv("MIN_PROFIT_MARGIN", 0.002))
USE_ATR_STOP = os.getenv("USE_ATR_STOP", "False").lower() == "true"
ATR_MULTIPLIER_SL = float(os.getenv("ATR_MULTIPLIER_SL", 1.5))
ATR_MULTIPLIER_TP = float(os.getenv("ATR_MULTIPLIER_TP", 3.0))
TRAILING_UPDATE_COOLDOWN = int(os.getenv("TRAILING_UPDATE_COOLDOWN", 600))
DYNAMIC_POSITION_SIZING = os.getenv("DYNAMIC_POSITION_SIZING", "false").lower() == "true"
SCALE_OUT_THRESHOLD = float(os.getenv("SCALE_OUT_THRESHOLD", 0.01))
SCALE_OUT_RATIO = float(os.getenv("SCALE_OUT_RATIO", 0.5))
USE_TRAILING_SL = os.getenv("USE_TRAILING_SL", "false").lower() == "true"
DYNAMIC_POSITION_SIZING = os.getenv("DYNAMIC_POSITION_SIZING", "false").lower() == "true"
MAX_TRADE_RISK = float(os.getenv("MAX_TRADE_RISK", 0.01))
REENTRY_COOLDOWN = int(os.getenv("REENTRY_COOLDOWN", 120))
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
LOG_ANALYSIS_ENABLED = os.getenv("LOG_ANALYSIS_ENABLED", "true").lower() == "true"
LOG_ANALYSIS_INTERVAL = float(os.getenv("LOG_ANALYSIS_INTERVAL", 30))
LOG_TICKER_LEVEL = os.getenv("LOG_TICKER_LEVEL", "INFO").upper()
LOG_TICKER_ENABLED = os.getenv("LOG_TICKER_ENABLED", "true").lower() == "true"
LOG_TICKER_INTERVAL = float(os.getenv("LOG_TICKER_INTERVAL", 5))
LOG_POSITION_ENABLED = os.getenv("LOG_POSITION_ENABLED", "true").lower() == "true"
LOG_POSITION_INTERVAL = float(os.getenv("LOG_POSITION_INTERVAL", 300))
PRICE_LOG_LEVEL = os.getenv("PRICE_LOG_LEVEL", "WARNING").upper()
SIGNAL_RETRY_COOLDOWN = int(os.getenv("SIGNAL_RETRY_COOLDOWN", 60))
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", 5.0))
MAX_DRAWDOWN = float(os.getenv("MAX_DRAWDOWN", 20.0))
BALANCE_FILE = "data/balance_tracker.json"
mode = os.getenv("MODE", "PAPER")
log.info(f"🧠 Realtime-Engine gestartet im {mode}-Modus")
IS_PAPER = mode.upper() == "PAPER"
PAPER_HANDLER = PaperOrderHandler() if IS_PAPER else None

# State
price_buffers = {}
last_price_time = {}
last_signal_attempt = {}
last_exit_times = {}
last_entry_times = {}
last_trailing_update_time = {}
last_analysis_log_time = 0
last_ticker_log_time = 0
last_position_log_time = 0
entry_counts = {}

# Load optimized params
try:
    with open("data/bot_params.json", "r") as f:
        OPTIMIZED_PARAMS = json.load(f)
    import hashlib
    with open("data/bot_params.json", "rb") as pf:
        params_bytes = pf.read()
        params_hash = hashlib.sha256(params_bytes).hexdigest()
    log.info(f"🔧 bot_params.json geladen – SHA256: {params_hash}")
    for pair, settings in list(OPTIMIZED_PARAMS.items())[:3]:
        log.info(f"  {pair}: TP={settings.get('tp')} SL={settings.get('sl')} ScaleOut={settings.get('scale_out')}")
except Exception:
    OPTIMIZED_PARAMS = {}
    log.warning("⚠️ Optimierte RSI-Parameter konnten nicht geladen werden.")

from core.order_factory import get_order_handler
position_manager = PositionManager(mode)
order_handler = get_order_handler(mode, position_manager)

def get_risk_values():
    if not os.path.exists(BALANCE_FILE):
        return 0.0, 0.0
    with open(BALANCE_FILE, "r") as f:
        data = json.load(f)
    return data.get("daily_loss_pct", 0.0), data.get("drawdown_pct", 0.0)

def log_risk_event(reason: str, daily_loss: float, drawdown: float):
    log_path = "data/risk_events.log"
    event = {
        "timestamp": datetime.utcnow().isoformat(),
        "reason": reason,
        "daily_loss_pct": daily_loss,
        "drawdown_pct": drawdown
    }
    try:
        if not os.path.exists(os.path.dirname(log_path)):
            os.makedirs(os.path.dirname(log_path))
        with open(log_path, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        log.error(f"Fehler beim Schreiben des Risk-Events: {e}")

def safe_get_candles(symbol, interval, limit, retries=3, delay=1):
    for attempt in range(retries):
        try:
            return kucoin_client.get_candles(symbol=symbol, interval=interval, limit=limit)
        except Exception as e:
            log.warning(f"Fehler beim Abrufen von Candles ({symbol}, {interval}), Versuch {attempt+1}/{retries}: {e}")
            time.sleep(delay)
    return None

def get_last_ws_price(symbol: str) -> float:
    try:
        if symbol in price_buffers and price_buffers[symbol]:
            return price_buffers[symbol][-1]
        return None
    except Exception as e:
        log.warning(f"⚠️ Fehler beim Abrufen des letzten WS-Preises für {symbol}: {e}")
        return None

def init_symbol(symbol: str, maxlen: int = 100):
    if symbol not in price_buffers:
        price_buffers[symbol] = collections.deque(maxlen=maxlen)
        log.debug(f"🆕 Symbol initialisiert: {symbol}")

def on_new_price(symbol: str, price: float, *_):
    global last_analysis_log_time, last_ticker_log_time, last_rsi_log_time, last_position_log_time
    init_symbol(symbol)

    # Logge die geladenen bot_params für das Symbol – aber nur einmal
    if symbol not in last_price_time:
        if symbol in OPTIMIZED_PARAMS:
            log.info(f"⚙️  Aktive bot_params für {symbol}: {OPTIMIZED_PARAMS[symbol]}")
        else:
            log.warning(f"⚠️  Keine bot_params gefunden für {symbol}.")

    # === Live Risk-Management Check ===
    daily_loss, drawdown = get_risk_values()
    if daily_loss >= MAX_DAILY_LOSS or drawdown >= MAX_DRAWDOWN:
        log.error(f"🚨 Risk-Limit erreicht! Tagesverlust {daily_loss:.2f}% / Drawdown {drawdown:.2f}%. Bot wird gestoppt.")
        send_telegram_message(
            f"🚨 <b>Risk-Limit erreicht</b>\nTagesverlust: {daily_loss:.2f}%\nDrawdown: {drawdown:.2f}%\n<b>Bot gestoppt.</b>",
            to_private=True,
            parse_mode="HTML"
        )
        log_risk_event("Risk-Limit erreicht – Bot gestoppt", daily_loss, drawdown)
        import sys
        sys.exit(0)

    now = time.time()
    if symbol in last_price_time and now - last_price_time[symbol] < ENGINE_LOOP_INTERVAL:
        return
    last_price_time[symbol] = now

    try:
        price = float(price)
    except Exception as e:
        log.warning(f"⚠️ Ungültiger Preis für {symbol}: {price} – Fehler: {e}")
        price = get_last_ws_price(symbol) or kucoin_client.get_symbol_price(symbol)
        if price is None:
            return

    # Live-Ticker-Log
    if LOG_TICKER_ENABLED:
        price_buffers[symbol].append(price)
        consolidated_prices = " | ".join([f"{sym}: {price_buffers[sym][-1]:.5f}" for sym in price_buffers if price_buffers[sym]])
        now = time.time()
        if now - last_ticker_log_time >= LOG_TICKER_INTERVAL:
            if LOG_TICKER_LEVEL == "DEBUG":
                log.debug(f"📈 Live-Ticker: {consolidated_prices}")
            elif LOG_TICKER_LEVEL == "WARNING":
                log.warning(f"📈 Live-Ticker: {consolidated_prices}")
            else:
                log.info(f"📈 Live-Ticker: {consolidated_prices}")
            last_ticker_log_time = now
    else:
        price_buffers[symbol].append(price)

    # Impuls-basierter Entry
    if len(price_buffers[symbol]) >= 2:
        last_price = price_buffers[symbol][-2]
        price_change = (price - last_price) / last_price
        log.debug(f"📊 Preisänderung für {symbol}: {price_change:.4%}")
        # --- Symbol-spezifische Settings laden ---
        pair_settings = OPTIMIZED_PARAMS.get(symbol, {})
        symbol_reentry_cd = pair_settings.get("reentry_cooldown", REENTRY_COOLDOWN)
        symbol_max_conc = pair_settings.get("max_concurrent_positions", 1)
        # --- Max concurrent positions check ---
        if entry_counts.get(symbol, 0) >= symbol_max_conc:
            log.info(f"🚫 Max concurrent positions erreicht für {symbol} (Limit: {symbol_max_conc})")
            return
        # --- Re-Entry Cooldown check ---
        now_ts = time.time()
        last_entry_ts = last_entry_times.get(symbol, 0)
        last_exit_ts = last_exit_times.get(symbol, 0)
        last_activity_ts = max(last_entry_ts, last_exit_ts)
        if now_ts - last_activity_ts < symbol_reentry_cd:
            wait_left = int(symbol_reentry_cd - (now_ts - last_activity_ts))
            log.info(f"⏳ Re-Entry Cooldown aktiv für {symbol}: noch {wait_left}s")
            return
        # Impulsschwelle für BUY (z. B. 0.0005 = 0.05 %) – konfigurierbar über .env
        IMPULSE_THRESHOLD = float(os.getenv("IMPULSE_THRESHOLD", 0.001))

        if price_change >= IMPULSE_THRESHOLD and not position_manager.has_open_position(symbol):
            log.info(f"📥 Impuls-BUY für {symbol}: Preisveränderung {price_change:.4%}")
            # Positionsgrößenberechnung
            if DYNAMIC_POSITION_SIZING:
                try:
                    if mode.upper() == "PAPER":
                        # Sizing ausschließlich aus PaperWallet ableiten
                        try:
                            base, quote = symbol.split("-")
                        except ValueError:
                            base, quote = symbol, "USDT"
                        pw = PaperWallet()
                        bal = pw.load_balance()
                        available_quote = float(bal.get(quote, 0.0))
                        log.info(f"💰 PAPER Sizing: verfügbarer {quote}: {available_quote:.8f}")
                        risk_pct = MAX_TRADE_RISK * 100.0
                        # Gebühren berücksichtigen: notional /(1+fee)
                        try:
                            from config.config import get_config
                            fee_rate = float(get_config("TAKER_FEE", 0.0)) or float(get_config("FEE_RATE", 0.0))
                        except Exception:
                            fee_rate = 0.0
                        raw_notional = max(0.0, available_quote * (risk_pct / 100.0))
                        notional = raw_notional / (1.0 + max(fee_rate, 0.0)) if raw_notional > 0 else 0.0
                        trade_quantity = max(0.1, notional / max(price, 1e-9))
                    else:
                        trade_quantity = get_dynamic_position_size(
                            symbol,
                            risk_percent=MAX_TRADE_RISK * 100,
                            min_position=0.1
                        )
                    if LOG_POSITION_ENABLED and LOG_ANALYSIS_ENABLED:
                        from core.logger import log_with_interval
                        log_with_interval(
                            f"position_{symbol}",
                            f"📊 Dynamische Positionsgröße für {symbol}: {trade_quantity:.4f}",
                            level=logging.INFO,
                            interval=LOG_POSITION_INTERVAL,
                        )
                except Exception as e:
                    log.error(f"❌ Fehler bei dynamischer Positionsgrößenberechnung: {e}")
                    trade_quantity = TRADE_QUANTITY
            else:
                if mode.upper() == "PAPER":
                    trade_quantity = TRADE_QUANTITY
                else:
                    trade_quantity = calculate_position_size(symbol, percent=5.0) or TRADE_QUANTITY
                if LOG_POSITION_ENABLED and LOG_ANALYSIS_ENABLED:
                    from core.logger import log_with_interval
                    log_with_interval(
                        f"position_{symbol}",
                        f"📊 Feste Positionsgröße für {symbol}: {trade_quantity:.4f}",
                        level=logging.INFO,
                        interval=LOG_POSITION_INTERVAL,
                    )

            # Fallback, falls Menge numerisch zu klein/0 ist
            try:
                min_notional = float(os.getenv("MIN_ORDER_VALUE_USDT", "1.0"))
            except Exception:
                min_notional = 1.0
            if not trade_quantity or trade_quantity <= 0:
                fallback_qty = max(TRADE_QUANTITY, min_notional / max(price, 1e-9), 0.1)
                log.warning(f"⚠️ Positionsgröße = 0. Fallback auf {fallback_qty:.8f}.")
                trade_quantity = fallback_qty

            # ATR-Parameter ggf. laden
            atr_sl_mult = OPTIMIZED_PARAMS.get(symbol, {}).get("atr_sl_mult")
            atr_tp_mult = OPTIMIZED_PARAMS.get(symbol, {}).get("atr_tp_mult")
            # Validierung der ATR-Parameter
            if atr_sl_mult is None or atr_sl_mult < 0.1:
                atr_sl_mult = ATR_MULTIPLIER_SL
            if atr_tp_mult is None or atr_tp_mult < 0.1:
                atr_tp_mult = ATR_MULTIPLIER_TP
            if IS_PAPER and PAPER_HANDLER is not None:
                response = PAPER_HANDLER.place_order(symbol, "buy", trade_quantity, price, entry_reason="impulse")
            else:
                response = send_order_prepared(kucoin_client, symbol, "buy", price, trade_quantity, strategy="impulse", order_type="limit")
            if response and isinstance(response, dict):
                status = str(response.get("status", "")).lower()
                exch_id = response.get("orderId") or response.get("order_id") or response.get("exch_order_id") or response.get("kucoin_order_id")
                # Nur wenn Order wirklich an die Börse gesendet/akzeptiert wurde
                if status in {"sent", "ack", "filled", "open"} or exch_id:
                    last_entry_times[symbol] = time.time()
                    # === entry_counts erhöhen ===
                    entry_counts[symbol] = entry_counts.get(symbol, 0) + 1
                    entry_price = float(response.get("price", price))
                    sl_offset = float(os.getenv("TRAILING_SL_OFFSET", 0.005))
                    tp_offset = float(os.getenv("TRAILING_TP_OFFSET", 0.02))
                    try:
                        kline_data = safe_get_candles(symbol, interval="15min", limit=50)
                        atr_val = calculate_atr(kline_data)
                        new_sl = entry_price - atr_val * atr_sl_mult if atr_val is not None and atr_sl_mult is not None else entry_price * (1 - sl_offset)
                        new_tp = entry_price + atr_val * atr_tp_mult if atr_val is not None and atr_tp_mult is not None else entry_price * (1 + tp_offset)
                        log.info(f"📏 ATR-Berechnung für {symbol}: ATR={atr_val}, SL-Mult={atr_sl_mult}, TP-Mult={atr_tp_mult}")
                    except Exception as e:
                        log.error(f"❌ ATR-Berechnung fehlgeschlagen, fallback auf feste Offsets: {e}")
                        new_sl = entry_price * (1 - sl_offset)
                        new_tp = entry_price * (1 + tp_offset)
                    position_manager.update_sl(symbol, new_sl)
                    position_manager.update_tp(symbol, new_tp)
                    if mode.upper() == "LIVE":
                        notify_live_balance()
                        send_telegram_message(
                            f"📥 BUY durch Preisimpuls!\n"
                            f"Symbol: {symbol}\nPreis: {entry_price:.5f}\n"
                            f"SL: {new_sl:.5f} | TP: {new_tp:.5f}",
                            to_private=True,
                            to_channel=True
                        )
                else:
                    log.info(f"🧯 BUY-Signal verworfen oder dupliziert – status={status}, response={response}")

    # === Trailing Stop-Loss / Take-Profit ===
    if USE_TRAILING_SL and position_manager.has_open_position(symbol):
        position = position_manager.get_open_position(symbol)
        entry_price = position.get("entry_price")
        quantity = position.get("quantity")
        current_sl = position.get("stop_loss") or position.get("sl")
        current_tp = position.get("take_profit") or position.get("tp")

        trailing_sl_offset = float(os.getenv("TRAILING_SL_OFFSET", 0.005))  # 0.5 %
        trailing_tp_offset = float(os.getenv("TRAILING_TP_OFFSET", 0.02))   # 2.0 %

        # ATR-Parameter laden für trailing (wie im BUY)
        atr_sl_mult = OPTIMIZED_PARAMS.get(symbol, {}).get("atr_sl_mult")
        atr_tp_mult = OPTIMIZED_PARAMS.get(symbol, {}).get("atr_tp_mult")
        # Validierung der ATR-Parameter
        if atr_sl_mult is None or atr_sl_mult < 0.1:
            atr_sl_mult = ATR_MULTIPLIER_SL
        if atr_tp_mult is None or atr_tp_mult < 0.1:
            atr_tp_mult = ATR_MULTIPLIER_TP

        # Nur updaten, wenn ausreichend Zeit vergangen ist
        cooldown = TRAILING_UPDATE_COOLDOWN
        last_update = last_trailing_update_time.get(symbol, 0)
        now = time.time()

        log.info(
            f"🚦 Starte Trailing-Logic für {symbol} | Aktueller SL: {current_sl} | Aktueller TP: {current_tp} | Cooldown: {cooldown}s"
        )

        if now - last_update >= cooldown:
            log.info(
                f"⏱️ Trailing-Cooldown für {symbol} abgelaufen (letztes Update vor {int(now - last_update)}s). Berechne neue SL/TP..."
            )
            # Neuen SL & TP berechnen basierend auf aktuellem Preis und ATR
            try:
                kline_data = safe_get_candles(symbol, interval="15min", limit=50)
                atr_val = calculate_atr(kline_data)
            except Exception:
                atr_val = None
            if atr_val:
                new_sl = max(current_sl or 0, price - atr_val * atr_sl_mult)
                new_tp = max(current_tp or 0, price + atr_val * atr_tp_mult)
            else:
                new_sl = max(current_sl or 0, price - price * trailing_sl_offset)
                new_tp = max(current_tp or 0, price + price * trailing_tp_offset)

            log.info(
                f"🔢 Berechnete neue Werte für {symbol}: new_sl={new_sl} (alt: {current_sl}), new_tp={new_tp} (alt: {current_tp})"
            )

            # Nur updaten, wenn näher am Kurs (Long)
            if new_sl > (current_sl or 0):
                position_manager.update_sl(symbol, new_sl)
                log.info(f"🔄 SL für {symbol} aktualisiert auf {new_sl}")
                send_telegram_message(
                    f"🔄 Trailing SL aktualisiert\nSymbol: {symbol}\nNeuer SL: {new_sl:.5f}",
                    to_private=True,
                    to_channel=False
                )
            if new_tp > (current_tp or 0):
                position_manager.update_tp(symbol, new_tp)
                log.info(f"🔄 TP für {symbol} aktualisiert auf {new_tp}")
                send_telegram_message(
                    f"🔄 Trailing TP aktualisiert\nSymbol: {symbol}\nNeuer TP: {new_tp:.5f}",
                    to_private=True,
                    to_channel=False
                )
            last_trailing_update_time[symbol] = now
        else:
            seconds_left = int(cooldown - (now - last_update))
            log.info(
                f"⏳ Trailing-Update für {symbol} übersprungen – Cooldown aktiv, noch {seconds_left}s verbleibend."
            )

    # === Check Take-Profit / Stop-Loss für offene Positionen ===
    if position_manager.has_open_position(symbol):
        position = position_manager.get_open_position(symbol)
        sl = position.get("stop_loss") or position.get("sl")
        tp = position.get("take_profit") or position.get("tp")
        quantity = position.get("quantity")
        log.debug(f"🔎 Exit-Check {symbol} | price={price:.6f} sl={sl} tp={tp} qty={quantity}")

        if sl and price <= sl:
            log.info(f"🛑 Stop-Loss ausgelöst bei {price:.5f} für {symbol}")
            if IS_PAPER and PAPER_HANDLER is not None:
                response = PAPER_HANDLER.place_order(symbol, "sell", quantity, price, entry_reason="stop_loss")
            else:
                response = send_order_prepared(kucoin_client, symbol, "sell", price, quantity, strategy="impulse", order_type="market")
            if response and isinstance(response, dict):
                status = str(response.get("status", "")).lower()
                exch_id = response.get("orderId") or response.get("order_id") or response.get("exch_order_id") or response.get("kucoin_order_id")
                if status in {"sent", "ack", "filled", "open"} or exch_id:
                    last_exit_times[symbol] = time.time()
                    # === entry_counts zurücksetzen ===
                    entry_counts[symbol] = 0
                    if not IS_PAPER:
                        position_manager.close_position(symbol)
                    if mode.upper() == "LIVE":
                        notify_live_balance()
                        send_telegram_message(
                            f"🔔 Auto-Exit ausgelöst!\n"
                            f"Symbol: {symbol}\nPreis: {price:.5f}\n"
                            f"Stop-Loss erreicht.\nPNL: Berechnung folgt.",
                            to_private=True,
                            to_channel=True
                        )
                else:
                    log.info(f"🧯 SL-Exit nicht bestätigt – status={status}, response={response}")

        elif tp and price >= tp:
            log.info(f"🎯 Take-Profit erreicht bei {price:.5f} für {symbol}")
            # === SCALE OUT ===
            scale_out_enabled = OPTIMIZED_PARAMS.get(symbol, {}).get("scale_out", {}).get("active", False)
            sell_percent = OPTIMIZED_PARAMS.get(symbol, {}).get("scale_out", {}).get("sell_percent", 0)

            if scale_out_enabled and sell_percent > 0:
                partial_qty = quantity * sell_percent
                log.info(f"📉 Scale-Out aktiviert – Verkaufe {sell_percent:.0%} ({partial_qty:.4f}) von {symbol}")
                if IS_PAPER and PAPER_HANDLER is not None:
                    scale_response = PAPER_HANDLER.place_order(symbol, "sell", partial_qty, price, entry_reason="scale_out")
                else:
                    scale_response = send_order_prepared(kucoin_client, symbol, "sell", price, partial_qty, strategy="impulse", order_type="market")
                if scale_response and isinstance(scale_response, dict):
                    st = str(scale_response.get("status", "")).lower()
                    ex_id = scale_response.get("orderId") or scale_response.get("order_id") or scale_response.get("exch_order_id") or scale_response.get("kucoin_order_id")
                    if st in {"sent", "ack", "filled", "open"} or ex_id:
                        if mode.upper() == "LIVE":
                            notify_live_balance()
                            send_telegram_message(
                                f"⚖️ Scale-Out Verkauf\nSymbol: {symbol}\nMenge: {partial_qty:.4f}\nPreis: {price:.5f}",
                                to_private=True,
                                to_channel=True
                            )
                        if not IS_PAPER:
                            position_manager.reduce_position(symbol, partial_qty)
                        return
                    else:
                        log.info(f"🧯 Scale-Out nicht bestätigt – status={st}, response={scale_response}")

            if IS_PAPER and PAPER_HANDLER is not None:
                response = PAPER_HANDLER.place_order(symbol, "sell", quantity, price, entry_reason="take_profit")
            else:
                response = send_order_prepared(kucoin_client, symbol, "sell", price, quantity, strategy="impulse", order_type="market")
            if response and isinstance(response, dict):
                status = str(response.get("status", "")).lower()
                exch_id = response.get("orderId") or response.get("order_id") or response.get("exch_order_id") or response.get("kucoin_order_id")
                if status in {"sent", "ack", "filled", "open"} or exch_id:
                    last_exit_times[symbol] = time.time()
                    # === entry_counts zurücksetzen ===
                    entry_counts[symbol] = 0
                    if not IS_PAPER:
                        position_manager.close_position(symbol)
                    if mode.upper() == "LIVE":
                        notify_live_balance()
                        send_telegram_message(
                            f"🎯 Take-Profit erreicht!\n"
                            f"Symbol: {symbol}\nPreis: {price:.5f}\n"
                            f"PNL: Berechnung folgt.",
                            to_private=True,
                            to_channel=True
                        )
                else:
                    log.info(f"🧯 TP-Exit nicht bestätigt – status={status}, response={response}")

    # === Recovery: SL/TP nachladen falls fehlen oder zu weit entfernt ===
    if position_manager.has_open_position(symbol):
        position = position_manager.get_open_position(symbol)
        entry_price = position.get("entry_price")
        current_sl = position.get("stop_loss") or position.get("sl")
        current_tp = position.get("take_profit") or position.get("tp")
        # ATR-Parameter laden
        atr_sl_mult = OPTIMIZED_PARAMS.get(symbol, {}).get("atr_sl_mult")
        atr_tp_mult = OPTIMIZED_PARAMS.get(symbol, {}).get("atr_tp_mult")
        if atr_sl_mult is None or atr_sl_mult < 0.1:
            atr_sl_mult = ATR_MULTIPLIER_SL
        if atr_tp_mult is None or atr_tp_mult < 0.1:
            atr_tp_mult = ATR_MULTIPLIER_TP
        # ATR Wert holen
        try:
            kline_data = safe_get_candles(symbol, interval="15min", limit=50)
            atr_val = calculate_atr(kline_data)
        except Exception:
            atr_val = None
        sl_offset = float(os.getenv("TRAILING_SL_OFFSET", 0.005))
        tp_offset = float(os.getenv("TRAILING_TP_OFFSET", 0.02))
        # SL/TP zu weit entfernt (>10%) oder fehlt?
        sl_should_update = False
        tp_should_update = False
        if current_sl is None or (entry_price and current_sl < entry_price and abs(current_sl - entry_price) / entry_price > 0.1):
            sl_should_update = True
        if current_tp is None or (entry_price and current_tp > entry_price and abs(current_tp - entry_price) / entry_price > 0.1):
            tp_should_update = True
        if sl_should_update:
            if atr_val:
                new_sl = entry_price - atr_val * atr_sl_mult
            else:
                new_sl = entry_price * (1 - sl_offset)
            position_manager.update_sl(symbol, new_sl)
            log.info(f"♻️ Recovery: SL für {symbol} neu gesetzt auf {new_sl}")
        if tp_should_update:
            if atr_val:
                new_tp = entry_price + atr_val * atr_tp_mult
            else:
                new_tp = entry_price * (1 + tp_offset)
            position_manager.update_tp(symbol, new_tp)
            log.info(f"♻️ Recovery: TP für {symbol} neu gesetzt auf {new_tp}")

    import shutil
def cleanup_checkpoints():
    """Löscht alle Checkpoint-Dateien nach erfolgreicher Optimierung."""
    checkpoint_dir = "data/checkpoints"
    try:
        if os.path.exists(checkpoint_dir):
            shutil.rmtree(checkpoint_dir)
            log.info("🧹 Checkpoints-Ordner wurde nach erfolgreichem Prozess gelöscht.")
    except Exception as e:
        log.error(f"Fehler beim Löschen des Checkpoints-Ordners: {e}")

__all__ = ["get_last_ws_price", "cleanup_checkpoints"]