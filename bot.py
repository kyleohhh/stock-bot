import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import asyncio
import os
import json
from datetime import datetime, time
import pytz
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Channel ID where live prices will be posted
PRICE_CHANNEL_ID = int(os.getenv("PRICE_CHANNEL_ID", "0"))

# Stocks to track in the live feed (edit freely)
TRACKED_STOCKS = ["SPCX", "TSLA", "NVDA", "SPY"]

# How often to update prices (seconds)
UPDATE_INTERVAL = 60

# File to persist alerts across restarts
ALERTS_FILE = "alerts.json"

# Eastern timezone (NYSE/NASDAQ trading hours)
ET = pytz.timezone("America/New_York")

# ── Bot Setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# In-memory alert storage
alerts: dict = {}
alert_counter = 0

# Cache last known prices (used for alert comparisons)
price_cache: dict = {}

# Yahoo Finance auth cache — crumb token, refreshed when stale.
# Uses a persistent session (created in on_ready) so cookies survive between calls,
# which is required for the crumb-based auth to keep working.
_yahoo_crumb: str | None = None
http_session: aiohttp.ClientSession | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_alerts():
    global alerts, alert_counter
    if os.path.exists(ALERTS_FILE):
        with open(ALERTS_FILE) as f:
            data = json.load(f)
            alerts = {int(k): v for k, v in data.get("alerts", {}).items()}
            alert_counter = data.get("counter", 0)

def save_alerts():
    with open(ALERTS_FILE, "w") as f:
        json.dump({"alerts": alerts, "counter": alert_counter}, f, indent=2)

def format_time_no_leading_zero(dt: datetime, fmt: str) -> str:
    """
    Cross-platform alternative to %-I / %#I (which differ between Linux and Windows).
    Formats with %I (zero-padded) then strips a leading zero if present.
    """
    s = dt.strftime(fmt)
    return s.replace(" 0", " ", 1) if " 0" in s else s

def get_session_label() -> tuple:
    """Returns (session label string, embed color int) based on current ET time."""
    now = datetime.now(ET)
    minutes = now.hour * 60 + now.minute
    weekday = now.weekday()  # 0=Mon, 6=Sun

    if weekday >= 5:
        return "🔴 Market Closed", 0x607D8B
    if minutes < 240:        # Midnight – 4:00 AM
        return "🔴 Market Closed", 0x607D8B
    elif minutes < 570:      # 4:00 AM – 9:30 AM
        return "🌅 Pre-Market", 0xFFA726
    elif minutes < 960:      # 9:30 AM – 4:00 PM
        return "🟢 Market Open", 0x00C853
    elif minutes < 1200:     # 4:00 PM – 8:00 PM
        return "🌙 After-Hours", 0x5C6BC0
    else:
        return "🔴 Market Closed", 0x607D8B

