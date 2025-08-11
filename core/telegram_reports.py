
from core.telegram_utils import send_telegram_message

def send_position_summary(positions: list):
    """
    Sendet eine Übersicht offener Positionen über Telegram.
    Erwartet eine Liste von Dictionaries mit Schlüsseln: symbol, quantity, entry_price.
    """
    if not positions:
        send_telegram_message("📭 Keine offenen Positionen.")
        return

    msg_lines = ["📊 Aktuelle Positionen:"]
    for p in positions:
        symbol = p.get("symbol", "N/A")
        qty = p.get("quantity", "N/A")
        entry = p.get("entry_price", "N/A")
        msg_lines.append(f"• {symbol} | Entry: {entry} | Menge: {qty}")
    send_telegram_message("\n".join(msg_lines))