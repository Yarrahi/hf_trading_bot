from config.config import MODE
from core.wallet import Wallet as LiveWallet
from core.paper_wallet import PaperWallet

def get_wallet():
    if MODE == "LIVE":
        return LiveWallet()
    elif MODE == "PAPER":
        return PaperWallet()
    else:
        raise ValueError(f"Unbekannter Modus: {MODE}")