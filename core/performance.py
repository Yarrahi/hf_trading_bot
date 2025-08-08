import os
import json
from datetime import datetime
import pandas as pd
import matplotlib.pyplot as plt
from core.utils import ensure_directory
from core.logger import log_info
from core.telegram_utils import send_safe_message
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_CHAT_ID_PRIVATE = os.getenv("TELEGRAM_CHAT_ID_PRIVATE")
TELEGRAM_CHAT_ID_CHANNEL = os.getenv("TELEGRAM_CHAT_ID_CHANNEL")
DAILY_REPORT_FILE = "data/performance.json"

ORDER_HISTORY_FILE = "data/order_history.json"

def log_trade(order):
    """Loggt eine Order in die zentrale order_history.json"""
    ensure_directory(os.path.dirname(ORDER_HISTORY_FILE))

    order_data = {
        "id": order.get("id"),
        "timestamp": order.get("timestamp", int(datetime.utcnow().timestamp())),
        "mode": order.get("mode"),
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "quantity": order.get("quantity"),
        "price": order.get("price"),
        "fee": order.get("fee"),
        "entry_price": order.get("entry_price"),
        "sl": order.get("sl"),
        "tp": order.get("tp"),
        "pnl": order.get("pnl"),
        "reason": order.get("reason"),
    }

    try:
        # Bestehende Daten laden
        if os.path.exists(ORDER_HISTORY_FILE):
            with open(ORDER_HISTORY_FILE, "r") as f:
                trades = json.load(f)
        else:
            trades = []

        # Wenn Order mit gleicher ID existiert, aber aktueller Eintrag hat mehr Felder, dann ersetzen
        existing_ids = [t.get("id") for t in trades]
        if order_data["id"] in existing_ids:
            index = existing_ids.index(order_data["id"])
            old = trades[index]
            if sum(v is not None for v in order_data.values()) > sum(v is not None for v in old.values()):
                trades[index] = order_data
                log_info(f"♻️ Order mit mehr Details ersetzt: {order_data['id']}")
            else:
                log_info(f"⚠️ Doppelter Order-Eintrag erkannt, wird nicht erneut gespeichert: {order_data['id']}")
            with open(ORDER_HISTORY_FILE, "w") as f:
                json.dump(trades, f, indent=4)
            return

        # Eintrag hinzufügen und speichern
        trades.append(order_data)
        with open(ORDER_HISTORY_FILE, "w") as f:
            json.dump(trades, f, indent=4)

        log_info(f"📘 Order gespeichert in order_history.json: {order_data['side']} {order_data['symbol']} ({order_data['quantity']})")

    except Exception as e:
        log_info(f"❌ Fehler beim Loggen der Order: {e}")


# --- Performance Reporting & Telegram ---

def calculate_performance():
    """Berechnet PnL, Trefferquote und Drawdown aus ORDER_HISTORY_FILE und gibt ein Dict zurück."""
    if not os.path.exists(ORDER_HISTORY_FILE):
        return {
            "pnl": 0.0,
            "hit_rate": 0.0,
            "drawdown": 0.0,
            "num_trades": 0,
            "num_wins": 0,
            "num_losses": 0,
        }
    with open(ORDER_HISTORY_FILE, "r") as f:
        trades = json.load(f)
    if not trades:
        return {
            "pnl": 0.0,
            "hit_rate": 0.0,
            "drawdown": 0.0,
            "num_trades": 0,
            "num_wins": 0,
            "num_losses": 0,
        }

    # Nur abgeschlossene Trades mit PnL berücksichtigen
    closed_trades = [t for t in trades if t.get("pnl") is not None]
    num_trades = len(closed_trades)
    pnl_total = sum(t.get("pnl", 0.0) for t in closed_trades)
    num_wins = sum(1 for t in closed_trades if t.get("pnl", 0.0) > 0)
    num_losses = sum(1 for t in closed_trades if t.get("pnl", 0.0) < 0)
    hit_rate = (num_wins / num_trades) * 100 if num_trades > 0 else 0.0

    # Drawdown-Berechnung (maximaler Verlust vom letzten Hoch)
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for t in closed_trades:
        equity += t.get("pnl", 0.0)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_drawdown:
            max_drawdown = dd

    return {
        "pnl": pnl_total,
        "hit_rate": hit_rate,
        "drawdown": max_drawdown,
        "num_trades": num_trades,
        "num_wins": num_wins,
        "num_losses": num_losses,
    }


