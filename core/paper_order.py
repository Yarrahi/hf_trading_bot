import time
import uuid
from core.logger import log_info, log_warning
from core.telegram_utils import send_telegram_message
from core.position import PositionManager
from core.utils import load_json_file, save_json_file
from strategies.atr import calculate_atr
from core.kucoin_api import KuCoinClientWrapper
from config.config import get_config
from core.paper_wallet import PaperWallet
from core.filters import prepare_order
from decimal import Decimal, ROUND_DOWN
from core.filters import filter_book

class PaperOrderHandler:
    def _ensure_history_file(self):
        """Ensure trades_file exists and is a JSON list; normalize if it's a dict/None."""
        try:
            import os
            # Create folder if missing
            folder = os.path.dirname(self.trades_file)
            if folder and not os.path.exists(folder):
                os.makedirs(folder, exist_ok=True)
            raw = load_json_file(self.trades_file)
            if raw is None:
                save_json_file(self.trades_file, [])
                log_info(f"🗃️ {self.trades_file} initialisiert (leere Liste)")
            elif isinstance(raw, dict):
                # normalize silently to list
                save_json_file(self.trades_file, [])
                log_info(f"🗃️ {self.trades_file} war dict – auf Liste [] normalisiert")
            elif not isinstance(raw, list):
                save_json_file(self.trades_file, [])
                log_info(f"🗃️ {self.trades_file} war {type(raw).__name__} – auf Liste [] normalisiert")
        except Exception as e:
            log_warning(f"⚠️ Konnte {self.trades_file} nicht normalisieren: {e}")

    def __init__(self):
        self.trades_file = "data/order_history.json"
        self.positions_file = "data/positions_paper.json"
        self.position_manager = PositionManager(mode="PAPER")
        self.wallet = PaperWallet()
        self.price_cache = {}
        self.atr_multiplier_sl = float(get_config("ATR_MULTIPLIER_SL", 1.5))
        self.atr_multiplier_tp = float(get_config("ATR_MULTIPLIER_TP", 3))
        self.atr_period = int(get_config("ATR_PERIOD", 14))
        self.atr_timeframe = get_config("ATR_TIMEFRAME", "1hour")
        self._ensure_history_file()

    def place_order(self, symbol, side, quantity, price=None, entry_reason=None, **kwargs):
        # Verwende Preis aus price_cache, falls kein Preis übergeben wurde
        # Prüfe auf offene Position bei SELL
        if side == "sell" and not self.position_manager.has_open_position(symbol):
            log_warning(f"⚠️ Keine offene Position für {symbol}, SELL-Order übersprungen.")
            return None

        if price is None:
            price = self.price_cache.get(symbol)
            if price is None:
                log_warning(f"Kein Preis verfügbar für {symbol}, Order nicht platziert.")
                return None

        # === Phase 1 parity: Precision & minFunds check ===
        try:
            qpx, qqty, err, notional = prepare_order(symbol, side, price, quantity)
            if err:
                log_warning(f"⚠️ PAPER Local reject {symbol} {side}: {err} (px={qpx}, qty={qqty}, notional={notional})")
                return {"status": "rejected_local", "reason": err, "symbol": symbol, "side": side}
            price = float(qpx)
            quantity = float(qqty)
        except Exception as e:
            log_warning(f"⚠️ PAPER: prepare_order fehlgeschlagen – verwende ungerundete Werte: {e}")

        # ATR für SL/TP berechnen
        atr_value = None
        try:
            import pandas as pd
            client = KuCoinClientWrapper()
            end_time = int(time.time())
            start_time = end_time - (self.atr_period + 2) * 3600  # Hole genug Candles
            klines = client.market.get_kline(symbol, self.atr_timeframe, startAt=start_time)
            # KuCoin returns 7 values: time, open, close, high, low, volume, turnover
            df = pd.DataFrame(
                klines,
                columns=["time", "open", "close", "high", "low", "volume", "turnover"]
            )
            # Drop turnover if not needed
            if "turnover" in df.columns:
                df = df.drop(columns=["turnover"])
            df = df.astype({
                "open": float,
                "close": float,
                "high": float,
                "low": float,
                "volume": float
            })
            log_warning(f"⚠️ Debug ATR-Daten für {symbol}: {df.head().to_dict()}")
            if len(df) < self.atr_period + 1:
                log_warning(f"⚠️ Zu wenige Candles für ATR-Berechnung ({len(df)} von {self.atr_period + 1}), ATR wird als None gesetzt.")
                atr_value = None
            else:
                atr_value = calculate_atr(df, self.atr_period)
                log_info(f"📊 ATR berechnet: {atr_value} basierend auf {len(df)} Candles.")
        except Exception as e:
            log_warning(f"⚠️ Konnte ATR nicht berechnen für {symbol}: {e}")

        if atr_value is None or atr_value < 0.0001:
            log_warning(f"⚠️ ATR konnte nicht berechnet werden oder ist zu niedrig für {symbol}, setze Fallback-SL/TP (2% / 4%).")
            fallback_sl_pct = 0.02  # 2% SL
            fallback_tp_pct = 0.04  # 4% TP
            sl = round(price * (1 - fallback_sl_pct), 6)
            tp = round(price * (1 + fallback_tp_pct), 6)
        else:
            sl = round(price - atr_value * self.atr_multiplier_sl, 6)
            tp = round(price + atr_value * self.atr_multiplier_tp, 6)

        # Simulierte Gebühr berechnen (z. B. 0.1 %)
        fee_rate = float(get_config("TRADING_FEE_RATE", 0.001))
        fee = round(quantity * price * fee_rate, 6)

        timestamp = int(time.time())
        order_id = uuid.uuid4().hex[:8]
        order = {
            "id": order_id,
            "clientOid": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "timestamp": timestamp,
            "mode": "PAPER",
            "entry_reason": entry_reason if entry_reason is not None else "N/A",
            "entry_price": price,
            "sl": sl,
            "tp": tp,
            "pnl": 0.0,
            "reason": "N/A",
            "fee": fee
        }

        # === Wallet-Update zuerst (realistische Ausführung) ===
        try:
            base, quote = symbol.split("-")
        except ValueError:
            base, quote = symbol, "USDT"

        if side == "buy":
            # Berechne leistbare Menge inkl. Fee + Headroom und floore auf Lot
            from os import getenv as _getenv
            fee_rate = float(get_config("TRADING_FEE_RATE", 0.001))
            avail_quote = Decimal(str(self.wallet.get_balance(quote)))
            price_dec = Decimal(str(price))
            headroom_bps = int(_getenv("PAPER_HEADROOM_BPS", "5"))  # 0.05% default
            headroom = (avail_quote * Decimal(headroom_bps) / Decimal(10000))
            budget = (avail_quote - headroom) if avail_quote > headroom else Decimal("0")
            denom = price_dec * (Decimal("1") + Decimal(str(fee_rate)))

            # base increment aus FilterBook
            try:
                inc = filter_book.get(symbol).base_increment
                base_inc = Decimal(str(inc)) if inc else Decimal("0.00000001")
            except Exception:
                base_inc = Decimal("0.00000001")

            # Maximale Menge und auf Increment floorn
            max_affordable = (budget / denom) if denom > 0 else Decimal("0")
            if base_inc > 0:
                steps = (max_affordable / base_inc).to_integral_value(rounding=ROUND_DOWN)
                max_affordable = steps * base_inc
                max_affordable = max_affordable.quantize(base_inc, rounding=ROUND_DOWN)

            req_qty_dec = Decimal(str(quantity))
            final_qty_dec = min(req_qty_dec, max_affordable)
            if final_qty_dec <= 0:
                log_warning(f"⚠️ PAPER: Unzureichendes {quote}-Guthaben – BUY abgebrochen.")
                return {"status": "rejected", "reason": "insufficient_funds", "symbol": symbol}

            # Precision/minFunds mit angepasster Menge erneut prüfen
            adj_qpx, adj_qqty, adj_err, adj_notional = prepare_order(symbol, side, price_dec, final_qty_dec)
            if adj_err:
                log_warning(f"⚠️ PAPER Local reject nach Budget-Anpassung {symbol} {side}: {adj_err}")
                return {"status": "rejected_local", "reason": adj_err, "symbol": symbol, "side": side}

            price = float(adj_qpx)
            quantity = float(adj_qqty)

            if not self.wallet.update_balance(base, quantity, True, price=price, quote=quote, fee_rate=fee_rate):
                log_warning(f"⚠️ PAPER: Wallet-Update nach Budget-Anpassung fehlgeschlagen – BUY abgebrochen.")
                return {"status": "rejected", "reason": "wallet_update_failed", "symbol": symbol}

            # Kosten/Fee nach finaler Menge berechnen und Order/Position speichern
            cost = price * quantity
            fee = round(cost * fee_rate, 6)
            order["quantity"] = quantity
            order["fee"] = fee
            order["funds"] = cost
            log_info(f"📄 PAPER-Order erstellt: {side} {quantity} {symbol} @ {price}")
            self.record_order(order)

            self.position_manager.save_position({
                "pair": symbol,
                "symbol": symbol,
                "side": "buy",
                "quantity": quantity,
                "entry_price": price,
                "price": price,
                "fee": 0.0,
                "entry_fee": fee,
                "sl": sl,
                "tp": tp
            })

        elif side == "sell":
            # Verfügbarkeit mit Decimal prüfen und auf Lot floorn
            from os import getenv as _getenv
            avail_base_dec = Decimal(str(self.wallet.get_balance(base)))
            if avail_base_dec <= 0:
                log_warning(f"⚠️ Keine {base}-Menge verfügbar – SELL abgebrochen.")
                return {"status": "rejected", "reason": "insufficient_base", "symbol": symbol}

            try:
                inc = filter_book.get(symbol).base_increment
                base_inc = Decimal(str(inc)) if inc else Decimal("0.00000001")
            except Exception:
                base_inc = Decimal("0.00000001")

            requested_dec = Decimal(str(quantity))
            # nutze die kleinere von angefragter Menge und verfügbarer Menge
            target_dec = requested_dec if requested_dec <= avail_base_dec else avail_base_dec
            # floor auf increment
            steps = (target_dec / base_inc).to_integral_value(rounding=ROUND_DOWN)
            sell_qty_dec = steps * base_inc
            sell_qty_dec = sell_qty_dec.quantize(base_inc, rounding=ROUND_DOWN)

            if sell_qty_dec <= 0:
                log_warning(f"⚠️ Verfügbare Menge < Lot-Size – SELL abgebrochen.")
                return {"status": "rejected", "reason": "lot_too_small", "symbol": symbol}

            sell_qty = float(sell_qty_dec)

            if not self.wallet.update_balance(base, sell_qty, False, price=price, quote=quote, fee_rate=fee_rate):
                log_warning(f"⚠️ PAPER: Wallet-Update für SELL fehlgeschlagen – abgebrochen.")
                return {"status": "rejected", "reason": "wallet_update_failed", "symbol": symbol}

            # PNL auf Basis der tatsächlich verkauften Menge berechnen
            open_pos = self.position_manager.get_open_position(symbol)
            if open_pos:
                entry_price = open_pos.get("entry_price") or open_pos.get("price") or 0.0
                entry_qty = open_pos.get("quantity") or open_pos.get("size") or 0.0
                realized_qty = min(sell_qty, entry_qty)
                pnl = round((price - entry_price) * realized_qty - (price * sell_qty * fee_rate), 6)
                order["pnl"] = pnl
                order["reason"] = "TP/SL/Signal Sell"

            # Order-Felder finalisieren und speichern
            proceeds = price * sell_qty
            fee = round(proceeds * fee_rate, 6)
            order["quantity"] = sell_qty
            order["fee"] = fee
            order["funds"] = proceeds
            log_info(f"📄 PAPER-Order erstellt: sell {sell_qty} {symbol} @ {price}")
            self.record_order(order)

            # Position aktualisieren: partial reduce vs. full close
            if open_pos:
                entry_price = open_pos.get("entry_price") or open_pos.get("price") or 0.0
                entry_qty = open_pos.get("quantity") or open_pos.get("size") or 0.0
                remaining_qty = max(0.0, float(entry_qty) - float(sell_qty))
                if remaining_qty > 1e-12:
                    # Partial reduce: speichere Restposition
                    self.position_manager.save_position({
                        "pair": symbol,
                        "symbol": symbol,
                        "side": "buy",
                        "quantity": float(f"{remaining_qty:.8f}"),
                        "entry_price": entry_price,
                        "price": price,
                        "fee": 0.0,
                        "entry_fee": open_pos.get("entry_fee", 0.0),
                        "sl": open_pos.get("sl", 0.0),
                        "tp": open_pos.get("tp", 0.0)
                    })
                else:
                    # Full close
                    self.position_manager.close(symbol)

            # setze quantity für nachfolgende Telegram-Nachricht
            quantity = sell_qty

        msg = (
            f"🧪 PAPER-ORDER\n"
            f"➡️ {side.upper()} {symbol}\n"
            f"💰 Preis: {price}\n"
            f"📦 Menge: {quantity}\n"
            f"🕒 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))}"
        )
        if entry_reason:
            msg += f"\n📌 Grund: {entry_reason}"
        if sl and tp:
            msg += f"\n🛑 Stop Loss: {sl}\n🎯 Take Profit: {tp}"
        else:
            msg += "\n🛑 Stop Loss: N/A\n🎯 Take Profit: N/A"

        # Sende Nachricht sowohl an privaten als auch öffentlichen Kanal
        send_telegram_message(msg, to_channel=True, to_private=True)

        return order

    def record_order(self, order):
        # --- Normalize id and essential fields ---
        try:
            if isinstance(order, dict):
                oid = order.get("id") or order.get("orderId") or order.get("clientOid")
                if not oid:
                    sym = order.get("symbol", "?")
                    s = order.get("side", "?")
                    oid = f"{sym}-{s}-{int(time.time()*1000)}"
                order.setdefault("id", oid)
                order.setdefault("clientOid", oid)
                order.setdefault("timestamp", int(time.time()))
                # Ensure basics exist
                order.setdefault("symbol", order.get("pair", "?"))
                order.setdefault("side", order.get("action", "?"))
        except Exception:
            pass

        # Speichert Order in order_history.json ohne Duplikate
        self._ensure_history_file()
        raw_orders = load_json_file(self.trades_file)
        orders = raw_orders if isinstance(raw_orders, list) else []

        # Prüfe auf Duplikat anhand von id ODER (symbol, side, timestamp)
        if any(
            (o.get("id") and o.get("id") == order.get("id")) or (
                o.get("symbol") == order.get("symbol") and
                o.get("side") == order.get("side") and
                o.get("timestamp") == order.get("timestamp")
            )
            for o in orders
        ):
            log_warning(
                f"⚠️ Duplikat erkannt (id: {order.get('id')}, Symbol: {order.get('symbol')}, Side: {order.get('side')}, TS: {order.get('timestamp')}), überspringe Speicherung."
            )
            return

        # Für SELL: ensure all relevant fields are set, and calculate PNL if not set
        if order.get("side") == "sell":
            order["reason"] = "Manual/Signal Sell"
            order["sl"] = order.get("sl", 0.0)
            order["tp"] = order.get("tp", 0.0)
            order["fee"] = order.get("fee", 0.0)
            # Calculate PNL if not set or 0
            if order.get("pnl", 0) == 0:
                # Try to find matching BUY in history
                for o in orders:
                    if (
                        o.get("symbol") == order.get("symbol") and
                        o.get("side") == "buy" and
                        o.get("entry_price") == o.get("price") and
                        o.get("quantity") == order.get("quantity") and
                        o.get("pnl", 0) == 0
                    ):
                        entry_price = o.get("price", 0)
                        entry_qty = o.get("quantity", 0)
                        fee = order.get("fee", 0.0)
                        pnl = round((order.get("price", 0) - entry_price) * entry_qty - fee, 6)
                        order["pnl"] = pnl
                        break

        # Falls SELL mit PNL, aktualisiere passenden BUY-Eintrag
        if order.get("side") == "sell" and order.get("pnl", 0) != 0:
            for i, o in enumerate(orders):
                if (
                    o.get("symbol") == order.get("symbol") and
                    o.get("side") == "buy" and
                    o.get("entry_price") == o.get("price") and
                    o.get("quantity") == order.get("quantity") and
                    o.get("pnl", 0) == 0
                ):
                    orders[i].update({
                        "pnl": order["pnl"],
                        "reason": order["reason"],
                        "fee": order["fee"],
                        "timestamp": order["timestamp"]
                    })
                    save_json_file(self.trades_file, orders)
                    log_info(f"💾 PAPER-Trade aktualisiert mit PNL ({order['id']})")
                    # Do not return here; also append the SELL order to history.
                    break

        orders.append(order)
        save_json_file(self.trades_file, orders)
        log_info(f"💾 PAPER-Trade gespeichert ({order['id']})")

    def log_trade_to_json(self, trade):
        # Alias für record_order, um Kompatibilität mit LIVE-Code zu gewährleisten
        self.record_order(trade)

    def log_order_history(self, order):
        # Funktion zum Speichern der Order-Historie (Kompatibel mit LIVE-Modus)
        self.record_order(order)