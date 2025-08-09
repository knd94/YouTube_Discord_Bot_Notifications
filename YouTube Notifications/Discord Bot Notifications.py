import os
import re
import json
import asyncio
from datetime import datetime, date
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

import aiohttp
import feedparser
import discord
from discord.ext import tasks

# ---------------------------
# CONFIG
# ---------------------------
DISCORD_TOKEN = "YOUR_BOT_TOKEN"

CHANNEL_ID = YOUR_CHANNEL_ID # Numbers only 

YT_CHANNEL_ID = "YOUR_YOUTUBE_CHANNEL_ID"

channel_url = "YOUR_YOUTUBE_CHANNEL_URL"

TWITCH_LINK = "YOUR_TWITCH_LINK"

KICK_LINK = "YOUR_KICK_LINK"

TIKTOK_USERNAME = "YOUR_TIKTOK_USERNAME_(WITHOUT_@)"

LIVESTREAM_DAYS = ["Day", "Day"] # Insert as many days as you would like, e.g., ["Friday", "Saturday"]

LIVESTREAM_TIMES = ["HH:MM"] # 24 hour clock. E.g. 14=2pm

ANNOUNCE_MENTION = "@everyone"
LAST_VIDEO_FILE = "last_video.txt"
LAST_TIKTOK_FILE = "last_tiktok.txt"

# Playwright fallback options
PLAYWRIGHT_HEADLESS = True
PLAYWRIGHT_NAV_TIMEOUT_MS = 60000

# Debug / behavior toggles
DEBUG_LIST_GUILDS = False
DEBUG_SCRAPE_STARTUP = True
DEBUG_PLAYWRIGHT_VISIBLE = False

RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL_ID}"

# ---------------------------
# Globals / persistence
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

def london_now():
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo("Europe/London"))
    return datetime.now()

def load_text_file(path):
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip() or None
        except Exception as e:
            print(f"[load_text_file] Failed to read {path}: {e}")
    return None

def save_text_file(path, text):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text or "")
    except Exception as e:
        print(f"[save_text_file] Failed to write {path}: {e}")

last_video_link = load_text_file(LAST_VIDEO_FILE)
last_tiktok_link = load_text_file(LAST_TIKTOK_FILE)

# make sure announced_today exists (fixes NameError)
announced_today = {}

# concurrency primitives to avoid duplicate announces
tiktok_lock = asyncio.Lock()
pending_announcements = set()

# ---------------------------
# Channel access helpers
# ---------------------------
async def _get_channel_safe(preferred_channel_id):
    # try cache
    ch = client.get_channel(preferred_channel_id)
    if ch:
        return ch
    # try fetch
    try:
        ch = await client.fetch_channel(preferred_channel_id)
        return ch
    except discord.errors.Forbidden:
        print(f"[get_channel_safe] fetch_channel forbidden for {preferred_channel_id}")
    except discord.errors.NotFound:
        print(f"[get_channel_safe] Channel id {preferred_channel_id} not found.")
    except Exception as e:
        print(f"[get_channel_safe] fetch_channel failed: {e}")

    # fallback: use any text channel the bot can send to
    for g in client.guilds:
        for chcand in g.text_channels:
            perms = chcand.permissions_for(g.me or client.user)
            if perms.view_channel and perms.send_messages:
                print(f"[get_channel_safe] Using fallback channel #{chcand.name} (id={chcand.id}) in guild {g.name}")
                return chcand
    return None

def _print_guild_channel_info():
    print("=== Bot guilds and accessible text channels ===")
    for g in client.guilds:
        print(f"Guild: {g.name} (id={g.id})")
        try:
            me = g.me
        except Exception:
            me = None
        for ch in g.text_channels[:50]:
            perms = ch.permissions_for(g.me or client.user)
            print(f"  - #{ch.name} (id={ch.id}) send={perms.send_messages} view={perms.view_channel}")
    print("=== end guild list ===")

# ---------------------------
# YouTube RSS checker (safe send)
# ---------------------------
@tasks.loop(minutes=5)
async def check_youtube():
    global last_video_link
    try:
        feed = feedparser.parse(RSS_URL)
    except Exception as e:
        print("[check_youtube] RSS parse error:", e)
        return
    entries = getattr(feed, "entries", None)
    if not entries:
        return
    latest = entries[0]
    link = latest.get("link")
    title = latest.get("title", "New video")
    if not link:
        return
    if link == last_video_link:
        return
    ch = await _get_channel_safe(CHANNEL_ID)
    if ch is None:
        print("[check_youtube] Channel not available to announce.")
        return
    try:
        await ch.send(f"{ANNOUNCE_MENTION} New YouTube video: **{title}** — {link}")
        last_video_link = link
        save_text_file(LAST_VIDEO_FILE, link)
        print("[check_youtube] Announced and saved:", link)
    except Exception as e:
        print("[check_youtube] Failed to send YouTube announcement:", e)

@check_youtube.before_loop
async def before_check_youtube():
    await client.wait_until_ready()

