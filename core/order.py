import threading
import decimal
import os
import json
import time
import requests
from config.config import get_config
USE_ATR_STOP = os.getenv("USE_ATR_STOP", "False").lower() == "true"
ATR_MULTIPLIER_SL = float(os.getenv("ATR_MULTIPLIER_SL", 1.5))
ATR_MULTIPLIER_TP = float(os.getenv("ATR_MULTIPLIER_TP", 3.0))

# DEBUG_MODE für detailliertes Logging
DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() == "true"
from core.telegram_utils import send_safe_message
from core.telegram_utils import send_telegram_message
from core.utils import load_json_file
from core.logger import log_info, log_error, log_debug
from core.recovery import auto_backup
from core.wallet import get_dynamic_position_size, calculate_position_size
SILENT_MODE = get_config("SILENT_MODE") == "true"
LOG_TO_TELEGRAM = get_config("LOG_TO_TELEGRAM") == "true"

fee_rate = float(get_config("TRADING_FEE_RATE", 0.001))  # z. B. 0.001 = 0.1%

LOG_TRADES = os.getenv("LOG_TRADES", "False").lower() == "true"

LOG_TO_TRADES_LOG = os.getenv("LOG_TO_TRADES_LOG", "False").lower() == "true"

# Timeout-Wrapper für Funktionsaufrufe
def run_with_timeout(func, args=(), kwargs={}, timeout=10):
    result = [None]
    exception = [None]

    def wrapper():
        try:
            result[0] = func(*args, **kwargs)
        except Exception as e:
            exception[0] = e

    thread = threading.Thread(target=wrapper)
    thread.daemon = True
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        raise TimeoutError("⏱️ Funktionsaufruf überschreitet das Zeitlimit von {} Sekunden.".format(timeout))
    if exception[0]:
        raise exception[0]
    return result[0]
from core.wallet import Wallet, notify_live_balance, wallet_instance, safe_update_balance
from core.wallet import get_live_balance
from core.filters import prepare_order
from core.ids import make_client_oid
from core.orders_db import get_db

import uuid

ORDER_HISTORY_FILE = "data/order_history.json"

order_history_lock = threading.Lock()

# Hilfsklasse für Decimal-Encoding beim JSON-Dump
class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        return super().default(obj)

# Refactored: LiveOrderHandler -> OrderHandler
class OrderHandler:
    def __init__(self, mode):
        self.mode = mode

    def place_order(self, symbol, side, quantity, price=None, entry_reason=None, position_manager=None):
        # Fallback: use handler's stored position_manager if caller didn't provide one
        if position_manager is None:
            position_manager = getattr(self, "position_manager", None)
        # Ensure entry_reason and position_manager are passed through
        return place_market_order_live(
            symbol,
            side,
            quantity,
            price=price,
            position_manager=position_manager,
            entry_reason=entry_reason,
        )

