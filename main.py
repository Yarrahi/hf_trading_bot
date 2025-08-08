import os
import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
import argparse
from dotenv import load_dotenv
import json
import threading
import time

from core.logger import logger
from core.telegram_utils import send_telegram_message, send_position_summary
from core.recovery import restore_positions
from stream_kucoin import run_kucoin_stream

def main():
    # === 1. ENV laden ===
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", help="Trading mode (LIVE, PAPER)")
    args, unknown = parser.parse_known_args()
    os.environ["RUN_CONTEXT"] = "bot"
    load_dotenv()
    # MODE: prefer CLI --mode, else ENV .env (via config fallback)
    from config.config import MODE as CONFIG_MODE
    env_mode = os.getenv("MODE", CONFIG_MODE).upper()
    run_context = (args.mode.upper() if args.mode else env_mode)
    os.environ["MODE"] = run_context  # ensure other modules see the correct mode
    logger.info(f"🧰 Laufzeit-Modus ermittelt: {run_context} (CLI>{env_mode})")
    logger.info(f"📂 Geladene Konfiguration: .env")

    # === Startparameter ins Log schreiben ===
    USE_ATR_STOP = os.getenv("USE_ATR_STOP", "False").lower() == "true"
    ATR_MULTIPLIER_SL = float(os.getenv("ATR_MULTIPLIER_SL", 1.5))
    ATR_MULTIPLIER_TP = float(os.getenv("ATR_MULTIPLIER_TP", 3.0))
    REENTRY_COOLDOWN = int(os.getenv("REENTRY_COOLDOWN", 120))
    logger.info(f"Bot-Startparameter: ATR_STOP={USE_ATR_STOP}, SL-Multiplier={ATR_MULTIPLIER_SL}, TP-Multiplier={ATR_MULTIPLIER_TP}, ReEntryCooldown={REENTRY_COOLDOWN}s")

    # === 3. Konfiguration prüfen ===
    mode = run_context
    pairs = [p.strip() for p in os.getenv("PAIRS", "BTC-USDT").split(",") if p.strip()]

    # === 3b. Optionale Positionen-Wiederherstellung ===
    positions = restore_positions(mode=mode)
    if os.getenv("DEBUG_MODE", "false").lower() == "true":
        logger.debug(f"♻️ Recovery: {len(positions)} Positionen geladen.")

    logger.info(f"🚀 Starte HF Trading Bot im {mode}-Modus für: {', '.join(pairs)}")

    # === 4. Telegram-Startnachricht ===
    try:
        start_msg = (
            f"🤖 <b>HF Trading Bot gestartet</b>\n"
            f"Modus: <b>{mode}</b>\n"
            f"Paare: {', '.join(pairs)}"
        )
        send_telegram_message(
            start_msg,
            to_private=True,
            to_channel=True,
            parse_mode="HTML"
        )
        send_position_summary()
    except Exception as e:
        logger.warning(f"⚠️ Telegram konnte nicht benachrichtigt werden: {e}")

    # === 5. Realtime KuCoin Stream starten ===
    if os.path.exists("data/bot_params.json"):
        # Optimierte Parameter laden (Filter-Status-Log entfernt)
        optimized_params_path = "data/bot_params.json"
        if os.path.exists(optimized_params_path):
            with open(optimized_params_path, "r") as f:
                optimized_params = json.load(f)
            import hashlib
            with open(optimized_params_path, "rb") as f:
                params_bytes = f.read()
                params_hash = hashlib.sha256(params_bytes).hexdigest()
            logger.info(f"🔧 bot_params.json geladen – SHA256: {params_hash}")
            # Beispielwerte loggen
            for pair, settings in list(optimized_params.items())[:3]:
                logger.info(f"  {pair}: TP={settings.get('tp')} SL={settings.get('sl')} ScaleOut={settings.get('scale_out')}")

        logger.info("🚀 Starte Bot-Stream nach erfolgreicher Optimierung...")
        try:
            run_kucoin_stream(pairs, optimized_params)
        except KeyboardInterrupt:
            logger.info("🛑 Bot manuell gestoppt (KeyboardInterrupt)")
        except Exception as e:
            logger.exception(f"❌ Schwerer Fehler im Hauptprozess: {e}")
    else:
        logger.error("❌ Keine bot_params.json gefunden – bitte manuell bereitstellen, bevor der Bot gestartet werden kann.")
        try:
            send_telegram_message(
                "❌ <b>Bot-Start fehlgeschlagen</b>\nKeine <code>obot_params.json</code> gefunden.\nBitte manuell bereitstellen und Bot neu starten.",
                to_private=True,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"⚠️ Telegram konnte nicht über fehlende bot_params.json benachrichtigt werden: {e}")

if __name__ == "__main__":
    main()
