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

WINDOW_MS        = 20    # seconds to collect prints per zone
PRICE_BUCKET_ES  = 1.0   # ES grouping bucket in points
PRICE_BUCKET_NQ  = 5.0   # NQ grouping bucket in points
CLUSTER_WINDOW   = 180   # seconds to watch for a cluster (3 min)
CLUSTER_THRESH   = 3     # number of zones in window to trigger cluster alert

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
    bucket = PRICE_BUCKET_NQ if alert['instrument'] == 'NQ' else PRICE_BUCKET_ES
    bucketed = round(round(alert['price'] / bucket) * bucket, 2)
    return "{}-{}".format(alert['instrument'], bucketed)

# ── Buffers ───────────────────────────────────────────────────────────────────
alert_buffer  = {}
alert_tasks   = {}

# cluster tracking: instrument -> list of (timestamp, price, total_buy, total_sell)
cluster_log   = {}
cluster_alerted = {}  # instrument -> last cluster alert timestamp

def post_to_discord(content):
    try:
        requests.post(
            "https://discord.com/api/v10/channels/{}/messages".format(DISCORD_CHAN),
            headers={"Authorization": "Bot {}".format(DISCORD_TOKEN), "Content-Type": "application/json"},
            json={"content": content},
            timeout=10
        )
    except Exception as e:
        print("Discord error: {}".format(e))

def call_claude(system_prompt, user_content, max_tokens=300):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}]
        },
        timeout=15
    )
    return r.json()["content"][0]["text"]

async def check_cluster(instrument, et):
    import time
    now = time.time()

    if instrument not in cluster_log:
        cluster_log[instrument] = []

    # prune old entries
    cluster_log[instrument] = [e for e in cluster_log[instrument] if now - e["ts"] <= CLUSTER_WINDOW]

    recent = cluster_log[instrument]
    if len(recent) < CLUSTER_THRESH:
        return

    # check cooldown -- don't fire cluster alert more than once per 3 min
    last = cluster_alerted.get(instrument, 0)
    if now - last < CLUSTER_WINDOW:
        return

    cluster_alerted[instrument] = now

    # build cluster summary
    all_prices = [e["price"] for e in recent]
    total_buy  = sum(e["buy"] for e in recent)
    total_sell = sum(e["sell"] for e in recent)
    min_price  = min(all_prices)
    max_price  = max(all_prices)
    zone_range = "{} - {}".format(min_price, max_price) if min_price != max_price else str(min_price)
    zones      = len(recent)

    summary = (
        "Instrument: {}\nZone range: {}\nZones hit: {}\n"
        "Total buy volume across all zones: {}\nTotal sell volume across all zones: {}\n"
        "Time window: last 3 minutes"
    ).format(instrument, zone_range, zones, total_buy, total_sell)

    try:
        analysis = call_claude(
            "You analyze Bookmap order flow for ES/NQ futures traders. "
            "Multiple price zones have fired alerts within a 3-minute window, indicating heavy market activity. "
            "Give a bold one-line header like '⚡ Heavy activity cluster detected' then explain in 2 lines: "
            "the full price range being hit, which side is dominant based on total volume, and what this cluster of activity suggests. "
            "Plain English, conditional tone (not definitive). No em dashes. Use backticks for prices and volumes.",
            summary,
            max_tokens=200
        )
        post_to_discord("🚨 **CLUSTER ALERT | {} `{}`** | {} ET\n{}".format(instrument, zone_range, et, analysis))
        print("Cluster alert: {} {} ({} zones)".format(instrument, zone_range, zones))
    except Exception as e:
        print("Cluster Claude error: {}".format(e))

async def analyze_and_post(key):
    await asyncio.sleep(WINDOW_MS)
    if key not in alert_buffer:
        return
    alerts = alert_buffer.pop(key)
    alert_tasks.pop(key, None)
    if not alerts:
        return

    import time
    instrument = alerts[0]["instrument"]
    sweeps  = [a for a in alerts if a["type"] == "sweep"]
    absorbs = [a for a in alerts if a["type"] == "absorption"]
    prices  = sorted(set(a["price"] for a in alerts))
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
        analysis = call_claude(
            "You analyze Bookmap order flow alerts for ES/NQ futures traders. "
            "Multiple prints have been clustered from the same price zone over ~20 seconds. "
            "Describe ONLY what happened with the order flow in plain simple English -- who was aggressive, who absorbed, and the volume on each side. "
            "Do NOT declare if a level held or broke -- price needs more time to confirm that. "
            "If sellers were absorbed, say something like 'if price holds here, this absorption is bullish context.' "
            "If buyers were absorbed, say 'if price stays below here, this absorption is bearish context.' "
            "Write like you are texting a fellow trader a quick update. Keep it conditional, not definitive. "
            "3 lines max. Use backticks for prices and volumes. Never say go long or short. No em dashes.",
            summary
        )
        post_to_discord("📊 **{} `{}`** | {} ET\n{}".format(instrument, price_range, et, analysis))
        print("Posted: {} {} ({} prints, buy {} sell {})".format(instrument, price_range, len(alerts), total_buy, total_sell))
    except Exception as e:
        print("Claude error: {}".format(e))
        return

    # log to cluster tracker
    if instrument not in cluster_log:
        cluster_log[instrument] = []
    cluster_log[instrument].append({
        "ts": time.time(),
        "price": prices[0],
        "buy": total_buy,
        "sell": total_sell
    })

    await check_cluster(instrument, et)

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    from telethon.sessions import StringSession

    session = StringSession(SESSION_STR) if SESSION_STR else StringSession()
    client  = TelegramClient(session, API_ID, API_HASH)

    await client.start(phone=PHONE)

    if not SESSION_STR:
        print("\n=== SAVE THIS SESSION STRING TO RAILWAY VARS ===\n{}\n================================================\n".format(client.session.save()))

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