async def get_yahoo_crumb(session: aiohttp.ClientSession) -> str | None:
    """
    Yahoo's v7/finance/quote endpoint now requires a session cookie + crumb token,
    mimicking what a real browser does. We fetch the homepage to collect the cookie,
    then call the crumb endpoint to get the token. Cached at module level until
    a request fails, since the crumb stays valid for a while.
    """
    global _yahoo_crumb
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    try:
        # Step 1: hit the homepage so aiohttp's cookie jar picks up Yahoo's session cookie
        async with session.get("https://fc.yahoo.com", headers=headers, timeout=aiohttp.ClientTimeout(total=10)):
            pass
        async with session.get("https://finance.yahoo.com", headers=headers, timeout=aiohttp.ClientTimeout(total=10)):
            pass

        # Step 2: request the crumb using that same cookie-bearing session
        async with session.get(
            "https://query2.finance.yahoo.com/v1/test/getcrumb",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                crumb = (await resp.text()).strip()
                if crumb and "Invalid" not in crumb:
                    _yahoo_crumb = crumb
                    return crumb
    except Exception as e:
        print(f"[crumb fetch error] {e}")
    return None

async def get_price_v8_fallback(session: aiohttp.ClientSession, ticker: str):
    """
    Reliable fallback using the chart endpoint (no auth required). Doesn't carry
    the overnight/POSTPOST session, but always works — used when the
    crumb-authenticated v7 call fails for any reason, so the bot never goes dark.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.upper()}?interval=1m&range=1d"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        result = data.get("chart", {}).get("result")
        if not result:
            return None
        meta = result[0]["meta"]

        regular_price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        pre_price = meta.get("preMarketPrice")
        post_price = meta.get("postMarketPrice")
        pre_time = meta.get("preMarketTime")
        post_time = meta.get("postMarketTime")
        regular_time = meta.get("regularMarketTime")

        if regular_price is None or prev_close is None:
            return None

        candidates = [
            (regular_time, regular_price),
            (post_time, post_price),
            (pre_time, pre_price),
        ]
        candidates = [(t, p) for t, p in candidates if t is not None and p is not None]
        last_ts, last_price = max(candidates, key=lambda x: x[0]) if candidates else (regular_time, regular_price)

        change = last_price - prev_close
        change_pct = (change / prev_close) * 100 if prev_close else 0
        as_of = format_time_no_leading_zero(datetime.fromtimestamp(last_ts, tz=ET), "%a %I:%M %p ET") if last_ts else "unknown"

        return {
            "ticker": ticker.upper(),
            "price": float(last_price),
            "prev_close": float(prev_close),
            "change": change,
            "change_pct": change_pct,
            "as_of": as_of,
            "market_state": "",  # unknown precise state on this fallback path
        }
    except Exception as e:
        print(f"[v8 fallback error] {ticker}: {e}")
        return None

async def get_price(session: aiohttp.ClientSession, ticker: str):
    """
    Fetch price from Yahoo Finance, preferring the authenticated v7/quote endpoint
    (exposes marketState including overnight/POSTPOST sessions) and falling back
    to the unauthenticated v8/chart endpoint if the crumb handshake fails.
    Returns a dict on success, None on failure.
    """
    global _yahoo_crumb

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}

    # Make sure we have a crumb before trying the authenticated endpoint
    if _yahoo_crumb is None:
        await get_yahoo_crumb(session)

    if _yahoo_crumb:
        url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={ticker.upper()}&crumb={_yahoo_crumb}"
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    result = data.get("quoteResponse", {}).get("result")
                    if result:
                        q = result[0]
                        market_state = q.get("marketState", "")
                        regular_price = q.get("regularMarketPrice")
                        prev_close = q.get("regularMarketPreviousClose")
                        regular_time = q.get("regularMarketTime")
                        pre_price = q.get("preMarketPrice")
                        pre_time = q.get("preMarketTime")
                        post_price = q.get("postMarketPrice")
                        post_time = q.get("postMarketTime")

                        if regular_price is not None and prev_close is not None:
                            if market_state == "PRE" and pre_price is not None:
                                last_price, last_ts = pre_price, pre_time
                            elif market_state == "POST" and post_price is not None:
                                last_price, last_ts = post_price, post_time
                            elif market_state == "REGULAR":
                                last_price, last_ts = regular_price, regular_time
                            else:
                                candidates = [
                                    (regular_time, regular_price),
                                    (post_time, post_price),
                                    (pre_time, pre_price),
                                ]
                                candidates = [(t, p) for t, p in candidates if t is not None and p is not None]
                                last_ts, last_price = max(candidates, key=lambda x: x[0]) if candidates else (regular_time, regular_price)

                            change = last_price - prev_close
                            change_pct = (change / prev_close) * 100 if prev_close else 0
                            as_of = format_time_no_leading_zero(datetime.fromtimestamp(last_ts, tz=ET), "%a %I:%M %p ET") if last_ts else "unknown"

                            return {
                                "ticker": ticker.upper(),
                                "price": float(last_price),
                                "prev_close": float(prev_close),
                                "change": change,
                                "change_pct": change_pct,
                                "as_of": as_of,
                                "market_state": market_state,
                            }
                elif resp.status == 401:
                    # Crumb went stale — clear it so the next call refreshes it
                    print(f"[yahoo error] {ticker}: HTTP 401, refreshing crumb next call")
                    _yahoo_crumb = None
                else:
                    print(f"[yahoo error] {ticker}: HTTP {resp.status}")
        except Exception as e:
            print(f"[price fetch error] {ticker}: {e}")

    # Fallback: reliable, unauthenticated endpoint (no overnight data, but never fails)
    return await get_price_v8_fallback(session, ticker)

def format_price_line(ticker: str, data) -> str:
    """Format a single ticker line for the embed description."""
    if data is None:
        return f"`{ticker:<5}` — unavailable"
    arrow = "▲" if data["change"] >= 0 else "▼"
    dot = "🟢" if data["change"] >= 0 else "🔴"

    state = data.get("market_state", "")
    state_tag = ""
    if state == "POSTPOST":
        state_tag = "  🌌 *overnight*"
    elif state == "PRE":
        state_tag = "  🌅 *pre-market*"
    elif state == "POST":
        state_tag = "  🌙 *after-hours*"

    return (
        f"{dot} `{ticker:<5}` **${data['price']:,.2f}**  "
        f"{arrow} {data['change']:+.2f} ({data['change_pct']:+.2f}%)  "
        f"*as of {data['as_of']}*{state_tag}"
    )

def is_market_open() -> bool:
    """Rough check — NYSE hours Mon-Fri 9:30–16:00 ET."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return time(9, 30) <= now.time() <= time(16, 0)


# ── Background Tasks ──────────────────────────────────────────────────────────

@tasks.loop(seconds=UPDATE_INTERVAL)
async def update_prices():
    channel = bot.get_channel(PRICE_CHANNEL_ID)
    if channel is None:
        return
    if http_session is None:
        return

    session = http_session
    lines = []
    new_prices = {}

    for ticker in TRACKED_STOCKS:
        data = await get_price(session, ticker)
        new_prices[ticker] = data["price"] if data else None
        lines.append(format_price_line(ticker, data))

    # Update cache (used by alert checks below)
    for ticker, price in new_prices.items():
        if price is not None:
            price_cache[ticker] = price

    # Build embed
    session_label, embed_color = get_session_label()
    ts = format_time_no_leading_zero(datetime.now(ET), "%I:%M:%S %p ET")
    embed = discord.Embed(
        title=f"📈 Live Stock Feed  •  {session_label}",
        description="\n".join(lines),
        color=embed_color,
    )
    embed.set_footer(text=f"Updated {ts}  •  Powered by Yahoo Finance")

    # Try to edit the last pinned message, otherwise send a new one
    try:
        pinned = [message async for message in channel.history(limit=50) if message.author == bot.user and message.pinned]
        if pinned:
            await pinned[0].edit(embed=embed)
        else:
            msg = await channel.send(embed=embed)
            await msg.pin()
    except Exception as e:
        print(f"[update_prices error] {e}")

    # ── Check Alerts ──────────────────────────────────────────────────────
    triggered = []
    for alert_id, alert in alerts.items():
        ticker = alert["ticker"]
        price = new_prices.get(ticker)
        if price is None:
            continue
        condition = alert["condition"]
        target = alert["price"]
        hit = (condition == "above" and price >= target) or \
              (condition == "below" and price <= target)
        if hit:
            triggered.append(alert_id)
            try:
                alert_channel = bot.get_channel(alert["channel_id"])
                if alert_channel:
                    await alert_channel.send(
                        f"🚨 <@{alert['user_id']}> **Alert triggered!** "
                        f"`{ticker}` is now **${price:,.2f}** "
                        f"({'above' if condition == 'above' else 'below'} your ${target:,.2f} target)"
                    )
            except Exception as e:
                print(f"[alert send error] {e}")

    # Remove triggered alerts
    for alert_id in triggered:
        del alerts[alert_id]
    if triggered:
        save_alerts()


@update_prices.before_loop
async def before_update():
    await bot.wait_until_ready()


# ── Slash Commands ────────────────────────────────────────────────────────────

@tree.command(name="price", description="Get the current price of a stock")
@app_commands.describe(ticker="Stock ticker symbol, e.g. AAPL")
async def price_cmd(interaction: discord.Interaction, ticker: str):
    await interaction.response.defer()
    if http_session is None:
        await interaction.followup.send("❌ Bot is still starting up, try again in a moment.")
        return
    data = await get_price(http_session, ticker.upper())
    if data is None:
        await interaction.followup.send(f"❌ Could not fetch price for `{ticker.upper()}`. Check the ticker or try again.")
    else:
        state_labels = {
            "PRE": "🌅 Pre-Market",
            "REGULAR": "🟢 Market Open",
            "POST": "🌙 After-Hours",
            "POSTPOST": "🌌 Overnight",
            "CLOSED": "🔴 Market Closed",
        }
        state_label = state_labels.get(data.get("market_state", ""), "")
        arrow = "▲" if data["change"] >= 0 else "▼"
        await interaction.followup.send(
            f"`{data['ticker']}` **${data['price']:,.2f}**  "
            f"{arrow} {data['change']:+.2f} ({data['change_pct']:+.2f}%) vs prev close  "
            f"| {state_label}  •  *as of {data['as_of']}*"
        )


@tree.command(name="alert", description="Set a price alert for a stock")
@app_commands.describe(
    ticker="Stock ticker, e.g. TSLA",
    condition="Trigger when price goes above or below target",
    target_price="The price level to alert at"
)
@app_commands.choices(condition=[
    app_commands.Choice(name="above", value="above"),
    app_commands.Choice(name="below", value="below"),
])
async def alert_cmd(
    interaction: discord.Interaction,
    ticker: str,
    condition: app_commands.Choice[str],
    target_price: float
):
    global alert_counter
    alert_counter += 1
    alert_id = alert_counter
    alerts[alert_id] = {
        "user_id": interaction.user.id,
        "channel_id": interaction.channel_id,
        "ticker": ticker.upper(),
        "condition": condition.value,
        "price": target_price,
    }
    save_alerts()
    await interaction.response.send_message(
        f"✅ Alert **#{alert_id}** set: notify when `{ticker.upper()}` goes "
        f"**{condition.value}** **${target_price:,.2f}**",
        ephemeral=True
    )


@tree.command(name="alerts", description="View your active price alerts")
async def alerts_cmd(interaction: discord.Interaction):
    user_alerts = {k: v for k, v in alerts.items() if v["user_id"] == interaction.user.id}
    if not user_alerts:
        await interaction.response.send_message("You have no active alerts.", ephemeral=True)
        return
    lines = [
        f"**#{aid}** — `{a['ticker']}` {a['condition']} **${a['price']:,.2f}**"
        for aid, a in user_alerts.items()
    ]
    embed = discord.Embed(title="Your Active Alerts", description="\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="removealert", description="Remove one of your price alerts")
@app_commands.describe(alert_id="The alert ID from /alerts")
async def remove_alert_cmd(interaction: discord.Interaction, alert_id: int):
    alert = alerts.get(alert_id)
    if alert is None:
        await interaction.response.send_message(f"❌ Alert #{alert_id} not found.", ephemeral=True)
        return
    if alert["user_id"] != interaction.user.id:
        await interaction.response.send_message("❌ That's not your alert.", ephemeral=True)
        return
    del alerts[alert_id]
    save_alerts()
    await interaction.response.send_message(f"🗑️ Alert **#{alert_id}** removed.", ephemeral=True)


@tree.command(name="track", description="Add a stock to the live price feed (admin only)")
@app_commands.describe(ticker="Stock ticker to add to the feed")
@app_commands.default_permissions(administrator=True)
async def track_cmd(interaction: discord.Interaction, ticker: str):
    t = ticker.upper()
    if t not in TRACKED_STOCKS:
        TRACKED_STOCKS.append(t)
        await interaction.response.send_message(f"✅ `{t}` added to the live feed.", ephemeral=True)
    else:
        await interaction.response.send_message(f"`{t}` is already being tracked.", ephemeral=True)


@tree.command(name="untrack", description="Remove a stock from the live feed (admin only)")
@app_commands.describe(ticker="Stock ticker to remove")
@app_commands.default_permissions(administrator=True)
async def untrack_cmd(interaction: discord.Interaction, ticker: str):
    t = ticker.upper()
    if t in TRACKED_STOCKS:
        TRACKED_STOCKS.remove(t)
        price_cache.pop(t, None)
        await interaction.response.send_message(f"🗑️ `{t}` removed from the live feed.", ephemeral=True)
    else:
        await interaction.response.send_message(f"`{t}` isn't being tracked.", ephemeral=True)


# ── Bot Events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    global http_session
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    load_alerts()

    # Create one persistent session for the bot's lifetime so cookies from the
    # Yahoo crumb handshake are retained between price fetches.
    if http_session is None:
        # Yahoo's homepage response includes very large security headers (CSP, etc.)
        # that exceed aiohttp's default 8190-byte header line limit, causing the
        # crumb handshake to fail outright. Raising these limits fixes it.
        http_session = aiohttp.ClientSession(
            max_line_size=2**15,
            max_field_size=2**15,
        )

    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"❌ Slash command sync failed: {e}")

    if not update_prices.is_running():
        update_prices.start()


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN not set in environment")
    bot.run(DISCORD_TOKEN)
