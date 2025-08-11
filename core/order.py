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
        # Delegates to place_market_order_live(), which calls send_order_prepared() internally
        if position_manager is None:
            position_manager = getattr(self, "position_manager", None)
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
            # Ensure BUY has non-None SL/TP defaults to avoid 'None' in Telegram/history
            if order_side_up == "BUY" and order_local.get("entry_price"):
                try:
                    ep_tmp = float(order_local["entry_price"])
                    min_sl_offset = float(get_config("MIN_SL_OFFSET", 0.01))
                    min_tp_offset = float(get_config("MIN_TP_OFFSET", 0.03))
                    order_local["sl"] = round(ep_tmp * (1 - min_sl_offset), 6)
                    order_local["tp"] = round(ep_tmp * (1 + min_tp_offset), 6)
                except Exception:
                    pass

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

            # --- BUY fee backfill & avg fill price from fills (if fee still 0.0) ---
            try:
                if order_side_up == "BUY" and float(order_local.get("fee") or 0.0) == 0.0 and exch_id:
                    trade_client = getattr(api, "trade", None)
                    if trade_client and hasattr(trade_client, "get_fills"):
                        fills_resp = None
                        try:
                            fills_resp = trade_client.get_fills(orderId=exch_id)
                        except TypeError:
                            try:
                                fills_resp = trade_client.get_fills(order_id=exch_id)
                            except Exception:
                                fills_resp = None
                        fills_list = []
                        if isinstance(fills_resp, dict):
                            if isinstance(fills_resp.get("items"), list):
                                fills_list = fills_resp["items"]
                            elif isinstance(fills_resp.get("data"), list):
                                fills_list = fills_resp["data"]
                        elif isinstance(fills_resp, list):
                            fills_list = fills_resp
                        total_fee_sum = 0.0
                        total_fill_size = 0.0
                        total_fill_funds = 0.0
                        for fill in fills_list:
                            try:
                                total_fee_sum += float(fill.get("fee") or 0.0)
                                sz = float(fill.get("size") or fill.get("dealSize") or 0.0)
                                funds = float(fill.get("funds") or fill.get("dealFunds") or 0.0)
                                if sz > 0:
                                    total_fill_size += sz
                                if funds > 0:
                                    total_fill_funds += funds
                            except Exception:
                                continue
                        if total_fee_sum > 0:
                            order_local["fee"] = round(total_fee_sum, 8)
                        if total_fill_size > 0 and total_fill_funds > 0:
                            avg_px = round(total_fill_funds / total_fill_size, 8)
                            order_local["price"] = avg_px
                            if order_local.get("entry_price"):
                                order_local["entry_price"] = avg_px
            except Exception as _bf:
                log_error(f"⚠️ BUY Fee-Backfill fehlgeschlagen: {_bf}")


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
                    # --- Enrich SELL with entry/sl/tp from current LIVE position for proper Telegram + history ---
                    entry_price_val = None
                    sl_enriched = None
                    tp_enriched = None
                    p = None
                    try:
                        from core.position import PositionManager
                        pm_tmp = PositionManager(mode="LIVE")
                        data_tmp = pm_tmp.load_positions()
                        for pid, p_ in data_tmp.items():
                            if p_.get("symbol") == symbol or str(pid).startswith(f"{symbol}__"):
                                entry_price_val = p_.get("entry_price", None)
                                sl_enriched = p_.get("sl", None)
                                tp_enriched = p_.get("tp", None)
                                p = p_
                                break
                    except Exception:
                        pass
                    # Fallbacks if still None and we have a BUY price earlier in the session
                    if order_local.get("entry_price") in (None, 0):
                        order_local["entry_price"] = entry_price_val
                    if order_local.get("sl") in (None, 0):
                        order_local["sl"] = sl_enriched
                    if order_local.get("tp") in (None, 0):
                        order_local["tp"] = tp_enriched
                    # Build display helpers
                    try:
                        entry_str = f"{float(order_local.get('entry_price')):.6f}" if order_local.get("entry_price") not in (None, 0) else "nicht verfügbar"
                    except Exception:
                        entry_str = "nicht verfügbar"
                    try:
                        sl_val_str = f"{float(order_local.get('sl')):.6f}" if order_local.get("sl") not in (None, 0) else "-"
                    except Exception:
                        sl_val_str = "-"
                    try:
                        tp_val_str = f"{float(order_local.get('tp')):.6f}" if order_local.get("tp") not in (None, 0) else "-"
                    except Exception:
                        tp_val_str = "-"
                    fee_val = order_local.get("fee")
                    try:
                        fee_str = f"{float(fee_val):.6f}" if fee_val is not None else "?"
                    except Exception:
                        fee_str = str(fee_val) if fee_val is not None else "?"
                    # Compute PnL (percent and USDT) for Telegram & history
                    pnl_pct_str = "n/a"
                    pnl_usdt_str = ""
                    try:
                        exec_price = float(order_local.get("price") or 0)
                        entry_px = float(order_local.get("entry_price") or 0)
                        sell_fee = float(order_local.get("fee") or 0)
                        # Try to read entry_fee from positions
                        entry_fee_val = 0.0
                        try:
                            entry_fee_val = float(p.get("entry_fee") or p.get("fee") or 0.0) if p else 0.0
                        except Exception:
                            entry_fee_val = 0.0
                        if exec_price > 0 and entry_px > 0 and qty_val is not None:
                            pnl_abs = (exec_price - entry_px) * float(qty_val) - (sell_fee + entry_fee_val)
                            pnl_pct = ((exec_price - entry_px) / entry_px) * 100.0
                            order_local["pnl"] = round(pnl_pct, 4)
                            order_local["pnl_usdt"] = round(pnl_abs, 6)
                            sign = "+" if pnl_abs >= 0 else ""
                            pnl_pct_str = f"{pnl_pct:.2f}%"
                            pnl_usdt_str = f" ({sign}{pnl_abs:.6f} USDT)"
                    except Exception:
                        pass
                    send_telegram_message(
                        f"🔴 LIVE-ORDER\n"
                        f"➡️ SELL {symbol}\n"
                        f"💰 Preis: {price_str}\n"
                        f"📦 Menge: {qty_val:.6f}\n"
                        f"💸 Gebühr: {fee_str}\n"
                        f"🕒 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts_val))}\n"
                        f"🎯 Entry: {entry_str}\n"
                        f"📈 P&L: {pnl_pct_str}{pnl_usdt_str}\n"
                        f"🛑 Stop Loss: {sl_val_str}\n"
                        f"🎯 Take Profit: {tp_val_str}\n",
                        to_channel=True,
                        to_private=True
                    )
                # Persist AFTER Telegram prep so SELL has entry/sl/tp filled
                record_order(order_local)
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
    # --- Backfill for SELL: ensure entry_price/sl/tp present ---
    try:
        if isinstance(order, dict) and str(order.get("side", "")).upper() == "SELL":
            if not order.get("entry_price") or order.get("sl") is None or order.get("tp") is None:
                from core.position import PositionManager
                pm_bf = PositionManager(mode="LIVE")
                data_bf = pm_bf.load_positions()
                sym_bf = order.get("symbol")
                for pid_bf, p_bf in data_bf.items():
                    if p_bf.get("symbol") == sym_bf or str(pid_bf).startswith(f"{sym_bf}__"):
                        order.setdefault("entry_price", p_bf.get("entry_price"))
                        if order.get("sl") is None:
                            order["sl"] = p_bf.get("sl")
                        if order.get("tp") is None:
                            order["tp"] = p_bf.get("tp")
                        break
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
        if trade["side"] == "SELL" and (trade.get("pnl") is None or trade.get("pnl_usdt") is None):
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
                    if trade.get("pnl_usdt") is None:
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
        "timestamp": trade_data.get("timestamp"),
        "mode": trade_data.get("mode"),
        "symbol": trade_data.get("symbol"),
        "side": side_val,
        "quantity": trade_data.get("quantity"),
        "price": trade_data.get("price"),
        "fee": trade_data.get("fee"),
        "entry_price": trade_data.get("entry_price"),
        "sl": trade_data.get("sl"),
        "tp": trade_data.get("tp"),
        "pnl": trade_data.get("pnl"),
        "pnl_usdt": trade_data.get("pnl_usdt"),
        "reason": trade_data.get("reason"),
    }
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
    """
    Thin wrapper to route ALL LIVE orders through send_order_prepared() to avoid duplicate logic.
    - Ensures price is available (ticker fallback)
    - Calls unified sender with order_type='market'
    - Returns the API/response dict from send_order_prepared
    """
    try:
        from core.kucoin_api import KuCoinClientWrapper
        client = KuCoinClientWrapper()
        market_client = client.market
        api = client  # pass the wrapper, which exposes trade/market methods used by send_order_prepared
        if price is None:
            try:
                tk = market_client.get_ticker(pair)
                price = float(tk["price"])
            except Exception:
                price = 0.0
        # quantity must be provided by caller (sizing handled upstream) – we keep the signature for compatibility.
        if quantity in (None, 0):
            from core.logger import log_error
            log_error(f"❌ place_market_order_live: quantity fehlt/0 für {pair} {side}")
            return None
        # Delegate to unified sender (handles rounding, minFunds/minQty, idempotency, Telegram, history, positions)
        return send_order_prepared(api, pair, side, price, quantity, strategy=entry_reason or "default", order_type="market")
    except Exception as e:
        from core.logger import log_error
        log_error(f"❌ Fehler in place_market_order_live (delegiert): {e}")
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