
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict
import threading

@dataclass(frozen=True)
class SymbolFilters:
    price_increment: Decimal
    base_increment: Decimal
    min_funds: Optional[Decimal] = None
    min_qty: Optional[Decimal] = None
    max_qty: Optional[Decimal] = None

class FilterBook:
    def __init__(self):
        self._by_symbol: Dict[str, SymbolFilters] = {}
        self._lock = threading.RLock()

    def set_all(self, mapping: Dict[str, SymbolFilters]):
        with self._lock:
            self._by_symbol = dict(mapping)

    def get(self, symbol: str) -> Optional[SymbolFilters]:
        with self._lock:
            return self._by_symbol.get(symbol)

    def quantize_price(self, symbol: str, price) -> Decimal:
        f = self.get(symbol)
        if not f: return Decimal(str(price))
        inc = f.price_increment
        q = (Decimal(str(price)) / inc).to_integral_value(rounding=ROUND_DOWN) * inc
        return q

    def quantize_qty(self, symbol: str, qty) -> Decimal:
        f = self.get(symbol)
        if not f: return Decimal(str(qty))
        inc = f.base_increment
        q = (Decimal(str(qty)) / inc).to_integral_value(rounding=ROUND_DOWN) * inc
        return q

    def validate(self, symbol: str, side: str, price: Decimal, qty: Decimal):
        f = self.get(symbol)
        if not f:
            return None, (price * qty)
        notional = price * qty
        if f.min_funds and notional < f.min_funds:
            return f"below_min_funds: {notional} < {f.min_funds}", notional
        if f.min_qty and qty < f.min_qty:
            return f"below_min_qty: {qty} < {f.min_qty}", notional
        if f.max_qty and qty > f.max_qty:
            return f"above_max_qty: {qty} > {f.max_qty}", notional
        return None, notional

filter_book = FilterBook()

def prepare_order(symbol: str, side: str, px, qty):
    qpx = filter_book.quantize_price(symbol, px)
    qqty = filter_book.quantize_qty(symbol, qty)
    err, notional = filter_book.validate(symbol, side, qpx, qqty)
    return qpx, qqty, err, notional