# === Phase 1: unified, idempotent order sender ===
def send_order_prepared(api, symbol: str, side: str, price, qty, strategy: str = "default", order_type: str = "market"):
    """
    Einheitlicher Order-Send mit:
      - Runden via FilterBook (prepare_order)
      - minFunds/minQty-Prüfung
      - Idempotenz via SQLite (clientOid)
      - Fallback: get_order_by_client_oid() bei Timeout/Netzwerkfehler
    Rückgabe: API-Response (dict) erweitert um clientOid, oder Local-Reject-Dict.
    """
    # Force SELL to use MARKET to avoid SL/TP rejections (e.g., 200004) on tight moves
    if str(side).lower() == "sell":
        order_type = "market"
    # --- Normalize qty for LIVE BUY: handle quote-amount (USDT) and cap by available funds ---
    try:
        runtime_mode = (os.getenv("RUNTIME_MODE") or get_config("MODE") or "PAPER").upper()
    except Exception:
        runtime_mode = "PAPER"
    is_live = runtime_mode == "LIVE"

    qty_mode = (os.getenv("QTY_MODE") or "auto").lower()  # "auto" | "base" | "quote"

    avail_usdt = None
    if is_live and side.lower() == "buy" and hasattr(api, "get_account_list"):
        try:
            bals = api.get_account_list()
            for b in bals:
                if str(b.get("currency")).upper() == "USDT" and str(b.get("type", "")).lower() == "trade":
                    avail_usdt = float(b.get("available", 0.0))
                    break
        except Exception:
            avail_usdt = None

    qpx_in = float(price)
    qty_in = float(qty)
    qty_is_quote = (qty_mode == "quote")

    if qty_mode == "auto":
        # Heuristik: wenn qty*price >> verfügbares USDT, ist qty sehr wahrscheinlich USDT-Notional
        if avail_usdt is not None and qty_in * qpx_in > avail_usdt * 1.1:
            qty_is_quote = True
        # Zusätzlich: sehr große qty bei hohem Preis -> wahrscheinlich USDT-Notional
        if not qty_is_quote and qpx_in > 1000 and qty_in > 5:
            qty_is_quote = True

    if side.lower() == "buy" and qty_is_quote:
        base_qty = qty_in / qpx_in
        log_info(f"↪️ Interpretiere qty als QUOTE (USDT): {qty_in} USDT → {base_qty} {symbol.split('-')[0]}")
        qty_in = base_qty

    # Cap durch verfügbares USDT (mit Sicherheitsmarge)
    if is_live and side.lower() == "buy" and avail_usdt is not None:
        safety_margin_cap = float(os.getenv("KUCOIN_BUY_SAFETY_MARGIN", "0.98"))
        max_affordable_base = (avail_usdt * safety_margin_cap) / qpx_in
        if qty_in > max_affordable_base:
            log_info(f"💳 Menge gekappt durch verfügbare USDT: {qty_in} → {max_affordable_base}")
            qty_in = max_affordable_base

    # Optional: absolute Kappe per MAX_TRADE_USDT
    try:
        max_trade_usdt = float(os.getenv("MAX_TRADE_USDT", "0"))
    except Exception:
        max_trade_usdt = 0.0
    if side.lower() == "buy" and max_trade_usdt > 0:
        cap_base = max_trade_usdt / qpx_in
        if qty_in > cap_base:
            log_info(f"📉 Menge durch MAX_TRADE_USDT gekappt: {qty_in} → {cap_base}")
            qty_in = cap_base

    # 1) Precision + Constraints prüfen (nach Normalisierung)
    qpx, qqty, err, notional = prepare_order(symbol, side, price, qty_in)
    if err:
        log_info(f"Local reject {symbol} {side}: {err} (px={qpx}, qty={qqty}, notional={notional})")
        return {"status": "rejected_local", "reason": err, "symbol": symbol, "side": side, "price": str(qpx), "qty": str(qqty)}

    # --- Safety margin to avoid 200004 (Balance insufficient) on LIVE BUYs ---
    try:
        runtime_mode = os.getenv("RUNTIME_MODE") or get_config("MODE") or "PAPER"
    except Exception:
        runtime_mode = "PAPER"
    is_live = str(runtime_mode).upper() == "LIVE"
    safety_margin = float(os.getenv("KUCOIN_BUY_SAFETY_MARGIN", "0.98"))
    if is_live and str(side).lower() == "buy":
        pre_qty = qqty
        # reduce qty by margin
        reduced_qty = float(qqty) * safety_margin
        # re-apply rounding/constraints with the already rounded price `qpx`
        qpx2, qqty2, err2, notional2 = prepare_order(symbol, side, qpx, reduced_qty)
        if err2:
            # If we drop below min notional because of the margin, keep original qty (will likely fail), but log it
            log_info(f"⚠️ Safety margin made order invalid ({err2}); keeping original qty for {symbol} {side}")
        else:
            qqty = qqty2
            log_info(f"🔧 Safety margin applied for LIVE BUY: qty {pre_qty} -> {qqty} (margin={safety_margin})")

    # 2) Idempotenz-Key (prozessübergreifend stabil)
    oid = make_client_oid(symbol, side, str(qpx), str(qqty), strategy=strategy)
    odb = get_db()

    # TTL-basiertes Aufräumen (verhindert hängende Reservierungen)
    ttl_sec = int(os.getenv("IDEMPOTENCY_TTL_SEC", "5"))
    try:
        odb.purge_stale(ttl_sec=ttl_sec)
    except Exception:
        pass

    # Duplicate-Check VOR der Reservierung
    if odb.exists_active(oid, ttl_sec=ttl_sec):
        try:
            existing_state = (odb.get(oid) or {}).get("state")
        except Exception:
            existing_state = None
        log_info(f"idempotent-skip {symbol} {side} oid={oid} state={existing_state}")
        return {"status": "duplicate", "clientOid": oid}

    # Reservierung: markiere diese OID als gesendet
    odb.upsert_sent(oid, symbol, side, str(qpx), str(qqty))

    # 3) Senden mit clientOid und State pflegen
    try:
        def _submit_limit():
            # Try various wrappers/signatures
            # 1) Wrapper method with snake_case client_oid
            try:
                if hasattr(api, "create_limit_order"):
                    return api.create_limit_order(symbol, side, price=str(qpx), size=str(qqty), client_oid=oid)
            except TypeError:
                pass
            # 2) Wrapper method with camelCase clientOid
            try:
                if hasattr(api, "create_limit_order"):
                    return api.create_limit_order(symbol, side, price=str(qpx), size=str(qqty), clientOid=oid)
            except TypeError:
                pass
            # 3) Generic place_order on wrapper
            if hasattr(api, "place_order"):
                return api.place_order(symbol=symbol, side=side, price=str(qpx), size=str(qqty), client_oid=oid, order_type="limit")
            # 4) Direct access to underlying trade client if exposed
            trade = getattr(api, "trade", None)
            if trade and hasattr(trade, "create_limit_order"):
                try:
                    return trade.create_limit_order(symbol=symbol, side=side, price=str(qpx), size=str(qqty), clientOid=oid)
                except TypeError:
                    return trade.create_limit_order(symbol=symbol, side=side, price=str(qpx), size=str(qqty), client_oid=oid)
            raise AttributeError("No compatible limit-order method found on KuCoin client")

        def _submit_market():
            # 1) Wrapper method
            if hasattr(api, "create_market_order"):
                try:
                    return api.create_market_order(symbol, side, size=str(qqty))
                except TypeError:
                    return api.create_market_order(symbol=symbol, side=side, size=str(qqty))
            # 2) Generic place_order on wrapper
            if hasattr(api, "place_order"):
                return api.place_order(symbol=symbol, side=side, size=str(qqty), order_type="market")
            # 3) trade client
            trade = getattr(api, "trade", None)
            if trade and hasattr(trade, "create_market_order"):
                return trade.create_market_order(symbol=symbol, side=side, size=str(qqty))
            raise AttributeError("No compatible market-order method found on KuCoin client")

        if order_type == "market":
            resp = _submit_market()
        else:
            resp = _submit_limit()

        new_state = "open"
        if isinstance(resp, dict) and resp.get("status") in ("done", "filled", "success"):
            new_state = "filled"

        exch_id = None
        if isinstance(resp, dict):
            exch_id = resp.get("orderId") or resp.get("order_id") or resp.get("data") or resp.get("id")
            resp["clientOid"] = oid
            resp.setdefault("id", exch_id or oid)

        odb.set_state(oid, new_state, exch_id)

        # --- Enrich + persist order locally (history + positions) ---
        try:
            # Determine runtime/mode
            try:
                runtime_mode = (os.getenv("RUNTIME_MODE") or get_config("MODE") or "PAPER").upper()
            except Exception:
                runtime_mode = "PAPER"
            is_live = runtime_mode == "LIVE"

            # Prepare safe numeric values
            qpx_final = float(qpx)
            qqty_final = float(qqty)

            # Try to fetch order details to obtain dealSize/dealFunds/fee
            order_details = {}
            trade_client = getattr(api, "trade", None)
            if trade_client and exch_id:
                for _i in range(3):
                    try:
                        od = trade_client.get_order_details(exch_id)
                        if isinstance(od, dict):
                            order_details = od
                        deal_size_tmp = float((order_details.get("dealSize") or 0) or 0)
                        status_tmp = str(order_details.get("status") or "").lower()
                        if deal_size_tmp > 0 or status_tmp in ("done", "filled", "success", "finished"):
                            break
                    except Exception:
                        pass
                    time.sleep(0.35)

            # Build order dict with fallbacks
            now_ts = int(time.time())
            order_side_up = str(side).upper()
            # Default price:
            eff_price = None
            # 1) from order_details
            try:
                deal_size = float((order_details.get("dealSize") or 0) or 0)
                deal_funds = float((order_details.get("dealFunds") or 0) or 0)
                if deal_size > 0 and deal_funds > 0:
                    eff_price = round(deal_funds / deal_size, 8)
            except Exception:
                pass
            # 2) from input price (for limit) if still None
            if eff_price is None and order_type == "limit":
                eff_price = float(qpx_final)
            # 3) from ticker (market fallback)
            if eff_price is None:
                try:
                    market_client = getattr(api, "market", None)
                    if market_client:
                        tk = market_client.get_ticker(symbol)
                        eff_price = float(tk.get("price"))
                except Exception:
                    eff_price = None

            # Fee fallback
            fee_val = None
            try:
                fee_raw = order_details.get("fee") if isinstance(order_details, dict) else None
                if fee_raw is not None:
                    fee_val = round(float(fee_raw), 8)
            except Exception:
                fee_val = None
            if fee_val is None:
                fee_val = 0.0

            order_local = {
                "id": resp.get("id") or exch_id or oid,
                "timestamp": now_ts,
                "mode": "LIVE" if is_live else "PAPER",
                "symbol": symbol,
                "side": order_side_up,
                "quantity": qqty_final,
                "price": eff_price,
                "fee": fee_val,
                "entry_price": eff_price if order_side_up == "BUY" else None,
                "sl": None,
                "tp": None,
                "pnl": 0.0,
                "reason": strategy or "default",
                "trade_tags": {"sender": "send_order_prepared", "order_type": order_type},
            }

            # Compute basic SL/TP fallback (avoid None in history)
            try:
                if order_local["entry_price"]:
                    ep = float(order_local["entry_price"])
                    sl_off = 0.01
                    tp_off = 0.03
                    order_local["sl"] = round(ep * (1 - sl_off), 6)
                    order_local["tp"] = round(ep * (1 + tp_off), 6)
            except Exception:
                pass

            # Persist into order_history.json
            record_order(order_local)

            # --- Telegram notification for BUY/SELL (unified) ---
            try:
                from core.telegram_utils import send_telegram_message
                # Prepare formatting values
                price_val = order_local.get("price")
                try:
                    price_str = f"{float(price_val):.5f}" if price_val is not None else "?"
                except Exception:
                    price_str = str(price_val) if price_val is not None else "?"
                qty_val = qqty_final
                ts_val = order_local.get("timestamp", now_ts)
                sl_val = order_local.get("sl")
                tp_val = order_local.get("tp")
                # message per side
                if order_side_up == "BUY":
                    send_telegram_message(
                        f"🟢 LIVE-ORDER\n"
                        f"➡️ BUY {symbol}\n"
                        f"💰 Preis: {price_str}\n"
                        f"📦 Menge: {qty_val:.6f}\n"
                        f"🕒 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts_val))}\n"
                        f"🛑 Stop Loss: {sl_val}\n"
                        f"🎯 Take Profit: {tp_val}\n",
                        to_channel=True,
                        to_private=True
                    )
                elif order_side_up == "SELL":
                    fee_val = order_local.get("fee")
                    try:
                        fee_str = f"{float(fee_val):.6f}" if fee_val is not None else "?"
                    except Exception:
                        fee_str = str(fee_val) if fee_val is not None else "?"
                    send_telegram_message(
                        f"🔴 LIVE-ORDER\n"
                        f"➡️ SELL {symbol}\n"
                        f"💰 Preis: {price_str}\n"
                        f"📦 Menge: {qty_val:.6f}\n"
                        f"💸 Gebühr: {fee_str}\n"
                        f"🕒 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts_val))}\n"
                        f"🛑 Stop Loss: {sl_val}\n"
                        f"🎯 Take Profit: {tp_val}\n",
                        to_channel=True,
                        to_private=True
                    )
            except Exception as _te:
                log_error(f"⚠️ Telegram-Benachrichtigung in send_order_prepared fehlgeschlagen: {_te}")

            # Persist into live positions if LIVE + BUY
            if is_live and order_side_up == "BUY":
                try:
                    # Open via PositionManager to keep internal state updated (single source of truth)
                    from core.position import PositionManager
                    pm = PositionManager(mode="LIVE")
                    pm.open(symbol, qqty_final, order_local["price"] or qpx_final, fee=fee_val, entry_fee=fee_val)
                except Exception:
                    pass
        except Exception:
            # Never break the original return on persistence errors
            pass

        return resp

    except Exception as e:
        # 4) Fallback: prüfen, ob Order unter clientOid existiert
        try:
            getter = None
            if hasattr(api, "get_order_by_client_oid"):
                getter = api.get_order_by_client_oid
            elif hasattr(getattr(api, "trade", None), "get_order_by_client_oid"):
                getter = api.trade.get_order_by_client_oid
            if getter is not None:
                q = getter(oid)
                if q:
                    odb.set_state(oid, "open", q.get("orderId") or q.get("id"))
                    q["clientOid"] = oid
                    q.setdefault("id", q.get("orderId") or q.get("id") or oid)
                    return q
        except Exception:
            pass
        odb.set_state(oid, "failed", last_error=str(e))
        raise e

