import os
import json
import time
import asyncio
import logging
from datetime import datetime, timedelta
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
import yt_dlp
import glob
import shutil
from flask import Flask
from threading import Thread
from dotenv import load_dotenv

load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_IDS = [int(os.getenv("ADMIN_USER_IDS"))]

print(ADMIN_USER_IDS, type(ADMIN_USER_IDS[0]))

# Data files
USERS_DATA_FILE = "users_data.json"
VIDEOS_DATA_FILE = "videos_data.json"
BOT_DATA_FILE = "bot_data.json"

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize bot
app = Client(
    "video_downloader_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

flask_app = Flask(__name__)


@flask_app.route('/')
def home():
    return "Video Downloader Bot is running!"


def run_flask():
    """Run Flask app in a separate thread"""
    flask_app.run(host='0.0.0.0', port=5000)


# Data management functions


def load_json_data(filename, default_data=None):
    """Load data from JSON file"""
    if default_data is None:
        default_data = {}
    try:
        if os.path.exists(filename):
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        return default_data
    except Exception as e:
        logger.error(f"Error loading {filename}: {e}")
        return default_data


def save_json_data(filename, data):
    """Save data to JSON file"""
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        return True
    except Exception as e:
        logger.error(f"Error saving {filename}: {e}")
        return False


def is_admin(user_id):
    """Check if user is admin"""
    return user_id in ADMIN_USER_IDS

# Global state management for strict limits


class BotLimits:
    def __init__(self):
        self.max_concurrent_downloads = 2
        self.max_users_per_day = 2
        self.max_videos_per_user = 2
        self.max_total_daily_downloads = 3

        self.active_downloads = set()  # Track active download user_ids

        # Load or initialize bot data
        self.bot_data = load_json_data(BOT_DATA_FILE, {
            'last_reset_date': str(datetime.now().date()),
            'total_downloads_today': 0,
            'users_today': [],
            'user_downloads_today': {},
            'total_users': 0,
            'total_downloads_all_time': 0,
            'bot_start_date': str(datetime.now().date())
        })

        # Reset if needed on startup
        self.reset_daily_stats_if_needed()

    def reset_daily_stats_if_needed(self):
        """Reset stats if it's a new day"""
        current_date = str(datetime.now().date())

        if current_date != self.bot_data.get('last_reset_date'):
            logger.info(f"Resetting daily stats for new day: {current_date}")

            # Reset daily counters
            self.bot_data['last_reset_date'] = current_date
            self.bot_data['total_downloads_today'] = 0
            self.bot_data['users_today'] = []
            self.bot_data['user_downloads_today'] = {}

            # Clear active downloads (in case of bot restart)
            self.active_downloads.clear()

            # Save the reset data
            save_json_data(BOT_DATA_FILE, self.bot_data)

            logger.info("Daily stats have been reset successfully")

    def can_user_download(self, user_id):
        """Check if user can make a download request"""
        self.reset_daily_stats_if_needed()

        # Check if user is already downloading
        if user_id in self.active_downloads:
            return False, "❌ You already have an active download. Please wait."

        # Check concurrent downloads limit
        if len(self.active_downloads) >= self.max_concurrent_downloads:
            return False, f"⏳ Server busy. Maximum {self.max_concurrent_downloads} downloads allowed simultaneously."

        # Check daily total downloads limit
        if self.bot_data['total_downloads_today'] >= self.max_total_daily_downloads:
            return False, f"📊 Daily limit reached. Maximum {self.max_total_daily_downloads} downloads per day for all users."

        # Check daily users limit
        if len(self.bot_data['users_today']) >= self.max_users_per_day and user_id not in self.bot_data['users_today']:
            return False, f"👥 Daily user limit reached. Maximum {self.max_users_per_day} users can download per day."

        # Check user's daily video limit
        user_downloads_today = self.bot_data['user_downloads_today'].get(
            str(user_id), 0)
        if user_downloads_today >= self.max_videos_per_user:
            return False, f"🎥 You've reached your daily limit of {self.max_videos_per_user} videos."

        return True, "✅ You can download"

    def start_download(self, user_id):
        """Mark user as having started a download"""
        self.active_downloads.add(user_id)

    def complete_download(self, user_id, success=True):
        """Mark download as completed"""
        self.active_downloads.discard(user_id)

        if success:
            self.reset_daily_stats_if_needed()

            # Update daily stats
            self.bot_data['total_downloads_today'] += 1
            self.bot_data['total_downloads_all_time'] += 1

            # Add user to today's users if not already there
            if user_id not in self.bot_data['users_today']:
                self.bot_data['users_today'].append(user_id)

            # Update user's daily download count
            user_key = str(user_id)
            self.bot_data['user_downloads_today'][user_key] = self.bot_data['user_downloads_today'].get(
                user_key, 0) + 1

            # Save updated data
            save_json_data(BOT_DATA_FILE, self.bot_data)

    def get_stats(self):
        """Get current bot statistics"""
        self.reset_daily_stats_if_needed()
        return {
            'active_downloads': len(self.active_downloads),
            'daily_downloads': self.bot_data['total_downloads_today'],
            'users_today': len(self.bot_data['users_today']),
            'remaining_downloads': self.max_total_daily_downloads - self.bot_data['total_downloads_today'],
            'total_downloads_all_time': self.bot_data['total_downloads_all_time'],
            'total_users': self.bot_data['total_users'],
            'bot_start_date': self.bot_data['bot_start_date']
        }


# Initialize limits manager
limits = BotLimits()

# User data storage


def save_user_data(user_id, user_info, video_url=None):
    """Save user data to JSON file"""
    users_data = load_json_data(USERS_DATA_FILE, {})

    user_key = str(user_id)
    current_time = datetime.now().isoformat()

    if user_key not in users_data:
        users_data[user_key] = {
            'user_id': user_id,
            'first_name': user_info.get('first_name', ''),
            'last_name': user_info.get('last_name', ''),
            'username': user_info.get('username', ''),
            'first_seen': current_time,
            'last_seen': current_time,
            'total_downloads': 0,
            'videos_downloaded': []
        }
        # Update total users count
        limits.bot_data['total_users'] += 1
        save_json_data(BOT_DATA_FILE, limits.bot_data)
    else:
        # Update last seen
        users_data[user_key]['last_seen'] = current_time
        # Update user info in case it changed
        users_data[user_key]['first_name'] = user_info.get('first_name', '')
        users_data[user_key]['last_name'] = user_info.get('last_name', '')
        users_data[user_key]['username'] = user_info.get('username', '')

    if video_url:
        users_data[user_key]['total_downloads'] += 1

    save_json_data(USERS_DATA_FILE, users_data)


def save_video_data(user_id, video_info):
    """Save video download data to JSON file"""
    videos_data = load_json_data(VIDEOS_DATA_FILE, [])

    video_record = {
        'user_id': user_id,
        'video_url': video_info.get('url', ''),
        'video_title': video_info.get('title', ''),
        'duration': video_info.get('duration', 0),
        'format': video_info.get('format', ''),
        'file_size': video_info.get('file_size', 0),
        'download_date': datetime.now().isoformat(),
        'success': video_info.get('success', True)
    }

    videos_data.append(video_record)
    save_json_data(VIDEOS_DATA_FILE, videos_data)

    # Also update user's video list
    users_data = load_json_data(USERS_DATA_FILE, {})
    user_key = str(user_id)
    if user_key in users_data:
        users_data[user_key]['videos_downloaded'].append({
            'title': video_info.get('title', ''),
            'url': video_info.get('url', ''),
            'date': datetime.now().isoformat()
        })
        save_json_data(USERS_DATA_FILE, users_data)


# User data storage (kept minimal for active sessions)
user_data = {}


@app.on_message(filters.command("start"))
async def start_command(client: Client, message: Message):
    """Start command with current limits info"""
    user_info = {
        'first_name': message.from_user.first_name,
        'last_name': message.from_user.last_name,
        'username': message.from_user.username
    }
    save_user_data(message.from_user.id, user_info)

    stats = limits.get_stats()

    welcome_text = f"""
👋 **Welcome to the your number one Youtube video Downloader Bot!**

⚠️ **Daily Limits:**
• Maximum {limits.max_videos_per_user} videos per user a day

📱 **How to use:**
• send a video URL from Youtube.
• Choose video quality from the options
• Wait for download and upload

🔄 **Limits reset daily at midnight UTC**

use /help for more information and useful.
    """

    await message.reply_text(welcome_text)


@app.on_message(filters.command("stats"))
async def stats_command(client: Client, message: Message):
    """Show current bot statistics (public version)"""
    stats = limits.get_stats()
    user_id = message.from_user.id

    limits.reset_daily_stats_if_needed()
    user_downloads_today = limits.bot_data['user_downloads_today'].get(
        str(user_id), 0)

    stats_text = f"""
👤 **Your Status:**
• Your downloads today: {user_downloads_today}/{limits.max_videos_per_user}
• Can you download: {"✅ Yes" if limits.can_user_download(user_id)[0] else "❌ No"}

📋 **Daily Limits:**
• Max videos per user: {limits.max_videos_per_user}  

🕐 **Resets:** Daily at midnight UTC
    """

    await message.reply_text(stats_text)

# Admin-only commands


@app.on_message(filters.command("adminstats") & filters.user(ADMIN_USER_IDS))
async def admin_stats_command(client: Client, message: Message):
    """Show detailed admin statistics"""
    stats = limits.get_stats()
    users_data = load_json_data(USERS_DATA_FILE, {})
    videos_data = load_json_data(VIDEOS_DATA_FILE, [])

    # Calculate additional stats
    total_users = len(users_data)
    total_videos = len(videos_data)
    active_users_today = len(limits.bot_data['users_today'])

    # Recent downloads (last 10)
    recent_videos = sorted(videos_data, key=lambda x: x.get(
        'download_date', ''), reverse=True)[:10]

    admin_stats_text = f"""
🔧 **ADMIN STATISTICS**

📊 **Overall Stats:**
• Total Users: {total_users}
• Bot Running Since: {stats['bot_start_date']}
• All-time Downloads: {stats['total_downloads_all_time']}

📅 **Today's Stats:**
• Active downloads: {stats['active_downloads']}/{limits.max_concurrent_downloads}
• Downloads today: {stats['daily_downloads']}/{limits.max_total_daily_downloads}
• Users today: {active_users_today}/{limits.max_users_per_day}
• Remaining: {stats['remaining_downloads']}

📈 **Recent Activity:**
    """

    if recent_videos:
        admin_stats_text += "\n🎥 **Last 5 Downloads:**\n"
        for i, video in enumerate(recent_videos[:5], 1):
            user_id = video.get('user_id', 'Unknown')
            title = video.get('video_title', 'Unknown')[:30]
            date = video.get('download_date', '')[:10]  # Just date part
            admin_stats_text += f"{i}. User {user_id}: {title}... ({date})\n"

    await message.reply_text(admin_stats_text)


@app.on_message(filters.command("adminusers") & filters.user(ADMIN_USER_IDS))
async def admin_users_command(client: Client, message: Message):
    """Show user list for admin"""
    users_data = load_json_data(USERS_DATA_FILE, {})

    if not users_data:
        await message.reply_text("👥 **No users found in database**")
        return

    users_text = "👥 **USER LIST**\n\n"

    # Sort users by last seen (most recent first)
    sorted_users = sorted(users_data.items(),
                          key=lambda x: x[1].get('last_seen', ''),
                          reverse=True)

    # Show first 20 users
    for i, (user_id, user_info) in enumerate(sorted_users[:20], 1):
        name = user_info.get('first_name', 'Unknown')
        if user_info.get('last_name'):
            name += f" {user_info.get('last_name')}"

        username = user_info.get('username', 'No username')
        downloads = user_info.get('total_downloads', 0)
        last_seen = user_info.get('last_seen', '')[:10]  # Just date part

        users_text += f"{i}. **{name}** (@{username})\n"
        users_text += f"   ID: `{user_id}` | Downloads: {downloads} | Last: {last_seen}\n\n"

        # Telegram message length limit
        if len(users_text) > 3500:
            users_text += f"... and {len(sorted_users) - i} more users"
            break

    await message.reply_text(users_text)


@app.on_message(filters.command("adminvideos") & filters.user(ADMIN_USER_IDS))
async def admin_videos_command(client: Client, message: Message):
    """Show recent video downloads for admin"""
    videos_data = load_json_data(VIDEOS_DATA_FILE, [])

    if not videos_data:
        await message.reply_text("🎥 **No videos found in database**")
        return

    # Sort by download date (most recent first)
    recent_videos = sorted(videos_data, key=lambda x: x.get(
        'download_date', ''), reverse=True)

    videos_text = "🎥 **RECENT DOWNLOADS**\n\n"

    for i, video in enumerate(recent_videos[:15], 1):  # Show last 15 downloads
        title = video.get('video_title', 'Unknown')[:40]
        user_id = video.get('user_id', 'Unknown')
        date = video.get('download_date', '')[:16]  # Date and time
        format_info = video.get('format', 'Unknown')
        file_size = video.get('file_size', 0)
        size_mb = f"{file_size/1024/1024:.1f}MB" if file_size > 0 else "Unknown"

        videos_text += f"{i}. **{title}**\n"
        videos_text += f"   User: {user_id} | {date}\n"
        videos_text += f"   Format: {format_info} | Size: {size_mb}\n\n"

        # Telegram message length limit
        if len(videos_text) > 3500:
            videos_text += f"... and {len(recent_videos) - i} more videos"
            break

    await message.reply_text(videos_text)


@app.on_message(filters.command("adminreset") & filters.user(ADMIN_USER_IDS))
async def admin_reset_command(client: Client, message: Message):
    """Reset daily stats manually (admin only)"""
    # Force reset daily stats
    current_date = str(datetime.now().date())
    limits.bot_data['last_reset_date'] = current_date
    limits.bot_data['total_downloads_today'] = 0
    limits.bot_data['users_today'] = []
    limits.bot_data['user_downloads_today'] = {}
    limits.active_downloads.clear()

    # Save the reset data
    save_json_data(BOT_DATA_FILE, limits.bot_data)

    await message.reply_text("🔄 **Daily stats have been reset manually!**\n\n"
                             "✅ All daily limits are now available again.")


# Additional admin commands


@app.on_message(filters.command("adminhelp") & filters.user(ADMIN_USER_IDS))
async def admin_help_command(client: Client, message: Message):
    """Show admin commands help"""
    help_text = """
🔧 **ADMIN COMMANDS**

📊 **Statistics:**
• /adminstats - Detailed bot statistics
• /adminusers - List all users
• /adminvideos - Recent video downloads

🛠️ **Management:**
• /adminreset - Reset daily limits manually
• /adminbackup - Create data backup
• /admincleanup - Clean old temporary files

ℹ️ **Info:**
• /adminhelp - Show this help message

⚠️ **Note:** These commands can only be used by admins
    """
    await message.reply_text(help_text)


@app.on_message(filters.command("adminbackup") & filters.user(ADMIN_USER_IDS))
async def admin_backup_command(client: Client, message: Message):
    """Create backup of all data files"""
    try:
        backup_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = f"backup_{backup_time}"
        os.makedirs(backup_dir, exist_ok=True)

        # Copy all data files to backup directory

        files_backed_up = []
        for filename in [USERS_DATA_FILE, VIDEOS_DATA_FILE, BOT_DATA_FILE]:
            if os.path.exists(filename):
                backup_path = os.path.join(backup_dir, filename)
                shutil.copy2(filename, backup_path)
                files_backed_up.append(filename)

        # Create backup info file
        backup_info = {
            'backup_time': datetime.now().isoformat(),
            'files_backed_up': files_backed_up,
            'bot_stats': limits.get_stats()
        }

        with open(os.path.join(backup_dir, 'backup_info.json'), 'w') as f:
            json.dump(backup_info, f, indent=2)

        await message.reply_text(
            f"✅ **Backup created successfully!**\n\n"
            f"📁 Backup directory: `{backup_dir}`\n"
            f"📄 Files backed up: {len(files_backed_up)}\n"
            f"🕐 Backup time: {backup_time}"
        )

    except Exception as e:
        await message.reply_text(f"❌ Backup failed: {str(e)}")


@app.on_message(filters.command("admincleanup") & filters.user(ADMIN_USER_IDS))
async def admin_cleanup_command(client: Client, message: Message):
    """Clean up old temporary files and directories"""
    try:
        cleanup_count = 0

        # Clean up downloads directory
        if os.path.exists("downloads"):
            for user_dir in os.listdir("downloads"):
                user_path = os.path.join("downloads", user_dir)
                if os.path.isdir(user_path):
                    # Remove any leftover files
                    for file in os.listdir(user_path):
                        file_path = os.path.join(user_path, file)
                        try:
                            os.remove(file_path)
                            cleanup_count += 1
                        except:
                            pass

        # Clean up old backup directories (keep only last 5)
        backup_dirs = [d for d in os.listdir('.') if d.startswith('backup_')]
        if len(backup_dirs) > 5:
            backup_dirs.sort()
            for old_backup in backup_dirs[:-5]:
                try:
                    shutil.rmtree(old_backup)
                    cleanup_count += 1
                except:
                    pass

        await message.reply_text(
            f"✅ **Cleanup completed!**\n\n"
            f"🗑️ Files cleaned: {cleanup_count}\n"
            f"📁 Temporary files removed\n"
            f"🔄 Old backups cleaned"
        )

    except Exception as e:
        await message.reply_text(f"❌ Cleanup failed: {str(e)}")

# Help command for regular users


@app.on_message(filters.command("help"))
async def help_command(client: Client, message: Message):
    """Show help for regular users"""
    help_text = """
🎬 **Video Downloader Bot Help**

📱 **How to use:**
1. Send me a video URL from Youtube.
2. Choose video quality from the options
3. Wait for download and upload


📊 **Commands:**
• /start - Start the bot
• /stats - View your usage statistics
• /help - Show this help message

⚠️ **Limits (Free Plan):**
• Max 8 minutes video duration
• Max 50MB file size

🔄 **Limits reset daily at midnight UTC**

💡 **Tips:**
• Choose 360p for faster downloads
• Shorter videos work better
• Be patient
    """

    await message.reply_text(help_text)


progress_data = {}


@app.on_message(filters.text & ~filters.command([]))
async def handle_url(client: Client, message: Message):
    """Handle URL messages with strict limits"""
    user_id = message.from_user.id
    url = message.text.strip()

    # Save user interaction
    user_info = {
        'first_name': message.from_user.first_name,
        'last_name': message.from_user.last_name,
        'username': message.from_user.username
    }
    save_user_data(user_id, user_info)

    # Check if user can download
    can_download, limit_message = limits.can_user_download(user_id)
    if not can_download:
        await message.reply_text(limit_message)
        return

    # # Validate URL (basic check)
    # if not any(domain in url.lower() for domain in ['youtube.com', 'youtu.be', 'instagram.com', 'tiktok.com', 'facebook.com', 'twitter.com', 'x.com']):
    #     await message.reply_text("❌ Please send a valid video URL from supported platforms (YouTube, Instagram, TikTok, Facebook, Twitter)")
    #     return

    # Store URL temporarily
    user_data[user_id] = {
        'video_url': url,
        'timestamp': datetime.now()
    }

    try:
        # Get video info (lightweight check)
        await message.reply_text("🔍 Checking video... Please wait.")

        # Lightweight video info extraction
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'skip_download': True
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            title = info_dict.get('title', 'Unknown')
            duration = info_dict.get('duration', 0)

            # Check video size constraints for Render free plan
            if duration and duration > 380:  # 6 minutes max for free plan
                await message.reply_text("❌ Video too long. Maximum 6 minutes allowed.")
                return

        # Store video info for later use
        user_data[user_id]['video_info'] = {
            'title': title,
            'duration': duration,
            'url': url
        }

        # Create format options (limited for free plan)
        video_options = [
            ('480p', '🎥 480p quality'),
            ('360p', '🎥 360p quality'),
            ('worst', '🎥 Lowest quality (Fastest)')
        ]

        keyboard = []
        for code, desc in video_options:
            button = InlineKeyboardButton(
                desc, callback_data=f"download_{code}")
            keyboard.append([button])

        reply_markup = InlineKeyboardMarkup(keyboard)

        duration_str = f"{int(duration) // 60}:{int(duration) % 60:02d}" if duration else "Unknown"
        stats = limits.get_stats()

        await message.reply_text(
            f"🎵 **Video Found:**\n"
            f"📺 {title[:50]}...\n"
            f"⏳ Duration: {duration_str}\n\n"
            # f"📊 **Remaining today:** {stats['remaining_downloads']} downloads\n"
            f"👤 **Your remaining:** {limits.max_videos_per_user - limits.bot_data['user_downloads_today'].get(str(user_id), 0)} videos\n\n"
            f"⚠️ Choose 360p for best performance",
            reply_markup=reply_markup
        )

    except Exception as e:
        logger.error(f"Error fetching video info: {e}")
        await message.reply_text("❌ Unable to process this video. Please try a different URL.")


def progress_hook(d, user_id):
    """Synchronous progress hook for yt-dlp"""
    try:
        if d['status'] == 'downloading':
            # Store progress data globally
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            speed = d.get('speed', 0)
            eta = d.get('eta', 0)

            progress_data[user_id] = {
                'status': 'downloading',
                'downloaded': downloaded,
                'total': total,
                'speed': speed,
                'eta': eta,
                'last_update': time.time()
            }

        elif d['status'] == 'finished':
            file_size = d.get('total_bytes', 0)
            progress_data[user_id] = {
                'status': 'finished',
                'file_size': file_size,
                'last_update': time.time()
            }

    except Exception as e:
        logging.error(f"Progress hook error: {e}")


def create_progress_bar(percentage):
    """Create a visual progress bar"""
    filled = int(percentage / 10)
    empty = 10 - filled
    bar = "🟩" * filled + "⬜" * empty
    return f"[{bar}] {percentage:.1f}%"


def create_animated_progress_bar(current_time):
    """Create animated progress bar for unknown total size"""
    # Create a moving green block animation
    position = int(current_time * 2) % 10
    bar = ["⬜"] * 10
    bar[position] = "🟩"
    return "".join(bar)


def format_bytes(bytes_value):
    """Convert bytes to human readable format"""
    if bytes_value == 0:
        return "0B"

    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_value < 1024.0:
            return f"{bytes_value:.1f}{unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.1f}TB"


def format_speed(speed):
    """Format download speed"""
    if speed is None or speed == 0:
        return "0 B/s"
    return f"{format_bytes(speed)}/s"


def format_eta(eta):
    """Format estimated time remaining"""
    if eta is None or eta == 0:
        return "Unknown"

    if eta < 60:
        return f"{int(eta)}s"
    elif eta < 3600:
        return f"{int(eta//60)}m {int(eta%60)}s"
    else:
        hours = int(eta // 3600)
        minutes = int((eta % 3600) // 60)
        return f"{hours}h {minutes}m"


async def update_progress(callback_query, user_id, start_time):
    """Async function to update progress messages"""
    last_message_update = 0

    while user_id in progress_data:
        try:
            current_time = time.time()

            # Update every 3 seconds to avoid rate limiting
            if current_time - last_message_update < 3:
                await asyncio.sleep(1)
                continue

            data = progress_data.get(user_id)
            if not data:
                await asyncio.sleep(1)
                continue

            if data['status'] == 'downloading':
                downloaded = data['downloaded']
                total = data['total']
                speed = data['speed']
                eta = data['eta']

                elapsed = current_time - start_time
                elapsed_str = f"{int(elapsed//60)}m {int(elapsed%60)}s"

                if total > 0:
                    percentage = (downloaded / total) * 100
                    progress_bar = create_progress_bar(percentage)

                    progress_text = (
                        f"📥 **Downloading Video...**\n\n"
                        f"{progress_bar}\n\n"
                        f"📊 **Progress:** {percentage:.1f}%\n"
                        # f"📦 **Downloaded:** {format_bytes(downloaded)}\n"
                        # f"📏 **Total Size:** {format_bytes(total)}\n"
                        # f"🚀 **Speed:** {format_speed(speed)}\n"
                        # f"⏰ **ETA:** {format_eta(eta)}\n"
                        # f"⏱️ **Elapsed:** {elapsed_str}\n\n"
                        f"💡 *Please wait while we download your video...*"
                    )
                else:
                    # When total size is unknown
                    animated_bar = create_animated_progress_bar(current_time)

                    progress_text = (
                        f"📥 **Downloading Video**\n\n"
                        f"{animated_bar}\n\n"
                        # f"🔄 {'█' * (int(current_time) % 10 + 1)}\n\n"
                        # f"📦 **Downloaded:** {format_bytes(downloaded)}\n"
                        # f"🚀 **Speed:** {format_speed(speed)}\n"
                        # f"⏱️ **Elapsed:** {elapsed_str}\n\n"
                        # f"💡 *Calculating total size...*"
                    )

                try:
                    await callback_query.edit_message_text(progress_text)
                    last_message_update = current_time
                except Exception as e:
                    # Handle rate limiting or other Telegram errors
                    if "MESSAGE_NOT_MODIFIED" not in str(e):
                        logging.error(f"Message update error: {e}")

            elif data['status'] == 'finished':
                file_size = data['file_size']
                elapsed = current_time - start_time

                progress_text = (
                    f"✅ **Download Complete!**\n\n"
                    f"[██████████] 100%\n\n"
                    f"📁 **File Size:** {format_bytes(file_size)}\n"
                    f"⏱️ **Total Time:** {int(elapsed//60)}m {int(elapsed%60)}s\n\n"
                    f"⬆️ **Now uploading to Telegram...**"
                )

                try:
                    await callback_query.edit_message_text(progress_text)
                except:
                    pass
                break

            await asyncio.sleep(1)

        except Exception as e:
            logging.error(f"Progress update error: {e}")
            await asyncio.sleep(2)

# Modified download_video function


@app.on_callback_query(filters.regex("^download_"))
async def download_video(client: Client, callback_query: CallbackQuery):
    """Handle video download with real-time progress updates"""
    await callback_query.answer()

    user_id = callback_query.from_user.id
    format_code = callback_query.data.replace("download_", "")

    # Double-check limits before starting download
    can_download, limit_message = limits.can_user_download(user_id)
    if not can_download:
        await callback_query.edit_message_text(limit_message)
        return

    # Get stored URL and video info
    user_session = user_data.get(user_id, {})
    url = user_session.get('video_url')
    video_info = user_session.get('video_info', {})

    if not url:
        await callback_query.edit_message_text("❌ No video URL found. Please send a URL first.")
        return

    # Mark download as started
    limits.start_download(user_id)

    # Initialize progress data
    progress_data[user_id] = {'status': 'preparing'}
    start_time = time.time()

    try:
        # Show initial message
        await callback_query.edit_message_text(
            f"🔄 **Preparing Download...**\n\n"
            f"📺 **Video:** {video_info.get('title', 'Unknown')[:50]}...\n"
            f"📏 **Quality:** {format_code}\n\n"
            f"⏳ *Setting up download...*"
        )

        # Format mapping optimized for free plan
        format_mapping = {
            '360p': 'worst[height<=360]/worst',
            '480p': 'worst[height<=480]/worst',
            'worst': 'worst'
        }

        format_id = format_mapping.get(format_code, 'worst')

        # Create user-specific directory
        downloads_dir = os.path.join("downloads", str(user_id))
        os.makedirs(downloads_dir, exist_ok=True)

        # Get video info
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            title = info_dict.get('title', 'video')
            duration = info_dict.get('duration', 0)

        # Create safe filename
        safe_title = "".join(c for c in title if c.isalnum()
                             or c in (' ', '-', '_'))[:30]
        timestamp = int(time.time())
        filename = f"{safe_title}_{timestamp}.%(ext)s"
        filepath_template = os.path.join(downloads_dir, filename)

        # Download options with progress hook
        ydl_opts = {
            'format': format_id,
            'outtmpl': filepath_template,
            'noplaylist': True,
            'extractaudio': False,
            'audioformat': 'mp3',
            'quiet': True,
            'no_warnings': True,
            'prefer_insecure': True,
            'concurrent_fragment_downloads': 1,
            # Pass user_id
            'progress_hooks': [lambda d: progress_hook(d, user_id)],
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-us,en;q=0.5',
                'Accept-Encoding': 'gzip,deflate',
                'Accept-Charset': 'ISO-8859-1,utf-8;q=0.7,*;q=0.7',
                'Keep-Alive': '115',
                'Connection': 'keep-alive'},
            'socket_timeout': 90,
            'retries': 1,
            'fragment_retries': 1,
            'buffersize': 1024,
            'http_chunk_size': 1048576,
            'no_check_certificate': True,
            'prefer_ffmpeg': False
        }

        # Start progress updater task
        progress_task = asyncio.create_task(
            update_progress(callback_query, user_id, start_time))

        # Start download in a thread to avoid blocking
        def download_in_thread():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                return True
            except Exception as e:
                logging.error(f"Download thread error: {e}")
                return False

        # Run download in thread
        loop = asyncio.get_event_loop()
        download_success = await loop.run_in_executor(None, download_in_thread)

        # Wait a bit for final progress update
        await asyncio.sleep(2)

        # Cancel progress task
        progress_task.cancel()

        # Clean up progress data
        if user_id in progress_data:
            del progress_data[user_id]

        if not download_success:
            await callback_query.edit_message_text("❌ Download failed. Please try again.")
            limits.complete_download(user_id, success=False)
            return

        # Find downloaded file
        downloaded_files = glob.glob(os.path.join(
            downloads_dir, f"{safe_title}_{timestamp}.*"))

        if not downloaded_files:
            await callback_query.edit_message_text("❌ Download completed but file not found.")
            limits.complete_download(user_id, success=False)
            return

        filepath = downloaded_files[0]

        if os.path.exists(filepath):
            file_size = os.path.getsize(filepath)
            file_size_mb = file_size / (1024 * 1024)

            # Check file size limit for free plan
            if file_size_mb > 50:
                await callback_query.edit_message_text(
                    f"❌ **File Too Large**\n\n"
                    f"📏 **File Size:** {file_size_mb:.1f}MB\n"
                    f"🚫 **Limit:** 50MB (Free Plan)\n\n"
                    f"💡 *Try selecting 'Lowest Quality' format*"
                )
                os.remove(filepath)
                limits.complete_download(user_id, success=False)
                return

            # Show upload progress
            upload_text = (
                f"⬆️ **Uploading to Telegram...**\n\n"
                f"📁 **File:** {title[:40]}...\n"
                f"📏 **Size:** {file_size_mb:.1f}MB\n"
                f"📂 **Format:** {format_code}\n\n"
                f"⏳ *Please wait while we upload your video...*"
            )
            await callback_query.edit_message_text(upload_text)

            # Upload video
            try:
                duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "Unknown"

                await client.send_video(
                    chat_id=callback_query.from_user.id,
                    video=filepath,
                    caption=f"🎥 **{title[:100]}**\n\n"
                            f"📏 **Size:** {file_size_mb:.1f}MB\n"
                            f"⏳ **Duration:** {duration_str}\n"
                            f"📂 **Quality:** {format_code}\n\n"
                            f"✅ **Downloaded successfully!**"
                )

                # Success - save video data
                video_data = {
                    'url': url,
                    'title': title,
                    'duration': duration,
                    'format': format_code,
                    'file_size': file_size,
                    'success': True
                }
                save_video_data(user_id, video_data)
                save_user_data(user_id, {
                    'first_name': callback_query.from_user.first_name,
                    'last_name': callback_query.from_user.last_name,
                    'username': callback_query.from_user.username
                }, video_url=url)

                # Complete download tracking
                limits.complete_download(user_id, success=True)
                stats = limits.get_stats()
                user_remaining = limits.max_videos_per_user - \
                    limits.bot_data['user_downloads_today'].get(
                        str(user_id), 0)

                # Final success message
                success_text = (
                    f"🎉 **Upload Complete!**\n\n"
                    f"✅ **Video sent successfully**\n\n"
                    f"📊 **Your Remaining Downloads Today:** {user_remaining}\n"
                    f"🔄 **Limits reset daily at midnight UTC**\n\n"
                    f"💡 *Send another URL to download more videos!*"
                )
                await callback_query.edit_message_text(success_text)

            except Exception as upload_error:
                await callback_query.edit_message_text(
                    f"❌ **Upload Failed**\n\n"
                    f"🚫 **Error:** {str(upload_error)}\n\n"
                    f"💡 *Try again with a smaller file or different quality*"
                )
                limits.complete_download(user_id, success=False)

                # Save failed video data
                video_data = {
                    'url': url,
                    'title': title,
                    'duration': duration,
                    'format': format_code,
                    'file_size': file_size,
                    'success': False
                }
                save_video_data(user_id, video_data)

            # Clean up file
            try:
                os.remove(filepath)
            except:
                pass
        else:
            await callback_query.edit_message_text("❌ File not found after download.")
            limits.complete_download(user_id, success=False)

    except Exception as e:
        logger.error(f"Download error: {e}")
        await callback_query.edit_message_text(
            f"❌ **An Error Occurred**\n\n"
            f"🚫 **Error:** {str(e)}\n\n"
            f"💡 *Please try again with a different URL or quality*"
        )
        limits.complete_download(user_id, success=False)

    finally:
        # Clean up user data and progress data
        if user_id in user_data:
            del user_data[user_id]
        if user_id in progress_data:
            del progress_data[user_id]


def main():
    print("🚀 Starting Video Downloader Bot")
    # Create necessary directories
    os.makedirs("downloads", exist_ok=True)

    # Initialize data files if they don't exist
    if not os.path.exists(USERS_DATA_FILE):
        save_json_data(USERS_DATA_FILE, {})
    if not os.path.exists(VIDEOS_DATA_FILE):
        save_json_data(VIDEOS_DATA_FILE, [])

    print("✅ Data files initialized")
    print("✅ Bot starting...")

    try:
        Thread(target=run_flask).start()
        app.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped gracefully")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")


if __name__ == "__main__":
    main()