def save_daily_performance():
    """Speichert die berechneten Performance-Kennzahlen in DAILY_REPORT_FILE."""
    ensure_directory(os.path.dirname(DAILY_REPORT_FILE))
    perf = calculate_performance()
    perf["date"] = datetime.utcnow().strftime("%Y-%m-%d")
    # Lade bestehende Berichte
    if os.path.exists(DAILY_REPORT_FILE):
        with open(DAILY_REPORT_FILE, "r") as f:
            data = json.load(f)
    else:
        data = []
    # Überschreibe Bericht für das heutige Datum, falls vorhanden
    data = [d for d in data if d.get("date") != perf["date"]]
    data.append(perf)
    with open(DAILY_REPORT_FILE, "w") as f:
        json.dump(data, f, indent=4)


def generate_daily_report():
    """Erstellt einen formatierten Bericht als Textstring."""
    perf = calculate_performance()
    lines = []
    lines.append("📊 *Tagesreport*")
    lines.append(f"Datum: {datetime.utcnow().strftime('%Y-%m-%d')}")
    lines.append(f"Anzahl Trades: {perf['num_trades']}")
    lines.append(f"Gewinne: {perf['num_wins']} | Verluste: {perf['num_losses']}")
    lines.append(f"Trefferquote: {perf['hit_rate']:.1f}%")
    lines.append(f"Gesamter PnL: {perf['pnl']:.2f}")
    lines.append(f"Max. Drawdown: {perf['drawdown']:.2f}")
    return "\n".join(lines)


def send_daily_report():
    """Sendet den Tagesbericht an beide Telegram-IDs."""
    report = generate_daily_report()

    # Zusätzliche Info für Übersichtlichkeit (zentrale Formatierung)
    header = "📢 <b>HF Trading Bot – Tagesbericht</b>\n\n"
    full_report = header + report

    # Sende an private und öffentliche Kanäle (Flags statt Chat-IDs)
    send_safe_message(message=full_report, to_private=True, to_channel=False, parse_mode="HTML")
    send_safe_message(message=full_report, to_private=False, to_channel=True, parse_mode="HTML")


# --- Detailliertes Dashboard & Export/Plot ---

def generate_detailed_report():
    """Erstellt einen detaillierten Bericht pro Symbol mit PnL, Winrate und Gebührenanteil."""
    if not os.path.exists(ORDER_HISTORY_FILE):
        return {}
    with open(ORDER_HISTORY_FILE, "r") as f:
        trades = json.load(f)
    if not trades:
        return {}

    df = pd.DataFrame(trades)
    if "pnl" not in df.columns:
        df["pnl"] = 0.0
    if "fee" not in df.columns:
        df["fee"] = 0.0

    report = {}
    for symbol, group in df.groupby("symbol"):
        pnl = group["pnl"].sum()
        wins = (group["pnl"] > 0).sum()
        losses = (group["pnl"] < 0).sum()
        num_trades = len(group)
        hit_rate = (wins / num_trades) * 100 if num_trades > 0 else 0
        fees_total = group["fee"].sum()
        report[symbol] = {
            "pnl": round(pnl, 4),
            "num_trades": num_trades,
            "win_rate": round(hit_rate, 2),
            "fees": round(fees_total, 4)
        }
    return report


def export_performance_csv(file_path="data/performance_export.csv"):
    """Exportiert die gesamte Orderhistorie als CSV für externe Analyse."""
    if not os.path.exists(ORDER_HISTORY_FILE):
        return
    with open(ORDER_HISTORY_FILE, "r") as f:
        trades = json.load(f)
    if not trades:
        return
    df = pd.DataFrame(trades)
    df.to_csv(file_path, index=False)


def generate_equity_curve_plot(output_path="data/equity_curve.png"):
    """Erstellt eine Equity-Kurve basierend auf der Orderhistorie."""
    if not os.path.exists(ORDER_HISTORY_FILE):
        return
    with open(ORDER_HISTORY_FILE, "r") as f:
        trades = json.load(f)
    if not trades:
        return
    df = pd.DataFrame(trades)
    if "pnl" not in df.columns:
        return
    df["cumulative_pnl"] = df["pnl"].cumsum()
    plt.figure(figsize=(10, 5))
    plt.plot(df["cumulative_pnl"], label="Equity-Kurve")
    plt.xlabel("Trades")
    plt.ylabel("Kapital (PnL kumuliert)")
    plt.title("HF Trading Bot – Equity-Kurve")
    plt.legend()
    plt.grid(True)
    ensure_directory(os.path.dirname(output_path))
    plt.savefig(output_path)
    plt.close()