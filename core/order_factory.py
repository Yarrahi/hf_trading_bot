import os
from config.config import get_env_var
from core.order import OrderHandler as LiveOrderHandler
from core.paper_order import PaperOrderHandler


def get_order_handler(mode, position_manager=None):
    """Return the correct order handler based on the supplied mode."""
    if mode == "LIVE":
        handler = LiveOrderHandler(mode="LIVE")
        handler.position_manager = position_manager
        return handler
    if mode == "PAPER":
        return PaperOrderHandler()
    raise ValueError(f"Unbekannter Modus: {mode}")