def load_order_history():
    if not os.path.exists(ORDER_HISTORY_FILE):
        try:
            with open(ORDER_HISTORY_FILE, "w") as f:
                json.dump([], f, indent=4)
        except Exception as e:
            log_error(f"❌ Fehler beim Erstellen von order_history.json: {e}")
        return []

    try:
        with open(ORDER_HISTORY_FILE, "r") as f:
            content = f.read().strip()
            if not content:
                return []

            try:
                result = json.loads(content)
                return result
            except json.JSONDecodeError as e:
                log_error(f"❌ JSONDecodeError beim Parsen als Array: {e}")
                orders = []
                f.seek(0)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        orders.append(obj)
                    except json.JSONDecodeError as e2:
                        log_error(f"⚠️ Ungültige JSON-Zeile: {e2} | Inhalt: {line[:80]}")
                        continue
                return orders

    except Exception as e:
        log_error(f"❌ Allgemeiner Fehler beim Laden von order_history.json: {e}")
        return []

def save_order_history(data: list):
    try:
        with order_history_lock:
            with open(ORDER_HISTORY_FILE, "w") as f:
                json.dump(data, f, indent=4, cls=DecimalEncoder)
    except Exception as e:
        log_error(f"❌ s_o_h: Fehler beim Speichern von order_history.json: {e}")
        import traceback
        log_error(traceback.format_exc())

