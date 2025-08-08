# paper_wallet.py
# Simuliertes Wallet für Paper-Trading-Modus

from core.logger import log_info
from config.config import get_pair_list

class PaperWallet:
    def __init__(self):
        # Initiales fiktives Guthaben, ENV-abhängig anpassbar
        import os
        start_usdt = float(os.getenv("PAPER_START_BALANCE_USDT", "10000"))
        if start_usdt <= 0:
            start_usdt = 10000
            log_info(f"⚠️ PAPER_START_BALANCE_USDT war ungültig oder 0 – Standardwert {start_usdt} USDT gesetzt.")
        self.balances = {
            "USDT": start_usdt,
            "BTC": 0.0,
            "ETH": 0.0,
            "ADA": 0.0,
            "DOGE": 0.0,
            "SOL": 0.0,
        }

    def get_balance(self, symbol="USDT"):
        value = self.balances.get(symbol, 0.0)
        log_info(f"💰 PAPER-Balance für {symbol}: {value}")
        return value

    def load_balance(self):
        pair = get_pair_list()[0]
        base, quote = pair.split("-")
        return {
            base: round(self.balances.get(base, 0.0), 8),
            quote: round(self.balances.get(quote, 0.0), 8)
        }

    def get_available_balance(self, coin: str) -> float:
        return self.get_balance(coin)

    def update_balance(self, symbol: str, quantity: float, is_buy: bool, price: float = None, quote: str = None, fee_rate: float = None) -> bool:
        """Aktualisiert die Paper-Balances unter Berücksichtigung von Preis und Gebühren.
        - symbol: Basis-Asset (z. B. "DOGE")
        - quantity: Menge des Basis-Assets
        - is_buy: True = Kauf, False = Verkauf
        - price: Preis in Quote-Asset pro 1 Basis-Asset (optional; default 1.0 für Abwärtskompatibilität)
        - quote: Quote-Asset (z. B. "USDT"); default: aus erstem Pair der ENV abgeleitet
        - fee_rate: Gebührenrate als Dezimal (z. B. 0.001); default: aus Config (TAKER_FEE oder FEE_RATE)
        Gibt True zurück, wenn Balance aktualisiert wurde, sonst False (z. B. unzureichendes Guthaben).
        """
        # Ableitung Standardwerte
        if quote is None:
            pair = get_pair_list()[0]
            try:
                _, quote = pair.split("-")
            except ValueError:
                quote = "USDT"
        if fee_rate is None:
            try:
                from config.config import get_config
                fee_rate = float(get_config("TAKER_FEE", 0.0)) or float(get_config("FEE_RATE", 0.0))
            except Exception:
                fee_rate = 0.0
        if price is None:
            price = 1.0  # Rückwärtskompatibilität

        base = symbol
        quote_bal = self.balances.get(quote, 0.0)
        base_bal = self.balances.get(base, 0.0)

        if is_buy:
            cost = price * quantity
            fee = cost * fee_rate
            total = cost + fee
            if quote_bal + 1e-12 < total:
                log_info(f"⚠️ PAPER: Nicht genug {quote} für Kauf – benötigt {total:.8f}, verfügbar {quote_bal:.8f}")
                return False
            self.balances[quote] = quote_bal - total
            self.balances[base] = base_bal + quantity
            log_info(f"📥 PAPER: Gekauft {quantity:.8f} {base} @ {price:.8f} → Kosten {cost:.8f} {quote}, Fee {fee:.8f}")
        else:
            sell_qty = min(quantity, base_bal)
            if sell_qty <= 0:
                log_info(f"⚠️ PAPER: Keine {base}-Menge zum Verkauf verfügbar.")
                return False
            proceeds = price * sell_qty
            fee = proceeds * fee_rate
            self.balances[base] = base_bal - sell_qty
            self.balances[quote] = quote_bal + (proceeds - fee)
            log_info(f"📤 PAPER: Verkauft {sell_qty:.8f} {base} @ {price:.8f} → Erlös {(proceeds - fee):.8f} {quote}, Fee {fee:.8f}")
        return True