# ---------------------------
# Playwright renderer fallback
# ---------------------------
async def render_page_with_playwright(url, timeout_ms=PLAYWRIGHT_NAV_TIMEOUT_MS, visible=False, debug=False):
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        if debug:
            print("[playwright] import/install error:", e)
        return None

    for attempt in (1,2):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=(not visible),
                                                  args=["--no-sandbox","--disable-dev-shm-usage","--disable-blink-features=AutomationControlled"])
                page = await browser.new_page()
                await page.set_extra_http_headers({"Accept-Language":"en-GB,en-US;q=0.9"})
                try:
                    await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                except Exception as nav_exc:
                    if debug:
                        print(f"[playwright] goto attempt {attempt} error:", nav_exc)
                    await page.close(); await browser.close()
                    if attempt==1:
                        await asyncio.sleep(0.5); continue
                    return None
                try:
                    await page.wait_for_selector('a[href*="/video/"]', timeout=5000)
                except Exception:
                    if debug:
                        print("[playwright] video anchor selector not found (continuing).")
                content = await page.content()
                await page.close(); await browser.close()
                return content
        except Exception as e:
            if debug:
                print(f"[playwright] attempt {attempt} exception:", e)
            await asyncio.sleep(0.5)
    return None

# ---------------------------
# TikTok scraper + fallback
# ---------------------------
async def scrape_tiktok_latest(username, debug=False, use_playwright_fallback=True, visible_playwright=False):
    if not username:
        return None
    candidates = [
        f"https://www.tiktok.com/@{username}",
        f"https://www.tiktok.com/@{username}?lang=en",
        f"https://www.tiktok.com/@{username}?is_copy_url=1",
    ]
    headers = {
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language":"en-GB,en-US;q=0.9"
    }

    def extract_from_html(text):
        if not text:
            return None
        m = re.search(r'href=["\'](/@[^/]+/video/(\d+))["\']', text)
        if m:
            return "https://www.tiktok.com" + m.group(1)
        m2 = re.search(r'"videoId"\s*:\s*"(\d+)"', text)
        if m2:
            return f"https://www.tiktok.com/@{username}/video/{m2.group(1)}"
        m3 = re.search(r'(https?://www\.tiktok\.com/@[^/]+/video/\d+)', text)
        if m3:
            return m3.group(1)
        m4 = re.search(r'window\.__SIGI_STATE__\s*=\s*({.*?});', text, flags=re.S)
        if not m4:
            m4 = re.search(r'window\.__INIT_PROPS__\s*=\s*({.*?});', text, flags=re.S)
        if m4:
            try:
                blob = json.loads(m4.group(1))
                def find_video(o):
                    if isinstance(o,str):
                        if f"/@{username}/video/" in o: return o
                    elif isinstance(o,dict):
                        for v in o.values():
                            r = find_video(v)
                            if r: return r
                    elif isinstance(o,list):
                        for i in o:
                            r = find_video(i)
                            if r: return r
                    return None
                r = find_video(blob)
                if r:
                    if r.startswith("/@"):
                        return "https://www.tiktok.com" + r
                    return r
            except Exception:
                pass
        return None

    for url in candidates:
        text = None; status=None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=15000) as resp:
                    status = resp.status
                    text = await resp.text(errors="ignore")
        except Exception as e:
            if debug:
                print(f"[scrape] aiohttp fetch {url} error: {e}")
            text=None

        if debug:
            print("="*30)
            print(f"[scrape] tried {url} status={status} len={(len(text) if text else 'None')}")
            if text:
                print("page preview:\n", text[:1200])
        if text:
            found = extract_from_html(text)
            if found:
                if debug: print("[scrape] found via aiohttp:", found)
                return found

    # playwright fallback
    if use_playwright_fallback:
        if debug: print("[scrape] falling back to Playwright (visible=%s)"%visible_playwright)
        rendered = await render_page_with_playwright(f"https://www.tiktok.com/@{username}", timeout_ms=PLAYWRIGHT_NAV_TIMEOUT_MS, visible=visible_playwright, debug=debug)
        if rendered:
            if debug:
                print("[scrape] Playwright returned len:", len(rendered))
                print("Playwright preview:\n", rendered[:1200])
            found2 = extract_from_html(rendered)
            if found2:
                if debug: print("[scrape] found via Playwright:", found2)
                return found2
            else:
                if debug: print("[scrape] Playwright rendered but no video pattern found.")
        else:
            if debug: print("[scrape] Playwright failed to render or not installed.")
    if debug: print("[scrape] no video found for", username)
    return None

