import asyncio
import aiohttp
import websockets
import time
import hmac
import base64
import json
import hashlib
from config.config import get_symbol_config

from core.logger import ticker_logger

SYMBOL_CONFIG = get_symbol_config()
from dotenv import load_dotenv
import os
from strategies.realtime_engine import on_new_price
from core.position import PositionManager
from core.logger_setup import setup_logger
from core.logger import log_price
from core.telegram_utils import send_telegram_message
from core.utils import update_price_cache

load_dotenv()

position_manager = PositionManager()

API_KEY = os.getenv("KUCOIN_API_KEY")
API_SECRET = os.getenv("KUCOIN_API_SECRET")
API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE")
API_BASE_URL = "https://api.kucoin.com"
KUCOIN_WS_ENDPOINT = "wss://ws-api.kucoin.com"

logger = setup_logger(__name__)
send_telegram_message("📡 HF Bot gestartet – empfange Live-Daten von KuCoin ...")

async def get_ws_token():
    url = f"{API_BASE_URL}/api/v1/bullet-public"
    async with aiohttp.ClientSession() as session:
        async with session.post(url) as response:
            data = await response.json()
            return data['data']['instanceServers'][0]['endpoint'], data['data']['token']

async def subscribe_ticker(ws, symbol):
    topic = f"/market/ticker:{symbol}"
    sub_msg = {
        "id": str(int(time.time() * 1000)),
        "type": "subscribe",
        "topic": topic,
        "privateChannel": False,
        "response": True
    }
    await ws.send(json.dumps(sub_msg))
    logger.info(f"✅ Subscribed to {topic}")

async def handle_message(msg, optimized_params=None):
    try:
        data = json.loads(msg)
        msg_type = data.get("type")
        if msg_type in {"welcome", "ack", "pong"}:
            return
        if isinstance(data, dict) and 'topic' in data and 'data' in data:
            if not isinstance(data['data'], dict):
                logger.warning(f"⚠️ Ungültige Datenstruktur: {type(data['data'])} – Inhalt: {data['data']}")
                return
            symbol = data['topic'].split(':')[-1]
            price_str = data['data'].get('price')
            if price_str:
                price = float(price_str)
                ticker_logger.log(symbol, price)
                # Log-Eintrag wird gesammelt (Ticker-Logging alle 5 Sekunden in logger.py)
                update_price_cache(symbol, price)
                on_new_price(symbol, price, optimized_params)
        else:
            logger.warning(f"⚠️ Unerwartetes Format: {type(data)} – Inhalt: {data}")
    except Exception as e:
        logger.warning(f"⚠️ Fehler beim Verarbeiten der Nachricht: {e}")

async def stream_prices(pairs=None, optimized_params=None):
    endpoint, token = await get_ws_token()
    ws_url = f"{endpoint}?token={token}"

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                for symbol in pairs or SYMBOL_CONFIG:
                    await subscribe_ticker(ws, symbol)

                while True:
                    msg = await ws.recv()
                    await handle_message(msg, optimized_params)
        except Exception as e:
            logger.warning(f"⚠️ WebSocket Fehler oder Verbindungsabbruch: {e}. Versuche Reconnect in 5s ...")
            await asyncio.sleep(5)


def run_kucoin_stream(pairs=None, optimized_params=None):
    asyncio.run(stream_prices(pairs, optimized_params))

if __name__ == "__main__":
    asyncio.run(stream_prices())