def record_order(order, position=None):
    # --- Normalize response id for robustness ---
    try:
        if isinstance(order, dict):
            resp_id = order.get("id") or order.get("orderId") or order.get("clientOid")
            if not resp_id:
                sym = order.get("symbol", "?")
                side = order.get("side", "?")
                resp_id = f"{sym}-{side}-{int(time.time()*1000)}"
            order.setdefault("id", resp_id)
    except Exception:
        pass
    # Skip duplicates from idempotent acknowledgements
    try:
        if isinstance(order, dict) and order.get("status") == "duplicate":
            log_info(f"duplicate-not-saved id={order.get('id') or order.get('clientOid')}")
            return
    except Exception:
        pass
    if DEBUG_MODE:
        log_info(f"Speichere Order in record_order: {order}")
    """
    Erweitertes Order-Logging: Speichert vollständige Orderinformationen inkl. SL/TP, PNL und Gebühren.
    """
    try:
        trade = {
            "id": order.get("id"),
            "timestamp": order.get("timestamp", int(time.time())),
            "mode": order.get("mode", "LIVE"),
            "symbol": order.get("symbol"),
            "side": str(order.get("side")).upper(),
            "quantity": order.get("quantity"),
            "price": order.get("price"),
            "fee": order.get("fee", 0.0),
            "entry_price": order.get("entry_price"),
            "sl": order.get("sl"),
            "tp": order.get("tp"),
            "pnl": order.get("pnl"),
            "reason": order.get("reason") or order.get("entry_reason"),
            # Neue Trade-Tags
            "trade_tags": order.get("trade_tags", {}),
        }

        # --- PNL Berechnung für SELL Orders ---
        if trade["side"] == "SELL":
            try:
                entry_price = 0
                if position:
                    entry_price = position.get("entry_price", 0)
                if not entry_price:
                    # Fallback: letzte passende BUY Order laden
                    from core.utils import load_json_file
                    history = load_json_file("data/order_history.json")
                    from core.order import find_last_matching_buy
                    buy_entry = find_last_matching_buy(trade["symbol"], trade["quantity"], history)
                    if buy_entry:
                        entry_price = buy_entry.get("price", 0)
                exit_price = trade.get("price", 0)
                if entry_price and exit_price:
                    abs_pnl = (exit_price - entry_price) * trade.get("quantity", 0)
                    pct_pnl = ((exit_price - entry_price) / entry_price) * 100
                    trade["pnl"] = round(pct_pnl, 2)
                    trade["pnl_usdt"] = round(abs_pnl, 4)
            except Exception as e:
                from core.logger import log_error
                log_error(f"❌ Fehler bei PNL-Berechnung in record_order: {e}")

        log_trade_to_json(trade)
    except Exception as e:
        log_error(f"❌ Fehler in record_order(): {e}")


