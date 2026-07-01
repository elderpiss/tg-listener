import os
import asyncio
import re
import json
import requests
from telethon import TelegramClient, events
from datetime import datetime
import pytz
import time

# ── Config ────────────────────────────────────────────────────────────────────
API_ID        = int(os.environ["TELEGRAM_API_ID"])
API_HASH      = os.environ["TELEGRAM_API_HASH"]
PHONE         = os.environ["TELEGRAM_PHONE"]
SESSION_STR   = os.environ.get("TELEGRAM_SESSION", "")
CHAT_ID       = int(os.environ.get("TELEGRAM_CHAT_ID", "-1002859415071"))
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHAN  = os.environ.get("ALERT_CHANNEL_ID", "1521560322027032586")
MAIN_BOT_URL  = os.environ.get("MAIN_BOT_URL", "https://insider-traders-bot-production.up.railway.app")

WINDOW_MS        = 20    # seconds to collect prints per zone
PRICE_BUCKET_ES  = 1.0
PRICE_BUCKET_NQ  = 5.0
CLUSTER_WINDOW   = 180
CLUSTER_THRESH   = 3

# ── Filter Thresholds ─────────────────────────────────────────────────────────
MIN_TOTAL_VOLUME     = 600
MIN_PRINT_COUNT      = 3
MIN_IMBALANCE_RATIO  = 1.8
HIGH_CONVICTION_VOL  = 2000
PLAN_TOLERANCE       = 3.0   # points within a plan level to count as a match

# ── Plan Zone Cache ───────────────────────────────────────────────────────────
plan_zones       = []   # [{"price": 7500, "type": "demand"}, ...]
plan_last_text   = None
plan_last_fetch  = 0
PLAN_CACHE_SECS  = 300  # refresh every 5 minutes

def fetch_plan_zones():
    global plan_zones, plan_last_text, plan_last_fetch
    now = time.time()
    if now - plan_last_fetch < PLAN_CACHE_SECS and plan_zones:
        return plan_zones
    try:
        r = requests.get("{}/health".format(MAIN_BOT_URL), timeout=5)
        data = r.json()
        plan_text = data.get("plan")
        if plan_text and plan_text != plan_last_text:
            plan_last_text = plan_text
            plan_zones = extract_zones(plan_text)
            print("Updated plan zones: {}".format(plan_zones))
        plan_last_fetch = now
    except Exception as e:
        print("Plan fetch error: {}".format(e))
    return plan_zones

def extract_zones(plan_text):
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 500,
                "system": 'Extract every price level from this trading plan with its bias. "7580 supply" or "watching for shorts off X" = supply. "7500 demand" or "watching for longs off X" = demand. Respond ONLY with a JSON array like: [{"price": 7580, "type": "supply"}, {"price": 7500, "type": "demand"}]. Skip unclear levels, VWAP, and reference targets with no explicit bias.',
                "messages": [{"role": "user", "content": plan_text}]
            },
            timeout=15
        )
        text = r.json()["content"][0]["text"].strip()
        match = re.search(r'\[[\s\S]*\]', text)
        if match:
            return json.loads(match.group())
    except Exception as e:
        print("Zone extraction error: {}".format(e))
    return []

def find_matching_zone(avg_price, zones):
    for z in zones:
        if abs(z["price"] - avg_price) <= PLAN_TOLERANCE:
            return z
    return None

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

# ── Filter logic ──────────────────────────────────────────────────────────────
def passes_filters(alerts, zones):
    total_buy  = sum(a["volume"] for a in alerts if a["side"] == "buyers")
    total_sell = sum(a["volume"] for a in alerts if a["side"] == "sellers")
    total_vol  = total_buy + total_sell
    avg_price  = sum(a["price"] for a in alerts) / len(alerts)

    dominant = max(total_buy, total_sell)
    weak     = min(total_buy, total_sell)
    imbalance = float('inf') if weak == 0 else dominant / weak

    is_high_conviction = total_vol >= HIGH_CONVICTION_VOL

    if not is_high_conviction:
        if total_vol < MIN_TOTAL_VOLUME:
            return False, None, "below volume threshold ({})".format(total_vol)
        if len(alerts) < MIN_PRINT_COUNT:
            return False, None, "below print count ({})".format(len(alerts))
        if imbalance < MIN_IMBALANCE_RATIO:
            return False, None, "too balanced ({:.1f}x)".format(imbalance)

    # Plan zone check -- applies regardless of tier
    matched_zone = find_matching_zone(avg_price, zones)
    if matched_zone:
        dominant_side = "demand" if total_buy > total_sell else "supply"
        if dominant_side != matched_zone["type"]:
            return False, None, "conflicts with plan: {} zone at {} but flow is {}-aligned".format(
                matched_zone["type"], matched_zone["price"], dominant_side)

    return True, matched_zone, is_high_conviction

