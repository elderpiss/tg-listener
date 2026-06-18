import os
import asyncio
import re
import requests
from telethon import TelegramClient, events
from datetime import datetime
import pytz

# ── Config ────────────────────────────────────────────────────────────────────
API_ID        = int(os.environ["TELEGRAM_API_ID"])
API_HASH      = os.environ["TELEGRAM_API_HASH"]
PHONE         = os.environ["TELEGRAM_PHONE"]
SESSION_STR   = os.environ.get("TELEGRAM_SESSION", "")
CHAT_ID       = int(os.environ.get("TELEGRAM_CHAT_ID", "-1002859415071"))
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHAN  = os.environ.get("ALERT_CHANNEL_ID", "1513645010262691890")

WINDOW_MS     = 20        # seconds to wait before analyzing
PRICE_BUCKET  = 1.0       # group alerts within this many points

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
    # bucket price to nearest PRICE_BUCKET increment
    bucketed = round(round(alert['price'] / PRICE_BUCKET) * PRICE_BUCKET, 2)
    return "{}-{}".format(alert['instrument'], bucketed)

# ── Buffer ────────────────────────────────────────────────────────────────────
alert_buffer = {}
alert_tasks  = {}

async def analyze_and_post(key):
    await asyncio.sleep(WINDOW_MS)
    if key not in alert_buffer:
        return
    alerts = alert_buffer.pop(key)
    alert_tasks.pop(key, None)
    if not alerts:
        return

    instrument = alerts[0]["instrument"]
    sweeps = [a for a in alerts if a["type"] == "sweep"]
    absorbs = [a for a in alerts if a["type"] == "absorption"]
    prices = sorted(set(a["price"] for a in alerts))
    price_range = "{} - {}".format(prices[0], prices[-1]) if len(prices) > 1 else str(prices[0])

    sweep_buy  = sum(a["volume"] for a in sweeps  if a["side"] == "buyers")
    sweep_sell = sum(a["volume"] for a in sweeps  if a["side"] == "sellers")
    abs_buy    = sum(a["volume"] for a in absorbs if a["side"] == "buyers")
    abs_sell   = sum(a["volume"] for a in absorbs if a["side"] == "sellers")
    total_buy  = sweep_buy + abs_buy
    total_sell = sweep_sell + abs_sell

    et = datetime.now(pytz.timezone("America/New_York")).strftime("%I:%M %p")

    summary = (
        "Instrument: {}\nPrice zone: {}\nTime: {} ET\n"
        "Sweep buys: {} | Sweep sells: {}\n"
        "Absorption buys: {} | Absorption sells: {}\n"
        "Total buy: {} | Total sell: {}\n"
        "Print count: {} | Raw: {}"
    ).format(
        instrument, price_range, et,
        sweep_buy, sweep_sell,
        abs_buy, abs_sell,
        total_buy, total_sell,
        len(alerts),
        ', '.join('{} {} {} v{}'.format(a["type"], a["side"], a["price"], a["volume"]) for a in alerts)
    )

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 300,
                "system": (
                    "You analyze Bookmap order flow alerts for ES/NQ futures traders. "
                    "Multiple prints have been clustered from the same price zone over ~20 seconds. "
                    "Describe ONLY what happened with the order flow in plain simple English -- who was aggressive, who absorbed, and the volume on each side. "
                    "Do NOT declare if a level held or broke -- price needs more time to confirm that. "
                    "If sellers were absorbed, say something like 'if price holds here, this absorption is bullish context.' "
                    "If buyers were absorbed, say 'if price stays below here, this absorption is bearish context.' "
                    "Write like you are texting a fellow trader a quick update. Keep it conditional, not definitive. "
                    "3 lines max. Use backticks for prices and volumes. Never say go long or short. No em dashes."
                ),
                "messages": [{"role": "user", "content": summary}]
            },
            timeout=15
        )
        analysis = r.json()["content"][0]["text"]
    except Exception as e:
        print("Claude error: {}".format(e))
        return

    try:
        requests.post(
            "https://discord.com/api/v10/channels/{}/messages".format(DISCORD_CHAN),
            headers={"Authorization": "Bot {}".format(DISCORD_TOKEN), "Content-Type": "application/json"},
            json={"content": "📊 **{} `{}`** | {} ET\n{}".format(instrument, price_range, et, analysis)},
            timeout=10
        )
        print("Posted: {} {} ({} prints, buy {} sell {})".format(instrument, price_range, len(alerts), total_buy, total_sell))
    except Exception as e:
        print("Discord error: {}".format(e))

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    from telethon.sessions import StringSession

    session = StringSession(SESSION_STR) if SESSION_STR else StringSession()
    client = TelegramClient(session, API_ID, API_HASH)

    await client.start(phone=PHONE)

    if not SESSION_STR:
        session_string = client.session.save()
        print("\n=== SAVE THIS SESSION STRING TO RAILWAY VARS ===\n{}\n================================================\n".format(session_string))

    print("Logged in as {}".format((await client.get_me()).first_name))
    print("Listening to chat: {}".format(CHAT_ID))

    @client.on(events.NewMessage(chats=CHAT_ID))
    async def handler(event):
        text = event.message.text or ""
        alert = parse_alert(text)
        if not alert:
            return
        key = get_key(alert)
        if key not in alert_buffer:
            alert_buffer[key] = []
            task = asyncio.ensure_future(analyze_and_post(key))
            alert_tasks[key] = task
            print("New zone: {} (first print: {} {} v{})".format(key, alert["type"], alert["side"], alert["volume"]))
        alert_buffer[key].append(alert)

    print("Listening for alerts...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