def round_down_quantity(quantity, step_size):
    return (quantity // step_size) * step_size

def log_trade_to_json(trade_data):
    # Ensures trade_data has the required fields and appends to JSON list file, preventing duplicates.
    from core.utils import load_json_file, save_json_file
    # Only include required fields for the trade log
    trade_id = trade_data.get("id")
    if not trade_id:
        # Skip entry if 'id' is missing
        from core.logger import log_info
        log_info(f"⚠️ Kein 'id'-Feld vorhanden, Trade wird übersprungen: {trade_data}")
        return
    side_val = trade_data.get("side")
    if side_val is not None:
        side_val = str(side_val).upper()
    trade = {
        "id": trade_id,
        "symbol": trade_data.get("symbol"),
        "side": side_val,
        "quantity": trade_data.get("quantity"),
        "price": trade_data.get("price"),
        "timestamp": trade_data.get("timestamp"),
        "mode": trade_data.get("mode"),
    }
    # Optionally include entry_reason if present
    if "entry_reason" in trade_data:
        trade["entry_reason"] = trade_data["entry_reason"]
    # Load current order history
    order_history = load_json_file("data/order_history.json")
    existing_ids = {t["id"] for t in order_history if isinstance(t, dict) and "id" in t}
    from core.logger import log_info
    if trade["id"] in existing_ids:
        log_info(f"⚠️ Duplikat mit ID {trade['id']} erkannt – wird nicht gespeichert.")
        return
    from core.utils import append_to_json_file
    append_to_json_file("data/order_history.json", trade)
    log_info(f"💾 Trade gespeichert (keine Duplikate): {trade['id']}")

def place_market_order_live(pair, side, quantity=None, price=None, position_manager=None, entry_reason: str = None):
    try:
        from config.config import get_config
        from core.kucoin_api import KuCoinClientWrapper
        client = KuCoinClientWrapper()
        trade_client = client.trade
        market_client = client.market
        from core import wallet

        base, quote = pair.split("-")

        try:
            all_symbols = market_client.get_symbol_list()
        except requests.exceptions.ReadTimeout as e:
            log_error(f"❌ Timeout bei KuCoin API – Symbol-Liste konnte nicht geladen werden: {e}")
            from core.telegram_utils import send_telegram_message
            send_telegram_message(f"❌ KuCoin TIMEOUT bei get_symbol_list() für {pair}")
            return None
        symbol_info = next((item for item in all_symbols if item['symbol'] == pair), None)
        if symbol_info is None:
            log_error(f"❌ Symbol-Info für {pair} nicht gefunden.")
            return None
        step_size = float(symbol_info['baseIncrement'])
        min_size = float(symbol_info['baseMinSize'])

        try:
            account_data = wallet_instance.api.get_account_list()
        except Exception as e:
            log_error(f"❌ Fehler beim Abrufen der Kontenliste: {e}")
            account_data = []
        quote_balance = next((acc for acc in account_data if acc["currency"] == quote and acc["type"] == "trade"), None)
        base_balance = next((acc for acc in account_data if acc["currency"] == base and acc["type"] == "trade"), None)

        if quote_balance is None:
            log_error(f"❌ Kein Trade-Konto für Quote-Währung {quote} gefunden.")
            return None
        if base_balance is None:
            base_balance = {"available": "0.0"}

        fee_rate = float(get_config("FEE_RATE", 0.001))
        max_trade_usdt = float(get_config("MAX_TRADE_USDT", 9999))
        min_order_value = float(get_config("MIN_ORDER_VALUE_USDT", 5))
        position_size_percent_str = os.getenv("POSITION_SIZE_PERCENT", None)

        if side == "buy":
            available_funds = float(quote_balance["available"])

            if position_size_percent_str is not None:
                position_size_percent = float(position_size_percent_str) / 100.0
                trade_value = min(available_funds * position_size_percent, max_trade_usdt)
            else:
                trade_value = min(available_funds, max_trade_usdt)

            if trade_value < min_order_value:
                log_error(f"❌ Trade-Wert {trade_value:.2f} USDT liegt unter Mindestgrenze ({min_order_value} USDT)")
                return None

            if not price:
                ticker = market_client.get_ticker(pair)
                price = float(ticker["price"])

            quantity = (trade_value / price) * (1 - fee_rate)

        elif side == "sell":
            # --- Neue SELL-Order-Mengenberechnung laut Vorgabe ---
            if side.lower() == "sell":
                if position_manager is None:
                    # Auto-initialize a LIVE PositionManager if caller didn't provide one
                    from core.position import PositionManager as _PM
                    position_manager = _PM(mode="LIVE")
                    log_debug("ℹ️ position_manager fehlte im LIVE-Sell-Path – wurde automatisch initialisiert.")
                # Zugriff auf Wallet-Instanz
                from core import wallet as wallet_mod
                position = position_manager.get_open_position(pair)
                import logging
                logger = logging.getLogger()
                if not position:
                    logger.warning(f"⚠️ Kein aktiver Trade für {pair} vorhanden – SELL wird übersprungen.")
                    return
                position_qty = float(position["quantity"])
                available_qty = float(wallet_mod.wallet_instance.get_available_balance(base))
                quantity = min(position_qty, available_qty)
                quantity -= 0.0005  # Kleine Sicherheitsmarge zur Vermeidung von "Balance insufficient"
                quantity = max(quantity, 0)
                quantity = round(quantity, 6)
                log_debug(f"🔎 Angepasste SELL-Menge für {pair}: {quantity}")
                if quantity <= 0:
                    raise ValueError("❌ Zu geringe verfügbare Menge für SELL-Order")

            min_trade_qty = float(get_config("MIN_TRADE_QUANTITY", 0.0002))
            if quantity < min_trade_qty:
                log_error(f"❌ Nicht genug {base} zum Verkaufen. Verfügbar: {quantity:.8f}, benötigt > {min_trade_qty}")
                return None

        else:
            log_error(f"❌ Ungültiger Order-Typ: {side}")
            return None

        quantity = (quantity // step_size) * step_size
        quantity = round(quantity, 8)

        if quantity < min_size:
            log_error(f"❌ Ordergröße {quantity} zu klein für {pair} (min {min_size})")
            return None

        if DEBUG_MODE:
            log_info(f"📦 Platzierung LIVE-Order: {side.upper()} {quantity} {pair} ...")
        else:
            log_info(f"📦 Platzierung LIVE-Order: {side.upper()} {quantity} {pair} ...")
        if entry_reason:
            if DEBUG_MODE:
                log_info(f"📄 Entry-Grund: {entry_reason}")
            else:
                log_info(f"📄 Entry-Grund: {entry_reason}")
        if DEBUG_MODE:
            log_info(f"Orderdetails vor Ausführung: pair={pair}, side={side}, qty={quantity}, price={price}")

        try:
            response = run_with_timeout(
                trade_client.create_market_order,
                args=(pair, side),
                kwargs={"size": quantity},
                timeout=10
            )
        except TimeoutError as e:
            log_error(f"❌ TIMEOUT bei KuCoin-Order: {e}")
            from core.telegram_utils import send_telegram_message
            send_telegram_message(f"❌ TIMEOUT bei Order {side.upper()} {pair}")
            return None

        if not response or "orderId" not in response:
            log_error(f"❌ Ungültige API-Antwort: {response}")
            return None

        order_id = response["orderId"]
        order_details = {}
        if order_id:
            # Retry a few times to allow KuCoin to populate deal fields
            for _i in range(3):
                try:
                    od = trade_client.get_order_details(order_id)
                    if isinstance(od, dict):
                        order_details = od
                    # Break if we have any fills or a known status
                    deal_size_tmp = float((order_details.get("dealSize") or 0) or 0)
                    status_tmp = str(order_details.get("status") or "").lower()
                    if deal_size_tmp > 0 or status_tmp in ("done", "filled", "success", "finished"):
                        break
                except Exception as e:
                    log_error(f"⚠️ Konnte Orderdetails nicht laden (BUY): {e}")
                time.sleep(0.5)
        else:
            log_error("❌ Keine gültige Order-ID erhalten.")
            order_details = {}

        # Build initial order dict with safe fallbacks
        order = {
            "id": order_id,
            "timestamp": int(time.time()),
            "mode": "LIVE",
            "symbol": pair,
            "side": side.upper(),
            "quantity": float(quantity),
            "price": float(price) if price is not None else None,
            "fee": 0.0,
        }

        # Try to update with actual fee and deal price once available
        try:
            fee_raw = order_details.get("fee") if isinstance(order_details, dict) else None
            if fee_raw is not None:
                order["fee"] = round(float(fee_raw), 8)
            deal_size = float((order_details.get("dealSize") or 0) or 0) if isinstance(order_details, dict) else 0.0
            deal_funds = float((order_details.get("dealFunds") or 0) or 0) if isinstance(order_details, dict) else 0.0
            if deal_size > 0 and deal_funds > 0:
                order["price"] = round(deal_funds / deal_size, 8)
        except Exception as e:
            log_error(f"⚠️ Fehler beim Verarbeiten der Orderdetails (BUY): {e}")

        # Berechne SL/TP und PNL für den Trade
        try:
            entry_price_val = order.get("price") if side == "buy" else None
            from strategies.atr import calculate_atr
            atr_val = None
            try:
                try:
                    kline_data = market_client.get_kline(pair, "15min")
                    if isinstance(kline_data, str):
                        import json
                        kline_data = json.loads(kline_data)
                    if not isinstance(kline_data, list):
                        raise ValueError(f"Ungültiges Kline-Format: {type(kline_data)}")
                except Exception as e:
                    log_error(f"❌ Fehler beim Laden von Klines für ATR (angepasst): {e}")
                    kline_data = []
                import pandas as pd
                df = pd.DataFrame(kline_data, columns=["time","open","close","high","low","volume","turnover"])
                atr_val = calculate_atr(df)
            except Exception as e:
                log_error(f"❌ Fehler beim Laden von Klines für ATR: {e}")
                atr_val = None
            if entry_price_val is not None:
                if USE_ATR_STOP and atr_val is not None and atr_val > 0:
                    sl_val = round(entry_price_val - atr_val * ATR_MULTIPLIER_SL, 6)
                    tp_val = round(entry_price_val + atr_val * ATR_MULTIPLIER_TP, 6)
                else:
                    sl_offset = float(get_config("TRAILING_SL_OFFSET", 0.005))
                    tp_offset = float(get_config("TRAILING_TP_OFFSET", 0.02))
                    sl_val = round(entry_price_val * (1 - sl_offset), 6)
                    tp_val = round(entry_price_val * (1 + tp_offset), 6)
                    # --- Mindest-SL/TP-Offsets, falls ATR zu klein oder nicht vorhanden ---
                    min_sl_offset = float(get_config("MIN_SL_OFFSET", 0.01))
                    min_tp_offset = float(get_config("MIN_TP_OFFSET", 0.03))
                    sl_val = min(sl_val, entry_price_val * (1 - min_sl_offset))
                    tp_val = max(tp_val, entry_price_val * (1 + min_tp_offset))
            else:
                sl_val = None
                tp_val = None
            order["entry_price"] = entry_price_val
            order["sl"] = sl_val
            order["tp"] = tp_val
            order["pnl"] = 0.0
            order["reason"] = entry_reason or "Manual/Signal Sell" if side == "sell" else entry_reason
            # Trailing Stop-Loss aktivieren, falls konfiguriert
            trailing_stop_enabled = get_config("TRAILING_STOP", "False").lower() == "true"
            trailing_offset = float(get_config("TRAILING_SL_OFFSET", 0.005))
            if trailing_stop_enabled and entry_price_val:
                order["trailing_sl"] = round(entry_price_val * (1 - trailing_offset), 6)
            else:
                order["trailing_sl"] = None
        except Exception as e:
            log_error(f"❌ Fehler bei SL/TP/PNL-Berechnung: {e}")

        from core.position import PositionManager
        position_manager = PositionManager(mode="LIVE")
        try:
            if side == "buy":
                # Pass entry_fee from order dict if available, use order["price"] if present, else fallback to price param
                entry_fee = order["fee"] if order.get("fee") is not None else 0.0
                if DEBUG_MODE:
                    log_info(f"✅ Übergabe an save_position: fee={order.get('fee')}, entry_fee={entry_fee}")
                position_manager.open(pair, quantity, order.get("price", price), fee=order.get("fee"), entry_fee=entry_fee)
                # Telegram für BUY: Channel + Private
                try:
                    from core.telegram_utils import send_telegram_message
                    entry_price = order.get("price", None)
                    if entry_price is None and price is not None:
                        entry_price = float(price)
                    entry_str = f"{entry_price:.6f}" if entry_price is not None else "?"
                    price_str = f"{entry_price:.5f}" if entry_price is not None else "?"
                    fee_str = f"{order.get('fee', '?'):.6f}" if order.get('fee') is not None else "?"
                    # Calculate SL/TP using ATR fallback (reuse PaperOrder logic)
                    atr_val = None
                    try:
                        try:
                            kline_data = market_client.get_kline(pair, "15min")
                            if isinstance(kline_data, str):
                                import json
                                kline_data = json.loads(kline_data)
                            if not isinstance(kline_data, list):
                                raise ValueError(f"Ungültiges Kline-Format: {type(kline_data)}")
                        except Exception as e:
                            log_error(f"❌ Fehler beim Laden von Klines für ATR (angepasst): {e}")
                            kline_data = []
                        import pandas as pd
                        df = pd.DataFrame(kline_data, columns=["time","open","close","high","low","volume","turnover"])
                        atr_val = calculate_atr(df)
                    except Exception as e:
                        log_error(f"❌ Fehler beim Laden von Klines für ATR: {e}")
                        atr_val = None
                    if atr_val is None or atr_val <= 0:
                        sl_val = round(entry_price * (1 - 0.05), 6)
                        tp_val = round(entry_price * (1 + 0.12), 6)
                    else:
                        sl_val = round(entry_price - atr_val * float(get_config("ATR_MULTIPLIER_SL", 1.5)), 6)
                        tp_val = round(entry_price + atr_val * float(get_config("ATR_MULTIPLIER_TP", 3.0)), 6)
                    send_telegram_message(
                        f"🟢 LIVE-ORDER\n"
                        f"➡️ BUY {pair}\n"
                        f"💰 Preis: {price_str}\n"
                        f"📦 Menge: {quantity:.6f}\n"
                        f"🕒 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(order['timestamp']))}\n"
                        f"🛑 Stop Loss: {sl_val}\n"
                        f"🎯 Take Profit: {tp_val}\n",
                        to_channel=True,
                        to_private=True
                    )
                except Exception as e:
                    log_error(f"❌ Fehler bei Telegram nach BUY: {str(e)}")
            elif side == "sell":
                # --- Fetch last BUY details for SELL ---
                last_buy = None
                try:
                    from core.utils import load_json_file
                    history = load_json_file("data/order_history.json")
                    last_buy = find_last_matching_buy(pair, quantity, history)
                except Exception as e:
                    log_error(f"❌ Fehler beim Laden der letzten BUY-Order: {e}")
                # Use position_manager first, fallback to last_buy
                position = None
                try:
                    position = position_manager.get_open_position(pair)
                except Exception as e:
                    log_error(f"❌ Fehler beim Abrufen der offenen Position: {e}")
                    position = None
                entry_price = None
                sl_val = None
                tp_val = None
                if position:
                    entry_price = position.get("entry_price", None)
                    sl_val = position.get("sl", None)
                    tp_val = position.get("tp", None)
                if (entry_price is None or entry_price == 0) and last_buy:
                    entry_price = last_buy.get("price", None)
                    sl_val = last_buy.get("sl", None)
                    tp_val = last_buy.get("tp", None)
                # If still None, calculate fallback SL/TP
                if entry_price not in [None, 0] and (sl_val is None or tp_val is None):
                    atr_val = None
                    try:
                        try:
                            kline_data = market_client.get_kline(pair, "15min")
                            if isinstance(kline_data, str):
                                import json
                                kline_data = json.loads(kline_data)
                            if not isinstance(kline_data, list):
                                raise ValueError(f"Ungültiges Kline-Format: {type(kline_data)}")
                        except Exception as e:
                            log_error(f"❌ Fehler beim Laden von Klines für ATR (angepasst): {e}")
                            kline_data = []
                        import pandas as pd
                        df = pd.DataFrame(kline_data, columns=["time","open","close","high","low","volume","turnover"])
                        atr_val = calculate_atr(df)
                    except Exception as e:
                        log_error(f"❌ Fehler beim Laden von Klines für ATR: {e}")
                        atr_val = None
                    if atr_val is None or atr_val <= 0:
                        sl_val = round(entry_price * (1 - 0.05), 6)
                        tp_val = round(entry_price * (1 + 0.12), 6)
                    else:
                        sl_val = round(entry_price - atr_val * float(get_config("ATR_MULTIPLIER_SL", 1.5)), 6)
                        tp_val = round(entry_price + atr_val * float(get_config("ATR_MULTIPLIER_TP", 3.0)), 6)
                order["entry_price"] = entry_price
                order["sl"] = sl_val
                order["tp"] = tp_val
                # --- Prepare values for safe usage ---
                price_val = order.get("price", None)
                fee_val = order.get("fee", None)
                # Defensive fallback: handle None and wrong types
                def safe_float(val, default=0.0):
                    try:
                        if val is None:
                            return default
                        return float(val)
                    except Exception:
                        return default
                price_val = safe_float(price_val, None)
                fee_val = safe_float(fee_val, None)
                entry_price_val = safe_float(entry_price, None)
                # --- Profit Calculation with fallback ---
                profit_str = "nicht berechenbar"
                abs_pnl = None
                pct_pnl = None
                try:
                    if entry_price_val not in [None, 0] and price_val not in [None, 0]:
                        abs_pnl = (price_val - entry_price_val) * quantity
                        pct_pnl = ((price_val - entry_price_val) / entry_price_val) * 100
                        order["pnl"] = round(pct_pnl, 2)
                        order["pnl_usdt"] = round(abs_pnl, 4)
                        profit_str = f"{pct_pnl:+.2f}%"
                except Exception as e:
                    log_error(f"❌ Fehler bei Profit-Berechnung: {e}")
                    profit_str = "nicht berechenbar"
                # Format strings with fallback/defaults
                try:
                    price_str = f"{price_val:.5f}" if price_val not in [None, 0] else "?"
                except Exception:
                    price_str = "?"
                try:
                    fee_str = f"{fee_val:.6f}" if fee_val not in [None, 0] else "?"
                except Exception:
                    fee_str = "?"
                try:
                    entry_str = f"{entry_price_val:.6f}" if entry_price_val not in [None, 0] else "nicht verfügbar"
                except Exception:
                    entry_str = "nicht verfügbar"
                # Ensure SL/TP are present in SELL notifications; fallback to min offsets if missing
                if (sl_val in (None, 0) or tp_val in (None, 0)) and entry_price_val not in (None, 0):
                    min_sl_offset = float(get_config("MIN_SL_OFFSET", 0.01))
                    min_tp_offset = float(get_config("MIN_TP_OFFSET", 0.03))
                    if sl_val in (None, 0):
                        sl_val = round(entry_price_val * (1 - min_sl_offset), 6)
                    if tp_val in (None, 0):
                        tp_val = round(entry_price_val * (1 + min_tp_offset), 6)
                # Format SL/TP as string with fallback
                try:
                    sl_val_str = f"{sl_val:.6f}" if sl_val not in [None, 0] else "?"
                except Exception:
                    sl_val_str = "?"
                try:
                    tp_val_str = f"{tp_val:.6f}" if tp_val not in [None, 0] else "?"
                except Exception:
                    tp_val_str = "?"
                from core.telegram_utils import send_telegram_message
                try:
                    send_telegram_message(
                        f"🔴 SELL Entry\n"
                        f"Symbol: {pair}\n"
                        f"Menge: {quantity:.6f}\n"
                        f"Preis: {price_str}\n"
                        f"Gebühr: {fee_str}\n"
                        f"Entry: {entry_str}\n"
                        f"Profit: {profit_str} | MODE: LIVE\n"
                        f"🛑 Stop Loss: {sl_val_str}\n"
                        f"🎯 Take Profit: {tp_val_str}\n",
                        to_channel=True,
                        to_private=True
                    )
                except Exception as e:
                    log_error(f"❌ Fehler beim Senden der Telegram SELL-Nachricht: {e}")
                # Teilverkäufe vorbereiten
                partial_sells_enabled = get_config("ENABLE_PARTIAL_SELLS", "False").lower() == "true"
                partial_sell_percent = float(get_config("PARTIAL_SELL_PERCENT", 0.5))
                partial_sell_profit_threshold = float(get_config("PARTIAL_SELL_PROFIT_THRESHOLD", 0.01))
                if partial_sells_enabled and entry_price and price_val:
                    profit_ratio = (price_val - entry_price) / entry_price
                    if profit_ratio >= partial_sell_profit_threshold:
                        partial_qty = round(quantity * partial_sell_percent, 6)
                        if partial_qty > 0:
                            try:
                                if DEBUG_MODE:
                                    log_info(f"📉 Teilverkauf ausgelöst: {partial_qty} {pair} bei {price_val}")
                                trade_client.create_market_order(pair, "sell", size=partial_qty)
                                send_telegram_message(
                                    f"📉 Teilverkauf ausgelöst\n"
                                    f"Symbol: {pair}\n"
                                    f"Menge: {partial_qty}\n"
                                    f"Preis: {price_val}\n"
                                    f"Gewinn: {profit_ratio*100:.2f}%\n",
                                    to_channel=True,
                                    to_private=True
                                )
                            except Exception as e:
                                log_error(f"❌ Fehler bei Teilverkauf: {e}")
                position_manager.close(pair)
                if DEBUG_MODE:
                    log_info(f"📁 Position in position_live.json für {pair} wurde geschlossen.")
        except Exception as e:
            log_error(f"❌ Fehler beim Aktualisieren der Position für {pair}: {str(e)}")
            from core.telegram_utils import send_telegram_message
            send_telegram_message(f"❌ Fehler beim Schreiben in position_live.json für {pair}: {str(e)}")

        # Try to get the latest position for this symbol, if possible
        position = None
        try:
            from core.position import PositionManager
            pm = PositionManager(mode="LIVE")
            position = pm.get_open_position(pair)
        except Exception:
            position = None
        # --- Ensure essential fields are present before persisting ---
        order["symbol"] = order.get("symbol") or pair
        order["side"] = (order.get("side") or side).upper()
        if order.get("price") is None and price is not None:
            order["price"] = float(price)
        if order.get("quantity") is None:
            order["quantity"] = float(quantity)
        # Ensure order dict retains entry_price, sl, tp, pnl, and trailing_sl before record_order
        order["entry_price"] = order.get("entry_price") or order.get("price")
        order["trailing_sl"] = order.get("trailing_sl")
        record_order(order, position=position)
        from core.performance import log_trade
        log_trade(order)
        try:
            if side in ["buy", "sell"]:
                notify_live_balance()
        except Exception as e:
            log_error(f"❌ Fehler in notify_live_balance: {str(e)}")

        log_info(f"✅ LIVE-Order ausgeführt: {side.upper()} {quantity:.6f} {pair} (Fee: {order['fee']})")
        
        try:
            auto_backup()
        except Exception as e:
            log_error(f"❌ Fehler beim automatischen Backup nach Order: {e}")
        return order

    except Exception as e:
        import traceback
        log_error("❌ KuCoin API Fehler:")
        log_error(f"Typ: {type(e)}")
        log_error(f"Inhalt: {str(e)}")
        log_error(traceback.format_exc())
        from core.telegram_utils import send_telegram_message
        send_telegram_message(f"❌ Order-Fehler ({side.upper()} {pair}): {str(e)}")
        return None

import logging
# Entfernt: append_to_order_history wird nicht mehr verwendet, Logging nur über log_trade_to_json
from core.utils import price_cache
# Add LIVE_MODE definition at the top of the file
from config.config import MODE
LIVE_MODE = MODE == "LIVE"

# Ensure import for calculate_atr at the top (for clarity)
from strategies.atr import calculate_atr

def place_order(symbol: str, side: str, quantity: float, price: float = None, mode: str = "LIVE") -> dict:
    """
    Führt eine Order im LIVE-Modus aus.
    """
    logger = logging.getLogger()
    if DEBUG_MODE:
        logger.info(f"📝 Neue Order: {side} {quantity} {symbol} @ {price} ({mode})")

    # LIVE-Prüfung: keine neue Order, falls bereits offene Position (nur im LIVE-Modus)
    if mode == "LIVE":
        from core.position import PositionManager
        position_manager = PositionManager(mode)
        if position_manager.has_open_position(symbol):
            if DEBUG_MODE:
                logger.info(f"⛔️ Abbruch: Bereits offene Position für {symbol} im LIVE-Modus – keine neue Order.")
            return None

    order = None
    order_id = None

    if LIVE_MODE:
        # --- LIVE-Modus: Positionsgröße für BUY ---
        if side.lower() == "buy":
            dynamic_enabled = get_config("DYNAMIC_POSITION_SIZING", "False").lower() == "true"
            if dynamic_enabled:
                quantity = get_dynamic_position_size(
                    symbol,
                    risk_percent=float(get_config("MAX_TRADE_RISK", 1.0)) * 100,
                    min_position=0.1
                )
                if DEBUG_MODE:
                    logger.info(f"📊 Dynamische Ordergröße für {symbol}: {quantity:.4f}")
            else:
                quantity = calculate_position_size(symbol, percent=float(get_config("FIXED_TRADE_PERCENT", 5.0))) or quantity
                if DEBUG_MODE:
                    logger.info(f"📊 Feste Ordergröße für {symbol}: {quantity:.4f}")
                # --- Neue Begrenzung für maximale Ordergröße ---
            max_trade_value = float(get_config("MAX_TRADE_USDT", 500))  # Maximaler Wert in USDT
            if price is None:
                from core.kucoin_api import KuCoinClientWrapper
                market_client = KuCoinClientWrapper().market
                ticker = market_client.get_ticker(symbol)
                price = float(ticker["price"])
            max_quantity = max_trade_value / price
            if quantity > max_quantity:
                if DEBUG_MODE:
                    logger.info(f"⚠️ Ordergröße {quantity:.4f} überschreitet Maximalwert, auf {max_quantity:.4f} reduziert.")
                quantity = max_quantity
        # Live order execution
        if side.lower() == "sell":
            from core.position import PositionManager
            position_manager = PositionManager(mode="LIVE")
            order_handler = OrderHandler(mode=MODE)
            order = place_market_order_live(symbol, side, quantity, position_manager=position_manager)
        else:
            order_handler = OrderHandler(mode=MODE)
            order = place_market_order_live(symbol, side, quantity)
        if order and "id" in order:
            order_id = order["id"]
        # --- Save to positions_live.json using merge_position_live ---
        if order is not None and order_id is not None:
            if side.lower() == "buy":
                fee = float(order["fee"]) if order.get("fee") is not None else 0.0
                timestamp = order.get("timestamp", int(time.time()))
                merge_position_live(
                    symbol=symbol,
                    quantity=order.get("quantity", 0),
                    price=order.get("price", 0),
                    fee=fee,
                    timestamp=timestamp,
                    sl=order.get("sl"),
                    tp=order.get("tp")
                )
    return order

# Korrigierte Version: Speichert Order-History als Liste von Dicts in order_history.json
# Funktion bleibt als Dummy bestehen, Aufrufe werden entfernt, record_order erledigt Logging
def log_order_history(order: dict):
    pass
# Add merge_position_live at the end of the file

def merge_position_live(symbol, quantity, price, fee, timestamp, sl=None, tp=None):
    import uuid
    from core.utils import load_json_file, save_json_file
    # LIVE_POSITIONS_FILE importiert aus config.config (muss ein Pfad/str sein)
    from config.config import LIVE_POSITIONS_FILE
    from pathlib import Path
    from core.logger import log_error
    if not isinstance(LIVE_POSITIONS_FILE, (str, Path)):
        log_error(f"❌ Ungültiger Dateipfad in merge_position_live: {type(LIVE_POSITIONS_FILE)}")
        return

    positions = load_json_file(LIVE_POSITIONS_FILE)
    # Falls eine Liste zurückgegeben wird, in ein Dict konvertieren
    if isinstance(positions, list):
        positions = {f"pos_{i}": p for i, p in enumerate(positions) if isinstance(p, dict)}
    if not isinstance(positions, dict):
        positions = {}

    existing_position_key = None
    for key, pos in positions.items():
        if pos.get("symbol") == symbol and pos.get("side") == "buy":
            existing_position_key = key
            break

    if existing_position_key:
        pos = positions[existing_position_key]
        old_qty = pos["quantity"]
        old_price = pos["entry_price"]
        new_qty = old_qty + quantity
        new_price = ((old_qty * old_price) + (quantity * price)) / new_qty
        pos["quantity"] = round(new_qty, 8)
        pos["entry_price"] = round(new_price, 8)
        pos["timestamp"] = timestamp
        pos["fee"] = round(pos.get("fee", 0.0) + fee, 8)
        # Add/update SL/TP if provided
        if sl is not None:
            pos["sl"] = round(sl, 8)
        if tp is not None:
            pos["tp"] = round(tp, 8)
        positions[existing_position_key] = pos
    else:
        key = f"{symbol}__{timestamp}__{uuid.uuid4().hex[:6]}"
        positions[key] = {
            "symbol": symbol,
            "pair": symbol,
            "quantity": round(quantity, 8),
            "entry_price": round(price, 8),
            "side": "buy",
            "fee": round(fee, 8),
            "entry_fee": round(fee, 8),
            "timestamp": timestamp
        }
        if sl is not None:
            positions[key]["sl"] = round(sl, 8)
        if tp is not None:
            positions[key]["tp"] = round(tp, 8)

    # Korrigierter Aufruf: Dateipfad als erstes Argument, Daten als zweites
    save_json_file(str(LIVE_POSITIONS_FILE), positions)
# Hilfsfunktion: Suche letzten passenden BUY-Eintrag aus der Orderhistorie
def find_last_matching_buy(symbol, quantity, history, tolerance=0.0001):
    for entry in reversed(history):
        if (
            entry.get("symbol") == symbol and
            entry.get("side") == "BUY" and
            abs(entry.get("quantity", 0) - quantity) < tolerance
        ):
            return entry
    return None