# ---------------------------
# Announce helper — single-run protected by lock
# ---------------------------
async def check_and_announce_tiktok_once(debug=False, visible_playwright=False):
    global last_tiktok_link
    if not TIKTOK_USERNAME:
        return

    latest = None
    try:
        latest = await scrape_tiktok_latest(TIKTOK_USERNAME, debug=debug, use_playwright_fallback=True, visible_playwright=visible_playwright)
    except Exception as e:
        print("[tiktok] scrape exception:", e)
        latest = None

    if not latest:
        if debug: print("[tiktok] no latest found")
        return

    async with tiktok_lock:
        # skip if already persisted or currently pending
        if latest == last_tiktok_link:
            if debug: print("[tiktok] latest already saved; skipping:", latest)
            return
        if latest in pending_announcements:
            if debug: print("[tiktok] latest already pending; skipping:", latest)
            return

        pending_announcements.add(latest)
        ch = await _get_channel_safe(CHANNEL_ID)
        if ch is None:
            print("[check_tiktok] Channel not available — cannot announce. Will retry later.")
            pending_announcements.discard(latest)
            if DEBUG_LIST_GUILDS: _print_guild_channel_info()
            return

        try:
            await ch.send(f"{ANNOUNCE_MENTION} New TikTok from @{TIKTOK_USERNAME}: {latest}")
            last_tiktok_link = latest
            save_text_file(LAST_TIKTOK_FILE, latest)
            print("[check_tiktok] Announced and saved:", latest)
        except Exception as e:
            print("[check_tiktok] Failed to send TikTok announcement:", e)
        finally:
            pending_announcements.discard(latest)

# ---------------------------
# Periodic wrappers
# ---------------------------
@tasks.loop(minutes=5)
async def check_tiktok_periodic():
    await check_and_announce_tiktok_once(debug=False, visible_playwright=DEBUG_PLAYWRIGHT_VISIBLE)

@check_tiktok_periodic.before_loop
async def before_check_tiktok_periodic():
    await client.wait_until_ready()

# Live schedule reminder (unchanged)
@tasks.loop(seconds=60)
async def scheduled_livestream_loop():
    now = london_now()
    weekday = now.strftime("%A")
    current_time_str = now.strftime("%H:%M")
    for tstr in LIVESTREAM_TIMES:
        try:
            parsed_time = datetime.strptime(tstr, "%H:%M").time()
        except Exception:
            continue
        if weekday in LIVESTREAM_DAYS and current_time_str == parsed_time.strftime("%H:%M"):
            key = (weekday, parsed_time.strftime("%H:%M"))
            if announced_today.get(key) == date.today().isoformat():
                continue
            ch = await _get_channel_safe(CHANNEL_ID)
            if ch:
                try:
                    await ch.send(f"{ANNOUNCE_MENTION} It's {weekday} — livestream alert :red_circle:! Check the channel: {channel_url}\n \n The LiveStream: {TWITCH_LINK} {KICK_LINK}")
                    announced_today[key] = date.today().isoformat()
                except Exception as e:
                    print("[scheduled] Failed to send reminder:", e)
            else:
                print("[scheduled] Channel not found or inaccessible.")

@scheduled_livestream_loop.before_loop
async def before_sched_loop():
    await client.wait_until_ready()

# Cleanup announced map hourly (fix uses announced_today global)
@tasks.loop(hours=1)
async def cleanup_announced():
    global announced_today
    today_iso = date.today().isoformat()
    keys_to_remove = [k for k, v in announced_today.items() if v != today_iso]
    for k in keys_to_remove:
        announced_today.pop(k, None)

@cleanup_announced.before_loop
async def before_cleanup():
    await client.wait_until_ready()

# ---------------------------
# Events & startup order fix (one-shot BEFORE loops)
# ---------------------------
@client.event
async def on_ready():
    print(f"Logged in as: {client.user} (id: {client.user.id})")
    if DEBUG_LIST_GUILDS:
        _print_guild_channel_info()

    # do the one-shot check first to avoid racing with periodic loops
    if DEBUG_SCRAPE_STARTUP:
        print("[startup] running immediate TikTok scrape (one-shot)")
        await check_and_announce_tiktok_once(debug=True, visible_playwright=DEBUG_PLAYWRIGHT_VISIBLE)

    # Now start periodic tasks
    try:
        if not check_youtube.is_running():
            check_youtube.start()
    except Exception:
        pass
    try:
        if not check_tiktok_periodic.is_running():
            check_tiktok_periodic.start()
    except Exception:
        pass
    try:
        if not scheduled_livestream_loop.is_running():
            scheduled_livestream_loop.start()
    except Exception:
        pass
    try:
        if not cleanup_announced.is_running():
            cleanup_announced.start()
    except Exception:
        pass

@client.event
async def on_message(message):
    if message.author.bot:
        return
    content = message.content.strip().lower()
    if content.startswith("!last_tiktok"):
        if last_tiktok_link:
            await message.channel.send(f"Latest known TikTok: {last_tiktok_link}")
        else:
            await message.channel.send("I don't have a last TikTok saved yet.")
    elif content.startswith("!last_video") or content.startswith("!latest"):
        if last_video_link:
            await message.channel.send(f"Latest known YouTube video: {last_video_link}")
        else:
            await message.channel.send("I don't have a last YouTube saved yet.")

# ---------------------------
# Run the bot
# ---------------------------
if not DISCORD_TOKEN:
    print("No DISCORD_TOKEN provided; set it in the file or env.")
    raise SystemExit(1)

try:
    client.run(DISCORD_TOKEN)
except Exception as e:
    print("[main] client.run error:", e)
