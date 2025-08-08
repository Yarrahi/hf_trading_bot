import os
from core.performance import send_daily_report, save_daily_performance
from core.recovery import send_backup_warning_if_needed, save_account_overview
from core.kucoin_api import get_live_account_balances
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "23:59")

def clean_markdown(text):
    # Entfernt Markdown-Symbole aus dem Text
    clean_text = text.replace("*", "").replace("_", "").replace("`", "").strip()
    if clean_text.startswith("#"):
        clean_text = clean_text.lstrip("#").strip()
    return clean_text

import re

def escape_md_v2(text: str) -> str:
    # Telegram MarkdownV2 benötigt Escape für folgende Sonderzeichen, inkl. Punkt und Backslash
    escape_chars = r'_*[]()~`>#+-=|{}.!\\'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def run_daily_report():
    """
    Speichert die Performance, Account-Balances und sendet den Tagesbericht via Telegram.
    """
    print(f"[{datetime.now()}] Generiere Tagesreport...")
    save_daily_performance()

    # Performance-Dashboard generieren (CSV + Equity-Kurve)
    from core.performance import export_performance_csv, generate_equity_curve_plot
    export_performance_csv()
    equity_curve_path = "data/equity_curve.png"
    generate_equity_curve_plot(output_path=equity_curve_path)

    # Account-Balances abrufen und sicherstellen, dass sie ein Dict sind
    balances = get_live_account_balances()
    if isinstance(balances, list):
        balances = {entry.get('currency', 'UNKNOWN'): {
            "available": float(entry.get('available', 0)),
            "hold": float(entry.get('holds', 0)),
            "balance": float(entry.get('balance', 0))
        } for entry in balances}

    save_account_overview(balances)

    # Performance-Report in MarkdownV2 formatieren
    from core.performance import generate_daily_report
    perf_report = generate_daily_report()
    lines = perf_report.splitlines()

    formatted_perf = "*📢 HF Trading Bot – Tagesbericht*\n\n"
    formatted_perf += "*📊 Performance*\n"
    for line in lines:
        clean_line = line.replace("*", "").replace("_", "").replace("`", "").strip()
        if "Gesamter PnL" in clean_line:
            try:
                value = float(clean_line.split(":")[-1].strip())
            except Exception:
                value = 0
            value_str = escape_md_v2(f"{value:.2f}")
            emoji = "🟢" if value >= 0 else "🔴"
            formatted_perf += f"{emoji} Gesamter PnL: *{value_str}*\n"
        else:
            clean_line = escape_md_v2(clean_line)
            if "Trefferquote" in clean_line:
                formatted_perf += f"🎯 *{clean_line}*\n"
            elif "Gewinne" in clean_line:
                formatted_perf += f"📈 *{clean_line}*\n"
            elif "Verluste" in clean_line:
                formatted_perf += f"📉 *{clean_line}*\n"
            elif "Drawdown" in clean_line or "Max. Drawdown" in clean_line:
                formatted_perf += f"📉 *{clean_line}*\n"
            elif "Datum" in clean_line:
                formatted_perf += f"📅 {clean_line}\n"
            elif "Anzahl Trades" in clean_line:
                formatted_perf += f"🔢 {clean_line}\n"
            else:
                formatted_perf += f"{clean_line}\n"

    # Kontostand Übersicht ebenfalls in MarkdownV2 formatieren
    balance_msg = "*📋 Kontostand Übersicht:*\n"

    from core.kucoin_api import get_symbol_price

    def convert_to_usdt(asset: str, amount: float) -> float:
        if asset.upper() == "USDT":
            return amount
        try:
            price = get_symbol_price(f"{asset}-USDT")
            return amount * float(price)
        except Exception:
            return 0.0

    sorted_balances = sorted(balances.items(), key=lambda x: float(x[1].get("balance", 0)), reverse=True)
    total_sum = 0
    for asset, data in sorted_balances:
        total = float(data.get("balance", 0))
        if total > 0:
            usdt_value = convert_to_usdt(asset, total)
            available = data.get("available", 0)
            hold = data.get("hold", 0)
            emoji = "💰" if usdt_value > 1000 else "🪙"
            asset_md = escape_md_v2(str(asset))
            available_str = escape_md_v2(str(available))
            hold_str = escape_md_v2(str(hold))
            total_str = escape_md_v2(f"{total:.8f}".rstrip('0').rstrip('.'))
            balance_msg += f"{emoji} *{asset_md}*: {available_str} avail \\| {hold_str} hold \\| *{total_str}*\n"
            total_sum += usdt_value
    total_sum_str = escape_md_v2(f"{total_sum:.2f}")
    balance_msg += f"\n*Gesamtsumme:* {total_sum_str} USDT\n"

    report = formatted_perf + "\n" + balance_msg

    from core.telegram_utils import send_safe_message
    send_safe_message(message=report, parse_mode="MarkdownV2", to_private=True, to_channel=False)

    # Option: Sende weiterhin Mini-Report im MarkdownV2-Format für Kanal
    # Kompakter Mini-Report für öffentlichen Kanal in MarkdownV2
    date_str = clean_markdown(next((line for line in lines if "Datum" in line), "N/A")).split(":")[-1].strip()
    trades_str = clean_markdown(next((line for line in lines if "Anzahl Trades" in line), "0")).split(":")[-1].strip()
    pnl_value = float(next((line.split(":")[-1].strip() for line in lines if "Gesamter PnL" in line), 0))
    pnl_str = escape_md_v2(f"{pnl_value:.2f}")
    total_value = total_sum
    total_str = escape_md_v2(f"{total_value:.2f}")

    mini_report_lines = [
        "*📢 HF Trading Bot – Kurzbericht*",
        f"📅 Datum: {escape_md_v2(date_str)}",
        f"🔢 Anzahl Trades: {escape_md_v2(trades_str)}",
        f"🟢 Gesamter PnL: *{pnl_str}*",
        f"💰 Gesamtsumme: *{total_str}* USDT",
    ]
    mini_report = "\n".join(mini_report_lines)
    send_safe_message(message=mini_report, to_private=False, to_channel=True, parse_mode="MarkdownV2")

    # Backup-Check und ggf. Warnung
    send_backup_warning_if_needed()
    # Equity-Kurve per Telegram senden
    try:
        from core.telegram_utils import send_document
        if os.path.exists(equity_curve_path):
            send_document(equity_curve_path, caption="📈 Equity-Kurve")
    except Exception as e:
        print(f"Fehler beim Senden der Equity-Kurve: {e}")
    print(f"[{datetime.now()}] Tagesreport gesendet.")

if __name__ == "__main__":
    run_daily_report()