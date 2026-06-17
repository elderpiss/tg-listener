import os
import asyncio
import re
import json
import time
import requests
from telethon import TelegramClient, events

# ── Config ────────────────────────────────────────────────────────────────────
API_ID        = int(os.environ["TELEGRAM_API_ID"])
API_HASH      = os.environ["TELEGRAM_API_HASH"]
PHONE         = os.environ["TELEGRAM_PHONE"]
SESSION_STR   = os.environ.get("TELEGRAM_SESSION", "")
CHAT_ID       = int(os.environ.get("TELEGRAM_CHAT_ID", "-1002859415071"))
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHAN  = os.environ.get("ALERT_CHANNEL_ID", "1513645010262691890")

# ── Alert parsing ─────────────────────────────────────────────────────────────
def parse_alert(text):
    sweep = re.search(r'Sweep (ES|NQ)\w+\.CME (buyers|sellers) at ([\d.]+) With Volume: (\d+)', text, re.I)
    absorb = re.search(r'Absorption (ES|NQ)\w+\.CME (buyers|sellers) at ([\d.]+) with volume of (\d+)', text, re.I)
    if sweep:
        return {"type": "sweep", "instrument": sweep.group(1).upper(), "side": sweep.group(2).lower(), "price": float(sweep.group(3)), "volume": int(sweep.group(4))}
    if absorb:
        return {"type": "absorption", "instrument": absorb.group(1).upper(), "side": absorb.group(2).lower(), "price": float(absorb.group(3)), "volume": int(absorb.group(4))}
    return None

def get_key(alert):
    return f"{alert['instrument']}-{round(alert['price'] * 4) / 4}"

# ── Buffer ────────────────────────────────────────────────────────────────────
alert_buffer = {}

async def analyze_and_post(key):
    await asyncio.sleep(8)
    if key not in alert_buffer:
        return
    alerts = alert_buffer.pop(key)
    if not alerts:
        return

    instrument = alerts[0]["instrument"]
    sweeps = [a for a in alerts if a["type"] == "sweep"]
    absorbs = [a for a in alerts if a["type"] == "absorption"]
    prices = sorted(set(a["price"] for a in alerts))
    price_range = f"{prices[0]} - {prices[-1]}" if len(prices) > 1 else str(prices[0])

    sweep_buy  = sum(a["volume"] for a in sweeps  if a["side"] == "buyers")
    sweep_sell = sum(a["volume"] for a in sweeps  if a["side"] == "sellers")
    abs_buy    = sum(a["volume"] for a in absorbs if a["side"] == "buyers")
    abs_sell   = sum(a["volume"] for a in absorbs if a["side"] == "sellers")

    from datetime import datetime
    import pytz
    et = datetime.now(pytz.timezone("America/New_York")).strftime("%I:%M %p")

    summary = (
        f"Instrument: {instrument}\nPrice: {price_range}\nTime: {et} ET\n"
        f"Sweep buys: {sweep_buy} | Sweep sells: {sweep_sell}\n"
        f"Absorption buys: {abs_buy} | Absorption sells: {abs_sell}\n"
        f"Total buy: {sweep_buy+abs_buy} | Total sell: {sweep_sell+abs_sell}\n"
        "Raw ({}): {}".format(len(alerts), ', '.join('{} {} {} v{}'.format(a["type"], a["side"], a["price"], a["volume"]) for a in alerts))
    )

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 250,
                "system": "Analyze Bookmap order flow alerts for ES/NQ futures. Report total volumes per side. Determine if level held or broke. Never say go long or short. 2-3 lines max. Use Discord bold and backticks. No em dashes.",
                "messages": [{"role": "user", "content": summary}]
            },
            timeout=15
        )
        analysis = r.json()["content"][0]["text"]
    except Exception as e:
        print(f"Claude error: {e}")
        return

    try:
        requests.post(
            f"https://discord.com/api/v10/channels/{DISCORD_CHAN}/messages",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"},
            json={"content": f"📊 **{instrument} `{price_range}`** | {et} ET\n{analysis}"},
            timeout=10
        )
        print(f"Posted to Discord: {instrument} {price_range}")
    except Exception as e:
        print(f"Discord error: {e}")

def schedule_post(key):
    if key in alert_buffer:
        asyncio.ensure_future(analyze_and_post(key))

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    from telethon.sessions import StringSession

    session = StringSession(SESSION_STR) if SESSION_STR else StringSession()
    client = TelegramClient(session, API_ID, API_HASH)

    await client.start(phone=PHONE)

    if not SESSION_STR:
        session_string = client.session.save()
        print(f"\n=== SAVE THIS SESSION STRING TO RAILWAY VARS ===\n{session_string}\n================================================\n")

    print(f"Logged in as {(await client.get_me()).first_name}")
    print(f"Listening to chat: {CHAT_ID}")

    @client.on(events.NewMessage(chats=CHAT_ID))
    async def handler(event):
        text = event.message.text or ""
        print(f"Message: {text[:60]}")
        alert = parse_alert(text)
        if not alert:
            return
        key = get_key(alert)
        if key not in alert_buffer:
            alert_buffer[key] = []
            asyncio.ensure_future(analyze_and_post(key))
        alert_buffer[key].append(alert)

    print("Listening for alerts...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