# ── Buffers ───────────────────────────────────────────────────────────────────
alert_buffer  = {}
alert_tasks   = {}
cluster_log   = {}
cluster_alerted = {}

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
    now = time.time()
    if instrument not in cluster_log:
        cluster_log[instrument] = []
    cluster_log[instrument] = [e for e in cluster_log[instrument] if now - e["ts"] <= CLUSTER_WINDOW]
    recent = cluster_log[instrument]
    if len(recent) < CLUSTER_THRESH:
        return
    last = cluster_alerted.get(instrument, 0)
    if now - last < CLUSTER_WINDOW:
        return
    cluster_alerted[instrument] = now

    all_prices = [e["price"] for e in recent]
    total_buy  = sum(e["buy"] for e in recent)
    total_sell = sum(e["sell"] for e in recent)
    min_price  = min(all_prices)
    max_price  = max(all_prices)
    zone_range = "{} - {}".format(min_price, max_price) if min_price != max_price else str(min_price)

    summary = (
        "Instrument: {}\nZone range: {}\nZones hit: {}\n"
        "Total buy volume across all zones: {}\nTotal sell volume across all zones: {}\n"
        "Time window: last 3 minutes"
    ).format(instrument, zone_range, len(recent), total_buy, total_sell)

    try:
        analysis = call_claude(
            "You analyze Bookmap order flow for ES/NQ futures traders. "
            "Multiple price zones fired within 3 minutes. "
            "Give a bold one-line header like '⚡ Heavy activity cluster detected' then explain in 2 lines: "
            "the full price range being hit, which side is dominant, and what this cluster suggests. "
            "Conditional tone, not definitive. No em dashes. Backticks for prices and volumes.",
            summary, max_tokens=200
        )
        post_to_discord("🚨 **CLUSTER ALERT | {} `{}`** | {} ET\n{}".format(instrument, zone_range, et, analysis))
        print("Cluster alert: {} {}".format(instrument, zone_range))
    except Exception as e:
        print("Cluster error: {}".format(e))

async def analyze_and_post(key):
    await asyncio.sleep(WINDOW_MS)
    if key not in alert_buffer:
        return
    alerts = alert_buffer.pop(key)
    alert_tasks.pop(key, None)
    if not alerts:
        return

    instrument = alerts[0]["instrument"]
    zones = fetch_plan_zones()
    passed, matched_zone, info = passes_filters(alerts, zones)

    if not passed:
        print("Filtered out {}: {}".format(key, info))
        return

    is_high_conviction = info  # passes_filters returns is_high_conviction as third value when passed=True

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

    plan_context = ""
    if matched_zone:
        plan_context = "\nTHIS MATCHES TODAY'S PLAN {} ZONE AT {}".format(
            matched_zone["type"].upper(), matched_zone["price"])

    summary = (
        "Instrument: {}\nPrice zone: {}\nTime: {} ET\n"
        "Sweep buys: {} | Sweep sells: {}\n"
        "Absorption buys: {} | Absorption sells: {}\n"
        "Total buy: {} | Total sell: {}\n"
        "Print count: {} | Raw: {}{}"
    ).format(
        instrument, price_range, et,
        sweep_buy, sweep_sell,
        abs_buy, abs_sell,
        total_buy, total_sell,
        len(alerts),
        ', '.join('{} {} {} v{}'.format(a["type"], a["side"], a["price"], a["volume"]) for a in alerts),
        plan_context
    )

    try:
        analysis = call_claude(
            "You analyze Bookmap order flow alerts for ES/NQ futures traders and write callouts in Elder's voice.\n\n"
            "Match this exact style:\n"
            "\"Aggressive buyers swept 7454.25-7454.5 four times in a row, totaling 823 contracts with zero sell-side response. No absorption, just pure one-sided buying aggression at this zone. If price holds above here, longs are valid.\"\n"
            "\"Sellers got absorbed at 7445.5 across 8 prints, total absorbed volume 1685. No aggressive sweeps at all, just passive sellers sitting there eating every buy that came in. If price holds here, longs are valid above 7445.5.\"\n"
            "\"Heavy sweep buying at 7465.0 with 2891 contracts hitting aggressively across 13 prints. Absorption sellers stepped in with 641 contracts trying to fade that move but got massively outsized by the buyers. If price holds above 7465.0, longs are valid.\"\n\n"
            "Rules:\n"
            "- State the exact total contract counts for each side\n"
            "- Describe what happened: pure one-sided aggression, absorption holding, or absorption getting run over\n"
            "- If this matches a plan zone, name it explicitly at the start e.g. 'This is at today's demand zone (7500)...'\n"
            "- End with 'if price holds/stays/continues' language -- longs are valid OR shorts are valid (never say go long/short)\n"
            "- 2-3 sentences max\n"
            "- No em dashes\n"
            "- Casual, direct tone",
            summary
        )

        # Build prefix based on conviction and plan match
        if matched_zone and is_high_conviction:
            prefix = "🚨🎯 **HIGH CONVICTION | PLAN ZONE MATCH** | "
        elif is_high_conviction:
            prefix = "🚨 **HIGH CONVICTION** | "
        elif matched_zone:
            prefix = "🎯 **PLAN ZONE MATCH** | "
        else:
            prefix = "📊 "

        post_to_discord("{}{} `{}` | {} ET\n{}".format(prefix, instrument, price_range, et, analysis))
        print("Posted: {} {} ({} prints, buy {} sell {})".format(instrument, price_range, len(alerts), total_buy, total_sell))
    except Exception as e:
        print("Claude error: {}".format(e))
        return

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

    # Pre-fetch plan zones on startup
    fetch_plan_zones()

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
