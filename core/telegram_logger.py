import logging
from core.telegram_utils import send_log_message

class TelegramHandler(logging.Handler):
    """
    Custom Logging-Handler für Telegram: sendet Logs ab WARNING-Level an den konfigurierten Telegram-Kanal.
    """
    def emit(self, record):
        try:
            msg = self.format(record)
            if record.levelno >= logging.WARNING:
                send_log_message(f"[{record.levelname}] {msg}")
        except Exception:
            pass
