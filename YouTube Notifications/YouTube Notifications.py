import os
from datetime import datetime, date
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None

import discord
import feedparser
from discord.ext import tasks

# ---------------------------
# CONFIG
# ---------------------------
DISCORD_TOKEN = "your_Discord_Bot_Token"

# Channel to send announcements into (numeric)
CHANNEL_ID = your_channel_ID

# YouTube channel ID (default kept from earlier)
YT_CHANNEL_ID = "your_YT_channel_ID"

# Which days/times to post scheduled livestream reminders (Europe/London)
LIVESTREAM_DAYS = ["Day", "Day"] # etc.
LIVESTREAM_TIMES = ["HH:MM"]  # HH:MM (24-hour) - can have multiple values

# What mention to use in announcements (leave blank to disable mention)
ANNOUNCE_MENTION = "@everyone"

# Persistence file for the last known video link
LAST_VIDEO_FILE = "last_video.txt"

# RSS feed URL for the YouTube uploads
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}"

# ---------------------------
# Discord client setup
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# ---------------------------
# Helpers: persistence & time
# ---------------------------
def load_last_video():
    if os.path.isfile(LAST_VIDEO_FILE):
        try:
            with open(LAST_VIDEO_FILE, "r", encoding="utf-8") as f:
                return f.read().strip() or None
        except Exception as e:
            print("Failed to read last_video file:", e)
    return None

def save_last_video(link):
    try:
        with open(LAST_VIDEO_FILE, "w", encoding="utf-8") as f:
            f.write(link or "")
    except Exception as e:
        print("Failed to write last_video file:", e)

last_video_link = load_last_video()

announced_today = {}  # keys: (weekday, time_str) -> iso-date

def london_now():
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Europe/London"))
    else:
        return datetime.now()

# ---------------------------
# Periodic tasks (defined, started in on_ready)
# ---------------------------
@tasks.loop(minutes=5)
async def check_new_video():
    """Poll the YouTube RSS feed for new videos or live posts."""
    global last_video_link
    try:
        feed = feedparser.parse(RSS_URL)
    except Exception as e:
        print("RSS parse error:", e)
        return

    entries = getattr(feed, "entries", None)
    if not entries:
        # no entries is possible if network fails or bad feed
        print("No entries in RSS feed (or fetch failed).")
        return

    latest = entries[0]
    link = latest.get("link")
    title = latest.get("title", "New video")
    if not link:
        return

    if link != last_video_link:
        last_video_link = link
        save_last_video(link)
        ch = client.get_channel(CHANNEL_ID)
        if ch:
            try:
                await ch.send(f"{ANNOUNCE_MENTION} New video alert! **{title}** is available: {link}")
            except Exception as e:
                print("Failed to send new video announcement:", e)
        else:
            print("Channel not found (check CHANNEL_ID and bot permissions).")

@check_new_video.before_loop
async def before_check():
    await client.wait_until_ready()

@tasks.loop(seconds=60)
async def scheduled_livestream_loop():
    """
    Every minute, check if current London day/time matches schedule.
    Post once per (weekday,time) per calendar day.
    """
    now = london_now()
    weekday = now.strftime("%A")
    current_time_str = now.strftime("%H:%M")

    for tstr in LIVESTREAM_TIMES:
        try:
            # validate time format
            parsed = datetime.strptime(tstr, "%H:%M").time()
        except Exception:
            continue
        if weekday in LIVESTREAM_DAYS and current_time_str == parsed.strftime("%H:%M"):
            key = (weekday, parsed.strftime("%H:%M"))
            if announced_today.get(key) == date.today().isoformat():
                continue  # already announced this slot today
            channel = client.get_channel(CHANNEL_ID)
            if channel:
                try:
                    channel_url = f"https://www.youtube.com/channel/{YT_CHANNEL_ID}"
                    await channel.send(
                        f"{ANNOUNCE_MENTION} It's {weekday} — livestream reminder! Check the channel: {channel_url}\n The LiveStream: {link} \n"
                        "If the stream hasn't started yet, this is your reminder to go live."
                    )
                    announced_today[key] = date.today().isoformat()
                except Exception as e:
                    print("Failed to send scheduled livestream reminder:", e)
            else:
                print("Channel not found for livestream reminder (check CHANNEL_ID).")

@scheduled_livestream_loop.before_loop
async def before_stream_loop():
    await client.wait_until_ready()

@tasks.loop(hours=1)
async def cleanup_announced():
    """Remove announced entries that are not today's date so next day can re-announce."""
    today_iso = date.today().isoformat()
    to_remove = [k for k, v in announced_today.items() if v != today_iso]
    for k in to_remove:
        announced_today.pop(k, None)

@cleanup_announced.before_loop
async def before_cleanup():
    await client.wait_until_ready()

# ---------------------------
# Events & simple commands
# ---------------------------
@client.event
async def on_ready():
    print(f"Logged in as: {client.user} (id: {client.user.id})")
    # Start loops safely (only when the event loop is running)
    try:
        if not check_new_video.is_running():
            check_new_video.start()
    except RuntimeError:
        pass

    try:
        if not scheduled_livestream_loop.is_running():
            scheduled_livestream_loop.start()
    except RuntimeError:
        pass

    try:
        if not cleanup_announced.is_running():
            cleanup_announced.start()
    except RuntimeError:
        pass

@client.event
async def on_message(message):
    global last_video_link
    if message.author.bot:
        return

    content = message.content.strip().lower()
    if content.startswith("!last_video") or content.startswith("!latest"):
        if last_video_link:
            await message.channel.send(f"Latest known video: {last_video_link}")
        else:
            await message.channel.send("I don't have a last video saved yet. Wait for the next check (every 5 minutes).")

# ---------------------------
# Run bot
# ---------------------------
if not DISCORD_TOKEN:
    print("No DISCORD_TOKEN configured in the file. Edit the DISCORD_TOKEN variable at the top.")
else:
    try:
        client.run(DISCORD_TOKEN)
    except Exception as e:
        print("Error running the Discord client:", e)
