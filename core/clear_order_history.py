import os
import json
from datetime import datetime
from core.logger import log_info, log_error
from core.telegram_utils import send_telegram_message

ORDER_HISTORY_FILE = "data/order_history.json"

def clear_order_history():
    try:
        # Clear the order_history.json by overwriting with empty list
        with open(ORDER_HISTORY_FILE, "w") as f:
            json.dump([], f)
        log_info(f"Order history cleared at {datetime.now().isoformat()}")
        # Telegram message senden
        send_telegram_message("Order history wurde erfolgreich geleert.")
    except Exception as e:
        log_error(f"Error clearing order history: {e}")

if __name__ == "__main__":
    clear_order_history()