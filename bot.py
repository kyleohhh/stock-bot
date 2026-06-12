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

# ── Bot Setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# In-memory alert storage
alerts: dict = {}
alert_counter = 0

# Cache last known prices
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

async def get_price(session: aiohttp.ClientSession, ticker: str) -> float | None:
    """Fetch the latest price from Yahoo Finance (unofficial API)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker.upper()}?interval=1m&range=1d"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                result = data["chart"]["result"]
                if result:
                    meta = result[0]["meta"]
                    # Use regularMarketPrice for current/last price
                    price = meta.get("regularMarketPrice")
                    if price:
                        return float(price)
    except Exception as e:
        print(f"[price fetch error] {ticker}: {e}")
    return None

def format_price_line(ticker: str, price: float | None, prev: float | None) -> str:
    if price is None:
        return f"`{ticker:<5}` — unavailable"
    arrow = ""
    if prev is not None:
        change = price - prev
        pct = (change / prev) * 100
        arrow = f"  {'🟢 +' if change >= 0 else '🔴 '}{change:+.2f} ({pct:+.2f}%)"
    return f"`{ticker:<5}` **${price:,.2f}**{arrow}"

def is_market_open() -> bool:
    """Rough check — NYSE hours Mon-Fri 9:30–16:00 ET."""
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    if now.weekday() >= 5:
        return False
    market_open = time(9, 30)
    market_close = time(16, 0)
    return market_open <= now.time() <= market_close


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
            price = await get_price(session, ticker)
            prev = price_cache.get(ticker)
            new_prices[ticker] = price
            lines.append(format_price_line(ticker, price, prev))

        # Update cache
        for ticker, price in new_prices.items():
            if price is not None:
                price_cache[ticker] = price

        # Build embed
        status = "🟢 Market Open" if is_market_open() else "🔴 Market Closed"
        ts = datetime.now(pytz.timezone("America/New_York")).strftime("%I:%M:%S %p ET")
        embed = discord.Embed(
            title=f"📈 Live Stock Feed  •  {status}",
            description="\n".join(lines),
            color=0x00C853 if is_market_open() else 0x607D8B,
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
        p = await get_price(session, ticker.upper())
    if p is None:
        await interaction.followup.send(f"❌ Could not fetch price for `{ticker.upper()}`. Check the ticker or try again.")
    else:
        await interaction.followup.send(f"`{ticker.upper()}` is currently **${p:,.2f}**")


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
