import os
import discord
import feedparser
from discord.ext import tasks
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Channel ID and Bot Token
DISCORD_TOKEN = 'YOUR_BOT_TOKEN'
CHANNEL_ID = 'YOUR_CHANNEL_ID'

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Variable to store the last video link to prevent duplicate announcements
last_video_link = None

@client.event
async def on_ready():
    print(f'We have logged in as {client.user}')
    check_new_video.start()  # Start the task loop

@tasks.loop(minutes=5)
async def check_new_video():
    global last_video_link
    # Use the RSS feed for the YouTube channel
    rss_url = 'https://www.youtube.com/feeds/videos.xml?channel_id=YOUR_CHANNEL_ID'
    feed = feedparser.parse(rss_url)
    
    # Check if there are entries in the feed
    if feed.entries:
        latest_entry = feed.entries[0]
        video_link = latest_entry.link
        video_title = latest_entry.title

        if video_link != last_video_link:
            last_video_link = video_link
            channel = client.get_channel(int(CHANNEL_ID))
            if channel:
                await channel.send(f"@everyone New video alert! **{video_title}** is now live: {video_link}\n\nCheck it out!")
    else:
        print("No new videos found.")
        
@check_new_video.before_loop
async def before_check():
    await client.wait_until_ready()

client.run(DISCORD_TOKEN) 
