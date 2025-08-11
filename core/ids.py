import time, hashlib
import os

DEFAULT_BUCKET_MS = int(os.getenv("IDEMPOTENCY_BUCKET_MS", "200"))

def make_client_oid(symbol: str, side: str, price: str, qty: str, strategy: str = "default", bucket_ms: int = DEFAULT_BUCKET_MS):
    ms = int(time.time_ns() // 1_000_000)
    if not bucket_ms or int(bucket_ms) <= 0:
        bucket_token = f"nb-{ms}"
    else:
        bucket_token = str(ms // int(bucket_ms) * int(bucket_ms))
    raw = f"{symbol}|{side}|{price}|{qty}|{strategy}|{bucket_token}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]
