from typing import Optional, Union
import uuid
import time
import os

from dotenv import load_dotenv
load_dotenv()
DEBUG_MODE = os.getenv("DEBUG_MODE", "False").lower() == "true"

class Position:
    def __init__(self, pair: str, quantity: float, entry_price: float, side: str = "buy", sl: Optional[float] = None, tp: Optional[float] = None):
        self.pair = pair
        self.quantity = quantity
        self.entry_price = entry_price
        self.side = side
        self.sl = sl
        self.tp = tp

import json

# Logger imports
from core.logger import log_debug

class PositionManager:
    def __init__(self, mode: str = "LIVE"):
        self.mode = str(mode).upper()
        live_file = os.getenv("LIVE_POSITIONS_FILE", "data/positions_live.json")
        paper_file = os.getenv("PAPER_POSITIONS_FILE", "data/positions_paper.json")
        self.file_path = live_file if self.mode == "LIVE" else paper_file
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        if DEBUG_MODE:
            from core.logger import log_info
            if os.path.exists(self.file_path):
                try:
                    with open(self.file_path, "r") as f:
                        data = json.load(f)
                    log_info(f"♻️ Recovery: {len(data)} Positionen geladen aus {self.file_path} ({self.mode})")
                except Exception:
                    pass

    def load_positions(self) -> dict:
        if not os.path.exists(self.file_path):
            return {}
        try:
            with open(self.file_path, "r") as f:
                data = json.load(f)
            return data
        except Exception as e:
            from core.logger import log_error
            log_error(f"Fehler beim Laden von Positionen ({self.file_path}): {e}")
            return {}

    def _save(self, data: dict) -> None:
        try:
            with open(self.file_path, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            from core.logger import log_error
            log_error(f"Fehler beim Speichern von Positionen ({self.file_path}): {e}")

    def close(self, pair: str) -> None:
        data = self.load_positions()
        # Neue Logik: Alle Positionen für das Symbol finden (Key beginnt mit SYMBOL__ oder symbol-Feld passt)
        keys_to_delete = [key for key, pos in data.items() if pos.get("symbol") == pair or key.startswith(f"{pair}__")]
        from core.logger import log_info
        log_info(f"🗑️ Zu löschende Keys für {pair}: {keys_to_delete}")
        log_info(f"🗑️ Lösche Positionen für {pair}: {keys_to_delete}")
        for key in keys_to_delete:
            del data[key]
        self._save(data)
        log_info(f"📤 Verbleibende Positionen nach Schließen: {list(data.keys())}")
        if keys_to_delete:
            log_info(f"📤 Position(en) geschlossen ({self.mode}): {pair} ({len(keys_to_delete)} gelöscht)")
        else:
            log_info(f"ℹ️ Keine Positionen zu schließen für {pair}")

    def exists(self, pair: str) -> bool:
        data = self.load_positions()
        return pair in data and data[pair].get("entry_price") is not None

    def get(self, pair: str) -> Union[dict, None]:
        data = self.load_positions()
        pos = data.get(pair)
        if not pos:
            log_debug(f"⚠️ Keine Position für {pair} gefunden.")
            return None
        return pos

    def get_entry_price(self, pair: str) -> Optional[float]:
        pos = self.get(pair)
        if pos:
            return pos.get("entry_price")
        log_debug(f"⚠️ Keine Position für {pair} gefunden.")
        return None

    def get_quantity(self, pair: str) -> float:
        pos = self.get(pair)
        if pos:
            return pos.get("quantity", 0.0)
        log_debug(f"⚠️ Keine Position für {pair} gefunden.")
        return 0.0

    def all(self) -> dict:
        """Gibt alle offenen Positionen mit vollständigen Daten zurück."""
        return self.load_positions()

    def has_open(self, pair: str) -> bool:
        return self.exists(pair)

    def set_sl_tp(self, pair: str, sl: float, tp: float) -> None:
        """Speichert Stop-Loss (SL) und Take-Profit (TP) für eine offene Position."""
        from core.logger import log_info, log_error
        data = self.load_positions()
        if pair in data:
            data[pair]["sl"] = sl
            data[pair]["tp"] = tp
            self._save(data)
            if DEBUG_MODE:
                log_info(f"💾 SL/TP für {pair} gesetzt: SL = {sl}, TP = {tp}")
        else:
            log_error(f"⚠️ Keine offene Position für {pair} gefunden – SL/TP nicht gesetzt.")

    def replace_position(self, pair: str, quantity: float, entry_price: float, side: str = "buy") -> None:
        """Ersetzt bestehende Position vollständig (falls vorhanden)"""
        self.save_position({
            "pair": pair,
            "quantity": quantity,
            "entry_price": entry_price,
            "side": side,
            "timestamp": int(time.time())
        })

    def save_position(self, position: dict) -> None:
        """Speichert eine offene Position dauerhaft und unterstützt mehrere Positionen pro Symbol."""
        if position.get("quantity", 0) <= 0:
            from core.logger import log_warning
            log_warning(f"⚠️ Position mit ungültiger Menge ({position.get('quantity')}) wird nicht gespeichert: {position}")
            return
        from core.logger import log_info, log_error
        path = self.file_path
        try:
            with open(self.file_path, 'r') as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}

        timestamp = position.get("timestamp", int(time.time()))
        symbol = position.get("pair") or position.get("symbol")
        if not symbol:
            log_error("⚠️ Position ohne Symbol kann nicht gespeichert werden.")
            return

        if DEBUG_MODE:
            log_info(f"💾 Speichere Position: Symbol={symbol}, Menge={position.get('quantity')}, Entry={position.get('entry_price')}, Side={position.get('side')}, Fee={position.get('fee')}, EntryFee={position.get('entry_fee')}")

        # Ensure the position dict has 'symbol' field for consistency
        position["symbol"] = symbol
        position["timestamp"] = timestamp

        try:
            fee = float(position["fee"])
        except (KeyError, TypeError, ValueError):
            from core.logger import log_warning
            log_warning(f"⚠️ Position hat ungültige Fee: {position.get('fee')}, setze auf 0.0")
            fee = 0.0
        position["fee"] = fee
        position["entry_fee"] = float(position.get("entry_fee", fee))

        # Prüfe bei LIVE-Modus, ob bereits eine Position für dieses Symbol besteht
        if self.mode == "LIVE":
            existing_key = None
            for key in data:
                if symbol in key:
                    existing_key = key
                    break
            if existing_key:
                if DEBUG_MODE:
                    from core.logger import log_info
                    log_info(f"🔄 Aktualisiere bestehende LIVE-Position für {symbol}")
                existing_position = data[existing_key]
                # Position zusammenführen: neue Menge addieren, gewichteter Durchschnittspreis berechnen
                total_quantity = existing_position["quantity"] + position["quantity"]
                if total_quantity > 0:
                    weighted_price = (
                        existing_position["entry_price"] * existing_position["quantity"]
                        + position["entry_price"] * position["quantity"]
                    ) / total_quantity
                else:
                    weighted_price = position["entry_price"]

                existing_position["quantity"] = total_quantity
                existing_position["entry_price"] = weighted_price
                existing_position["timestamp"] = timestamp
                existing_position["fee"] += position["fee"]
                existing_position["entry_fee"] = existing_position.get("entry_fee", 0.0) + position.get("entry_fee", position.get("fee", 0.0))
                data[existing_key] = existing_position
                position_id = existing_key
                self._save(data)
            else:
                if "fee" not in position:
                    from core.logger import log_warning
                    log_warning(f"⚠️ Position wurde ohne Fee gespeichert: {symbol}")
                random_id = uuid.uuid4().hex[:6]
                position_id = f"{symbol}__{timestamp}__{random_id}"
                data[position_id] = position
                self._save(data)
        else:
            random_id = uuid.uuid4().hex[:6]
            position_id = f"{symbol}__{timestamp}__{random_id}"
            data[position_id] = position
            self._save(data)

        from core.logger import log_info
        log_info(f"💾 Position gespeichert unter ID {position_id} für {symbol} ({self.mode})")
        log_info(f"💾 Gespeicherte Positionsdaten: {json.dumps(data[position_id], indent=2)}")

    def has_open_position(self, symbol):
        """
        Prüft, ob es eine offene Position für das angegebene Symbol gibt und loggt Details.
        """
        data = self.load_positions()
        from core.logger import log_info
        open_positions = [pos_id for pos_id, pos in data.items() if pos.get("symbol") == symbol or pos_id.startswith(f"{symbol}__")]
        if DEBUG_MODE:
            log_info(f"🔍 has_open_position-Check für {symbol}: Gefundene offene Positionen: {open_positions}")
        return len(open_positions) > 0

    def get_position(self, symbol):
        data = self.load_positions()
        for position_id, position in data.items():
            if position_id.startswith(f"{symbol}__"):
                return position
        log_debug(f"⚠️ Keine Position für {symbol} gefunden.")
        return None

    def open(self, pair: str, quantity: float, entry_price: float, side: str = "buy", fee: Optional[float] = None, entry_fee: Optional[float] = None) -> None:
        """Öffnet eine neue Position und speichert sie."""
        from core.logger import log_info
        if DEBUG_MODE:
            log_info(f"📥 Öffne Position: Pair={pair}, Menge={quantity}, Entry={entry_price}, Side={side}, Fee={fee}, EntryFee={entry_fee}")
            log_info(f"✅ Übergabe an save_position: fee={fee}, entry_fee={entry_fee}")
        final_fee = float(fee) if fee is not None else float(entry_fee) if entry_fee is not None else 0.0
        self.save_position({
            "pair": pair,
            "quantity": quantity,
            "entry_price": entry_price,
            "side": side,
            "fee": final_fee,
            "entry_fee": float(entry_fee) if entry_fee is not None else final_fee,
            "timestamp": int(time.time())
        })
        log_info(f"📥 Position geöffnet ({self.mode}): {pair} | Menge: {quantity} | Entry: {entry_price}")
        log_info(f"📥 Vollständige offene Position gespeichert: {self.get_position(pair)}")
    def get_open_position(self, symbol: str) -> Optional[dict]:
        """
        Gibt die offene Position für ein Symbol zurück oder None, wenn keine existiert.
        """
        data = self.load_positions()
        for pos_id, pos in data.items():
            if pos_id.startswith(f"{symbol}__"):
                return pos
        log_debug(f"⚠️ Keine Position für {symbol} gefunden.")
        return None
    def close_position(self, symbol: str) -> None:
        """Alias für close() – für Kompatibilität mit SELL-Logik."""
        self.close(symbol)

    def update_sl(self, symbol: str, new_sl: float) -> bool:
        """
        Aktualisiert den Stop-Loss (SL) einer offenen Position.
        """
        positions = self.load_positions()
        updated = False
        for pos_id, pos in positions.items():
            if pos.get("symbol") == symbol:
                pos["sl"] = new_sl
                updated = True
        if updated:
            self._save(positions)
            from core.logger import log_info
            log_info(f"🔄 SL für {symbol} aktualisiert auf {new_sl}")
        return updated

    def update_tp(self, symbol: str, new_tp: float) -> bool:
        """
        Aktualisiert den Take-Profit (TP) einer offenen Position.
        """
        positions = self.load_positions()
        updated = False
        for pos_id, pos in positions.items():
            if pos.get("symbol") == symbol:
                pos["tp"] = new_tp
                updated = True
        if updated:
            self._save(positions)
            from core.logger import log_info
            log_info(f"🔄 TP für {symbol} aktualisiert auf {new_tp}")
        return updated
    def get_open_positions(self) -> list:
        """
        Gibt eine Liste aller offenen Positionen zurück.
        """
        positions = []
        for pos_id, pos in self.load_positions().items():
            positions.append({
                "id": pos_id,
                "symbol": pos.get("symbol") or pos.get("pair"),
                "quantity": pos.get("quantity"),
                "entry_price": pos.get("entry_price"),
                "side": pos.get("side"),
                "sl": pos.get("sl"),
                "tp": pos.get("tp"),
                "timestamp": pos.get("timestamp")
            })
        return positions