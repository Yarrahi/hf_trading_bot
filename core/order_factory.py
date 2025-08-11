import os
from core.order import OrderHandler as LiveOrderHandler
from core.paper_order import PaperOrderHandler


def get_order_handler(mode: str | None, position_manager=None):
    """Return the correct order handler based on the supplied mode (env fallback).

    If `mode` is None, read MODE from environment with default "PAPER".
    The returned handler (Live/Paper) gets `position_manager` attached if provided.
    """
    mode_norm = (mode or os.getenv("MODE", "PAPER")).upper()

    if mode_norm == "LIVE":
        handler = LiveOrderHandler(mode="LIVE")
        handler.position_manager = position_manager
        return handler

    if mode_norm == "PAPER":
        handler = PaperOrderHandler()
        # keep parity: attach position manager if upstream provides it
        handler.position_manager = position_manager
        return handler

    raise ValueError(f"Unbekannter Modus: {mode}")