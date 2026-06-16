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

async def get_price(session: aiohttp.ClientSession, ticker: str):
    """
    Fetch price from Yahoo Finance's chart API (free, real-time).
    Returns the most current price 24/7 by checking session and falling back
    through preMarketPrice / postMarketPrice / regularMarketPrice as appropriate —
    Robinhood-style always-on last price.
    Returns a dict on success, None on failure.
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.upper()}?interval=1m&range=1d"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                print(f"[yahoo error] {ticker}: HTTP {resp.status}")
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
        pre_time = meta.get("preMarketTime")          # Unix seconds
        post_time = meta.get("postMarketTime")          # Unix seconds
        regular_time = meta.get("regularMarketTime")    # Unix seconds

        if regular_price is None or prev_close is None:
            return None

        label, _ = get_session_label()

        # Pick the most relevant price for the current session
        if "Pre-Market" in label and pre_price is not None:
            last_price, last_ts = pre_price, pre_time
        elif "After-Hours" in label and post_price is not None:
            last_price, last_ts = post_price, post_time
        else:
            # Market open, or closed with no fresher extended-hours data —
            # use whichever timestamp is most recent among what's available
            candidates = [
                (regular_time, regular_price),
                (post_time, post_price),
                (pre_time, pre_price),
            ]
            candidates = [(t, p) for t, p in candidates if t is not None and p is not None]
            last_ts, last_price = max(candidates, key=lambda x: x[0]) if candidates else (regular_time, regular_price)

        change = last_price - prev_close
        change_pct = (change / prev_close) * 100 if prev_close else 0

        if last_ts:
            dt = datetime.fromtimestamp(last_ts, tz=ET)
            as_of = format_time_no_leading_zero(dt, "%a %I:%M %p ET")
        else:
            as_of = "unknown"

        return {
            "ticker": ticker.upper(),
            "price": float(last_price),
            "prev_close": float(prev_close),
            "change": change,
            "change_pct": change_pct,
            "as_of": as_of,
        }

    except Exception as e:
        print(f"[price fetch error] {ticker}: {e}")
        return None

def format_price_line(ticker: str, data) -> str:
    """Format a single ticker line for the embed description."""
    if data is None:
        return f"`{ticker:<5}` — unavailable"
    arrow = "▲" if data["change"] >= 0 else "▼"
    dot = "🟢" if data["change"] >= 0 else "🔴"
    return (
        f"{dot} `{ticker:<5}` **${data['price']:,.2f}**  "
        f"{arrow} {data['change']:+.2f} ({data['change_pct']:+.2f}%)  "
        f"*as of {data['as_of']}*"
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

    async with aiohttp.ClientSession() as session:
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
    async with aiohttp.ClientSession() as session:
        data = await get_price(session, ticker.upper())
    if data is None:
        await interaction.followup.send(f"❌ Could not fetch price for `{ticker.upper()}`. Check the ticker or try again.")
    else:
        session_label, _ = get_session_label()
        arrow = "▲" if data["change"] >= 0 else "▼"
        await interaction.followup.send(
            f"`{data['ticker']}` **${data['price']:,.2f}**  "
            f"{arrow} {data['change']:+.2f} ({data['change_pct']:+.2f}%) vs prev close  "
            f"| {session_label}  •  *as of {data['as_of']}*"
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
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    load_alerts()
    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"❌ Slash command sync failed: {e}")
    update_prices.start()


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN not set in environment")
    bot.run(DISCORD_TOKEN)
