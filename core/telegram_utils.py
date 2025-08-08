from core.utils import get_env_variable, save_json_file
from telegram import Bot
from telegram.error import TelegramError

TELEGRAM_TOKEN = get_env_variable("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID_PRIVATE = get_env_variable("TELEGRAM_CHAT_ID_PRIVATE")
TELEGRAM_CHAT_ID_CHANNEL = get_env_variable("TELEGRAM_CHAT_ID_CHANNEL")
LOG_TO_TELEGRAM = get_env_variable("LOG_TO_TELEGRAM", "False") == "True"

bot = None
if TELEGRAM_TOKEN:
    bot = Bot(token=TELEGRAM_TOKEN)

def send_telegram_message(message: str, to_channel: bool = False, to_private: bool = True, parse_mode: str = "HTML"):
    if not bot:
        print("[DEBUG] Telegram nicht konfiguriert – kein Versand möglich.")
        return

    message = message.strip()

    if message.strip().isdigit() or message.strip().startswith('-'):
        print(f"[Telegram Warning] Nachricht sieht aus wie eine Chat-ID und wird nicht gesendet: {message}")
        return

    if not isinstance(message, str):
        print(f"[Telegram Error] ⚠️ Ungültiger Nachrichtentyp: {type(message)} – Inhalt: {message}")
        return

    targets = []
    if to_channel and TELEGRAM_CHAT_ID_CHANNEL:
        targets.append(TELEGRAM_CHAT_ID_CHANNEL)
    if to_private and TELEGRAM_CHAT_ID_PRIVATE:
        targets.append(TELEGRAM_CHAT_ID_PRIVATE)

    for chat_id in targets:
        try:
            bot.send_message(chat_id=str(chat_id), text=message, parse_mode=parse_mode, disable_web_page_preview=True)
        except TelegramError as e:
            print(f"[Telegram Error] {e}")

def send_safe_message(message: str, to_channel: bool = False, to_private: bool = True, parse_mode: str = "HTML", **kwargs):
    if not bot:
        print("[DEBUG] Telegram nicht konfiguriert – kein Versand möglich.")
        return

    message = message.strip()

    if message.strip().isdigit() or message.strip().startswith('-'):
        print(f"[Telegram Warning] Nachricht sieht aus wie eine Chat-ID und wird nicht gesendet: {message}")
        return

    escaped = message.replace("<", "&lt;").replace(">", "&gt;")
    targets = []
    if to_private and TELEGRAM_CHAT_ID_PRIVATE:
        targets.append(TELEGRAM_CHAT_ID_PRIVATE)
    if to_channel and TELEGRAM_CHAT_ID_CHANNEL:
        targets.append(TELEGRAM_CHAT_ID_CHANNEL)
    for chat_id in targets:
        try:
            bot.send_message(chat_id=str(chat_id), text=escaped, parse_mode=parse_mode, disable_web_page_preview=True, **kwargs)
        except TelegramError as e:
            if any(kw in message.upper() for kw in ["BUY", "SELL", "BOT"]):
                print(f"[Telegram Error] {e}")

def send_trade_alert(action: str, pair: str, price: float, entry_price: float):
    try:
        change = ((price - entry_price) / entry_price) * 100
        direction = "📈 Gewinn" if change > 0 else "📉 Verlust"
        message = (
            f"🚨 {action} ausgelöst für {pair}\n"
            f"💰 Preis: {price:.2f} USDT\n"
            f"🎯 Einstieg: {entry_price:.2f} USDT\n"
            f"{direction}: {change:.2f}%"
        )
        send_telegram_message(message, to_private=True, to_channel=False)
    except Exception as e:
        print(f"[Telegram Error] Fehler beim Senden von SL/TP-Telegram: {e}")

def send_log_message(message: str):
    if LOG_TO_TELEGRAM:
        send_safe_message(message)

def send_position_summary():
    """Stub: Positionsübersicht wurde deaktiviert und wird refaktoriert."""
    send_log_message("📭 Positionsübersicht aktuell deaktiviert (Refaktorierung in Arbeit).")

from core.kucoin_api import get_live_account_balances

def notify_live_balance():
    try:
        balances = get_live_account_balances()  # Immer als Dict
        if not isinstance(balances, dict):
            balances = {b.get('currency', 'UNKNOWN'): {
                "available": float(b.get('available', 0)),
                "hold": float(b.get('holds', 0)),
                "balance": float(b.get('balance', 0))
            } for b in balances}

        message = "📋 <b>API-Kontenübersicht:</b>\n"
        for asset, balance in balances.items():
            total = float(balance.get('balance', 0))
            if total > 0:
                available = balance.get('available', 0)
                hold = balance.get('hold', 0)
                message += (
                    f"🔹 {asset}: {available} verfügbar, "
                    f"{hold} reserviert, Gesamt: {total}\n"
                )
        send_telegram_message(message.strip())
    except Exception as e:
        print(f"[Telegram Error] Fehler beim Senden des Kontostands: {e}")


# Neue Funktion zum Senden von Dokumenten an Telegram
def send_document(file_path: str, caption: str = "", to_private: bool = True, to_channel: bool = False):
    """Sendet ein Dokument (z.B. PNG, CSV) an Telegram."""
    if not bot:
        print("[DEBUG] Telegram nicht konfiguriert – kein Versand möglich.")
        return
    targets = []
    if to_private and TELEGRAM_CHAT_ID_PRIVATE:
        targets.append(TELEGRAM_CHAT_ID_PRIVATE)
    if to_channel and TELEGRAM_CHAT_ID_CHANNEL:
        targets.append(TELEGRAM_CHAT_ID_CHANNEL)
    for chat_id in targets:
        try:
            with open(file_path, "rb") as f:
                bot.send_document(chat_id=str(chat_id), document=f, caption=caption)
        except TelegramError as e:
            print(f"[Telegram Error] Fehler beim Senden von Dokument {file_path}: {e}")
        except FileNotFoundError:
            print(f"[Telegram Error] Datei nicht gefunden: {file_path}")