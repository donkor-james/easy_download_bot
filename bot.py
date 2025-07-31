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
from dotenv import load_dotenv
import os
import json
import time

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
        self.max_concurrent_downloads = 3
        self.max_videos_per_user = 8
        self.max_total_daily_downloads = 1080
        self.cooldown_seconds = 30  # Cooldown after each download
        self.max_hourly_downloads = 200  # Spike protection

        self.active_downloads = set()  # Track active download user_ids
        self.user_last_download_time = {}  # user_id: timestamp
        self.hourly_downloads = []  # List of (timestamp) for last hour

        # Load or initialize bot data
        self.bot_data = load_json_data(BOT_DATA_FILE, {
            'last_reset_date': str(datetime.now().date()),
            'total_downloads_today': 0,
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
            # users_today removed
            self.bot_data['user_downloads_today'] = {}

            # Clear active downloads (in case of bot restart)
            self.active_downloads.clear()

            # Save the reset data
            save_json_data(BOT_DATA_FILE, self.bot_data)

            logger.info("Daily stats have been reset successfully")

    def can_user_download(self, user_id):
        """Check if user can make a download request, with cooldown and spike protection"""
        import time
        self.reset_daily_stats_if_needed()

        # Admins are exempt from daily user and download limits
        if is_admin(user_id):
            # Only check if already downloading, cooldown, concurrent, and spike protection
            if user_id in self.active_downloads:
                return False, "❌ You already have a download in progress. Please wait for it to finish."
            now = time.time()
            last_time = self.user_last_download_time.get(user_id, 0)
            if now - last_time < self.cooldown_seconds:
                wait_sec = int(self.cooldown_seconds - (now - last_time))
                return False, f"⏳ Please wait {wait_sec} seconds before starting another download."
            if len(self.active_downloads) >= self.max_concurrent_downloads:
                return False, f"⏳ Server busy: Many downloads happening all at once.\nPlease try again in a few minutes."
            one_hour_ago = now - 3600
            self.hourly_downloads = [
                t for t in self.hourly_downloads if t > one_hour_ago]
            if len(self.hourly_downloads) >= self.max_hourly_downloads:
                return False, "🚦 Too many requests at the moment. Please try again later."
            return True, "✅ Ready to download! (Admin: limits bypassed)"

        # Non-admins: all checks
        # Check if user is already downloading
        if user_id in self.active_downloads:
            return False, "❌ You already have a download in progress. Please wait for it to finish."

        # Check per-user cooldown
        now = time.time()
        last_time = self.user_last_download_time.get(user_id, 0)
        if now - last_time < self.cooldown_seconds:
            wait_sec = int(self.cooldown_seconds - (now - last_time))
            return False, f"⏳ Please wait {wait_sec} seconds before starting another download."

        # Check concurrent downloads limit
        if len(self.active_downloads) >= self.max_concurrent_downloads:
            return False, f"⏳ Server busy: Many downloads happening all at once.\nPlease try again in a few minutes."

        # Check hourly spike protection
        one_hour_ago = now - 3600
        self.hourly_downloads = [
            t for t in self.hourly_downloads if t > one_hour_ago]
        if len(self.hourly_downloads) >= self.max_hourly_downloads:
            return False, "🚦 Too many requests at the moment. Please try again later."

        # Check daily total downloads limit
        if self.bot_data['total_downloads_today'] >= self.max_total_daily_downloads:
            return False, "📊 The service is taking a break for today. Please come back tomorrow."

        # Check user's daily video limit
        user_downloads_today = self.bot_data['user_downloads_today'].get(
            str(user_id), 0)
        if user_downloads_today >= self.max_videos_per_user:
            return False, "🎥 You've reached your daily download limit. Please try again tomorrow."

        return True, "✅ Ready to download!"

    def start_download(self, user_id):
        """Mark user as having started a download"""
        import time
        self.active_downloads.add(user_id)
        self.user_last_download_time[user_id] = time.time()

    def complete_download(self, user_id, success=True):
        """Mark download as completed"""
        import time
        self.active_downloads.discard(user_id)

        if success:
            self.reset_daily_stats_if_needed()

            # Update daily stats
            self.bot_data['total_downloads_today'] += 1
            self.bot_data['total_downloads_all_time'] += 1

            # Update user's daily download count
            user_key = str(user_id)
            self.bot_data['user_downloads_today'][user_key] = self.bot_data['user_downloads_today'].get(
                user_key, 0) + 1

            # Add to hourly downloads
            self.hourly_downloads.append(time.time())

            # Save updated data
            save_json_data(BOT_DATA_FILE, self.bot_data)

    def get_stats(self):
        """Get current bot statistics"""
        self.reset_daily_stats_if_needed()
        return {
            'active_downloads': len(self.active_downloads),
            'daily_downloads': self.bot_data['total_downloads_today'],
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
👋 **Welcome to your number one Video Downloader Bot!**

⚠️ **Daily Limits:**
• Maximum {limits.max_videos_per_user} videos per user a day

📱 **How to use:**
• Send a video URL from any supported website or platform.
• Choose video quality from the options
• Wait for download and upload

🔄 **Limits reset daily at midnight UTC**

Use /help for more information and useful tips.
    """

    # Add menu button to start message
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Show Menu", callback_data="show_menu")]
    ])
    await message.reply_text(welcome_text, reply_markup=keyboard)
# Help command for regular users


# --- MENU COMMAND AND CALLBACK HANDLER ---
@app.on_message(filters.command("menu"))
async def show_command_menu(client, message):
    menu_text = """🤖 **Available Commands**

🚀 get started with the bot `/start`
📊 view your statistics `/stats`
❓ get help and support `/help`
📋 show the command menu `/menu`

Choose a command or type it directly."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 get started with the bot",
                              callback_data="start")],
        [InlineKeyboardButton("📊 view your statistics",
                              callback_data="stats")],
        [InlineKeyboardButton("❓ get help and support", callback_data="help")],
        [InlineKeyboardButton("❌ Close Menu", callback_data="close")]
    ])
    await message.reply_text(menu_text, reply_markup=keyboard)


# Callback handler for 'show_menu' button
@app.on_callback_query(filters.regex("^show_menu$"))
async def show_menu_callback(client, callback_query):
    menu_text = """🤖 **Available Commands**

🚀 get started with the bot `/start`
📊 view your statistics `/stats`
❓ get help and support `/help`
📋 show the command menu `/menu`

Choose a command or type it directly."""
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 get started with the bot",
                              callback_data="start")],
        [InlineKeyboardButton("📊 view your statistics",
                              callback_data="stats")],
        [InlineKeyboardButton("❓ get help and support", callback_data="help")],
        [InlineKeyboardButton("❌ Close Menu", callback_data="close")]
    ])
    await callback_query.edit_message_text(menu_text, reply_markup=keyboard)

# Remove the generic callback handler to ensure download_ callbacks are always processed

# Callback handlers for menu buttons


@app.on_callback_query(filters.regex("^start$"))
async def menu_start_callback(client, callback_query):
    user_info = {
        'first_name': callback_query.from_user.first_name,
        'last_name': callback_query.from_user.last_name,
        'username': callback_query.from_user.username
    }
    save_user_data(callback_query.from_user.id, user_info)
    stats = limits.get_stats()
    welcome_text = f"""
👋 **Welcome to your number one Video Downloader Bot!**

⚠️ **Daily Limits:**
• Maximum {limits.max_videos_per_user} videos per user a day

📱 **How to use:**
• Send a video URL from any supported website or platform.
• Choose video quality from the options
• Wait for download and upload

🔄 **Limits reset daily at midnight UTC**

Use /help for more information and useful tips.
    """
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Show Menu", callback_data="show_menu")]
    ])
    await callback_query.edit_message_text(welcome_text, reply_markup=keyboard)


@app.on_callback_query(filters.regex("^stats$"))
async def menu_stats_callback(client, callback_query):
    stats = limits.get_stats()
    user_id = callback_query.from_user.id
    limits.reset_daily_stats_if_needed()
    user_downloads_today = limits.bot_data['user_downloads_today'].get(
        str(user_id), 0)
    stats_text = f"""
👤 **Your Status:**
• Your downloads today: {user_downloads_today}/{limits.max_videos_per_user}
• Can you download: {'✅ Yes' if limits.can_user_download(user_id)[0] else '❌ No'}

📋 **Daily Limits:**
• Max videos per user: {limits.max_videos_per_user}

🕐 **Resets:** Daily at midnight UTC
    """
    await callback_query.edit_message_text(stats_text)


@app.on_callback_query(filters.regex("^help$"))
async def menu_help_callback(client, callback_query):
    help_text = """
🎬 **Video Downloader Bot Help**

📱 **How to use:**
1. Send me a video URL from any supported website or platform.
2. Choose video quality from the options
3. Wait for download and upload

📊 **Commands:**
• /start - Start the bot
• /stats - View your usage statistics
• /help - Show this help message
• /menu - Show the command menu

⚠️ **Limits:**
• Max 180MB file size per video/audio

🔄 **Limits reset daily at midnight UTC**

💡 **Tips:**
• Choose 360p or 480p for faster downloads
• Lower quality or shorter videos are more likely to be under 180MB
• Be patient

👨‍💻 **Contact Developer:**
If you have any problems or want to contact the bot owner, message [developer](https://t.me/lloyd_36)

    """
    await callback_query.edit_message_text(help_text)


@app.on_callback_query(filters.regex("^close$"))
async def menu_close_callback(client, callback_query):
    await callback_query.edit_message_text("❌ Menu closed.")


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

    # Calculate users active in the last 7 and 30 days
    from datetime import datetime, timedelta
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    users_this_week = 0
    users_this_month = 0
    for user in users_data.values():
        last_seen = user.get('last_seen')
        if last_seen:
            try:
                last_seen_dt = datetime.fromisoformat(last_seen)
                if last_seen_dt >= week_ago:
                    users_this_week += 1
                if last_seen_dt >= month_ago:
                    users_this_month += 1
            except Exception:
                pass

    # Calculate downloads for today
    today_str = now.date().isoformat()
    downloads_today = sum(1 for v in videos_data if v.get(
        'download_date', '').startswith(today_str))

    # Recent downloads (last 10)
    recent_videos = sorted(videos_data, key=lambda x: x.get(
        'download_date', ''), reverse=True)[:10]

    admin_stats_text = f"""
🔧 **ADMIN STATISTICS**

📊 **Overall Stats:**
• Total Users: {total_users}
• Users this week: {users_this_week}
• Users this month: {users_this_month}
• Bot Running Since: {stats['bot_start_date']}
• All-time Downloads: {stats['total_downloads_all_time']}

📅 **Today's Stats:**
• Active downloads: {stats['active_downloads']}/{limits.max_concurrent_downloads}
• Downloads today: {downloads_today}/{limits.max_total_daily_downloads}
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
1. Send me a video URL from any supported website or platform.
2. Choose video quality from the options
3. Wait for download and upload

📊 **Commands:**
• /start - Start the bot
• /stats - View your usage statistics
• /help - Show this help message
• /menu - Show the command menu

⚠️ **Limits:**
• Max 180MB file size per video/audio

🔄 **Limits reset daily at midnight UTC**

💡 **Tips:**
• Choose 360p or 480p for faster downloads
• Lower quality or shorter videos are more likely to be under 180MB
• Be patient

👨‍💻 **Contact Developer:**
If you have any problems or want to contact the bot owner, message [developer](https://t.me/lloyd_36)
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
            'skip_download': True,
            'cachedir': False
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            title = info_dict.get('title', 'Unknown')
            duration = info_dict.get('duration', 0)
            formats = info_dict.get('formats', [])

            # Check if any available format is under 180MB
            SIZE_LIMIT = 180 * 1024 * 1024
            has_size_info = any((f.get('filesize') or f.get(
                'filesize_approx')) for f in formats)
            if has_size_info:
                has_valid_format = False
                for f in formats:
                    size = f.get('filesize') or f.get('filesize_approx')
                    if size is not None and size <= SIZE_LIMIT:
                        has_valid_format = True
                        break
                if not has_valid_format:
                    await message.reply_text("❌ The video or audio is too large to download. Please try a shorter or lower quality video.")
                    return
            # If no formats have size info, allow user to proceed and enforce limit after download

        # Store video info for later use
        user_data[user_id]['video_info'] = {
            'title': title,
            'duration': duration,
            'url': url
        }

        # Create format options: 720p, 480p, 360p, plus Video Only and Audio Only
        video_options = [
            ('720p', '🎥 720p quality'),
            ('480p', '🎥 480p quality'),
            ('360p', '🎥 360p quality'),
        ]
        keyboard = []
        for code, desc in video_options:
            button = InlineKeyboardButton(
                desc, callback_data=f"download_{code}")
            keyboard.append([button])
        # Add Video Only and Audio Only as single buttons
        keyboard.append([InlineKeyboardButton(
            '🎬 Video Only', callback_data='download_videoonly')])
        keyboard.append([InlineKeyboardButton(
            '🎵 Audio Only', callback_data='download_audioonly')])
        reply_markup = InlineKeyboardMarkup(keyboard)

        duration_str = f"{int(duration) // 60}:{int(duration) % 60:02d}" if duration else "Unknown"
        stats = limits.get_stats()

        await message.reply_text(
            f"🎵 **Video Found:**\n"
            f"📺 {title[:50]}...\n"
            f"⏳ Duration: {duration_str}\n\n"
            f" **Your remaining:** {limits.max_videos_per_user - limits.bot_data['user_downloads_today'].get(str(user_id), 0)} videos\n\n"
            f"⚠️ Choose 360p or 480p for best performance",
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
            logging.info(
                f"[PROGRESS_HOOK] DOWNLOADING: user_id={user_id}, downloaded={downloaded}, total={total}, speed={speed}, eta={eta}")
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
            logging.info(
                f"[PROGRESS_HOOK] FINISHED: user_id={user_id}, file_size={file_size}")
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
    logging.info(
        f"[UPDATE_PROGRESS_START] update_progress started for user_id={user_id}")

    # Always run, wait for progress_data to appear
    while True:
        try:
            current_time = time.time()
            # Update every 3 seconds to avoid rate limiting
            if current_time - last_message_update < 3:
                await asyncio.sleep(1)
                continue

            data = progress_data.get(user_id)
            logging.info(f"[UPDATE_PROGRESS] user_id={user_id}, data={data}")
            if not data:
                # Wait for progress_data to be set by the progress hook
                await asyncio.sleep(1)
                continue

            # Log the status field explicitly
            logging.info(
                f"[UPDATE_PROGRESS_STATUS] user_id={user_id}, status={data.get('status')}")

            if data.get('status') == 'downloading':
                logging.info(f"[ENTER_DOWNLOADING_BLOCK] user_id={user_id}")
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
                        f"📦 **Downloaded:** {format_bytes(downloaded)}\n"
                        f"📏 **Total Size:** {format_bytes(total)}\n"
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
                        f"📦 **Downloaded:** {format_bytes(downloaded)}\n"
                        f"🚀 **Speed:** {format_speed(speed)}\n"
                        # f"⏱️ **Elapsed:** {elapsed_str}\n\n"
                        # f"💡 *Calculating total size...*"
                    )

                logging.info(
                    f"[PROGRESS_MSG_EDIT_ATTEMPT] About to edit progress message for user_id={user_id}, status=downloading")
                try:
                    await callback_query.edit_message_text(progress_text)
                    logging.info(
                        f"[PROGRESS_MSG_EDIT] Progress message edited for user_id={user_id}, status=downloading")
                    last_message_update = current_time
                except Exception as e:
                    logging.error(
                        f"[PROGRESS_MSG_EDIT_ERROR] user_id={user_id}, error={e}")

            elif data.get('status') == 'finished':
                logging.info(f"[ENTER_FINISHED_BLOCK] user_id={user_id}")
                file_size = data['file_size']
                elapsed = current_time - start_time
                progress_text = (
                    f"✅ **Download Complete!**\n\n"
                    f"[██████████] 100%\n\n"
                    f"📁 **File Size:** {format_bytes(file_size)}\n"
                    f"⏱️ **Total Time:** {int(elapsed//60)}m {int(elapsed%60)}s\n\n"
                    f"⬆️ **Now uploading to Telegram...**"
                )
                logging.info(
                    f"[PROGRESS_MSG_EDIT_ATTEMPT] About to edit progress message for user_id={user_id}, status=finished")
                try:
                    await callback_query.edit_message_text(progress_text)
                    logging.info(
                        f"[PROGRESS_MSG_EDIT] Progress message edited for user_id={user_id}, status=finished")
                except Exception as e:
                    logging.error(
                        f"[PROGRESS_MSG_EDIT_ERROR] user_id={user_id}, status=finished, error={e}")
                break

            await asyncio.sleep(1)

        except Exception as e:
            logging.error(f"Progress update error: {e}")
            await asyncio.sleep(2)

# Move download_video handler above the generic callback handler to ensure it is registered first


@app.on_callback_query(filters.regex("^download_"))
async def download_video(client: Client, callback_query: CallbackQuery):
    """Handle video download with real-time progress updates"""
    user_id = callback_query.from_user.id
    format_code = callback_query.data.replace("download_", "")
    user_session = user_data.get(user_id, {})
    url = user_session.get('video_url')
    video_info = user_session.get('video_info', {})
    try:
        logger.info(
            f"[DEBUG] download_video triggered for user {user_id} with data: {callback_query.data}")
        await callback_query.answer()
        # Immediate feedback to user for debugging
        await callback_query.edit_message_text("⏳ Processing your request...")
    except Exception as e:
        logger.error(f"[DEBUG] Exception at start of download_video: {e}")
        try:
            await callback_query.edit_message_text(f"❌ Internal error at start: {str(e)}")
        except:
            pass
        return

    try:
        # Get video info (again, to get formats)
        logger.info(
            f"[DEBUG] Starting yt-dlp info extraction for user {user_id} url={url}")
        with yt_dlp.YoutubeDL({'quiet': True, 'cachedir': False}) as ydl:
            info_dict = ydl.extract_info(url, download=False)
            title = info_dict.get('title', 'video')
            duration = info_dict.get('duration', 0)
            available_formats = info_dict.get('formats', [])
        logger.info(
            f"[DEBUG] yt-dlp info extraction complete for user {user_id} title={title}")

        # Define start_time for progress tracking
        start_time = time.time()

        # Format selection logic
        SIZE_LIMIT = 180 * 1024 * 1024  # 180MB in bytes
        used_lower_best = False
        used_worst = False
        format_id = None
        display_format_code = format_code

        def get_best_size(res):
            for f in available_formats:
                if f.get('height') == res and f.get('vcodec', 'none') != 'none' and f.get('acodec', 'none') != 'none':
                    return f.get('filesize') or f.get('filesize_approx')
            for f in available_formats:
                if f.get('height') == res and f.get('vcodec', 'none') != 'none':
                    return f.get('filesize') or f.get('filesize_approx')
            return None

        def get_worst_format(res=None):
            # Find the worst video (lowest quality) optionally at a given height
            worst = None
            for f in available_formats:
                if f.get('vcodec', 'none') != 'none':
                    if res is None or f.get('height') == res:
                        if not worst or (f.get('filesize') or f.get('filesize_approx') or 0) < (worst.get('filesize') or worst.get('filesize_approx') or float('inf')):
                            worst = f
            return worst

        # Log all available video+audio formats for debugging
        video_audio_formats = [
            {
                'format_id': f.get('format_id'),
                'ext': f.get('ext'),
                'height': f.get('height'),
                'filesize': f.get('filesize') or f.get('filesize_approx'),
                'vcodec': f.get('vcodec'),
                'acodec': f.get('acodec')
            }
            for f in available_formats
            if f.get('vcodec', 'none') != 'none' and f.get('acodec', 'none') != 'none'
        ]
        logger.info(
            f"[DEBUG] Available video+audio formats for user {user_id}: {video_audio_formats}")

        # Handle special cases for videoonly/audioonly
        if format_code == 'videoonly':
            # Fallback order: best 480p (strip audio), worst 720p, best 360p, all under SIZE_LIMIT
            selected_format = None
            selected_height = None
            selected_type = None
            # 1. Try best 480p video-only under size limit
            best_480 = None
            for f in available_formats:
                if f.get('vcodec', 'none') != 'none' and f.get('acodec', 'none') == 'none' and f.get('height') == 480:
                    size = f.get('filesize') or f.get('filesize_approx')
                    if size is not None and size <= SIZE_LIMIT:
                        if not best_480 or (size > (best_480.get('filesize') or 0)):
                            best_480 = f
            if best_480:
                selected_format = best_480
                selected_height = 480
                selected_type = 'best_480'
            # 2. If not, try worst 720p video-only under size limit
            if not selected_format:
                worst_720 = None
                for f in available_formats:
                    if f.get('vcodec', 'none') != 'none' and f.get('acodec', 'none') == 'none' and f.get('height') == 720:
                        size = f.get('filesize') or f.get('filesize_approx')
                        if size is not None and size <= SIZE_LIMIT:
                            if not worst_720 or (size < (worst_720.get('filesize') or float('inf'))):
                                worst_720 = f
                if worst_720:
                    selected_format = worst_720
                    selected_height = 720
                    selected_type = 'worst_720'
            # 3. If not, try best 360p video-only under size limit
            if not selected_format:
                best_360 = None
                for f in available_formats:
                    if f.get('vcodec', 'none') != 'none' and f.get('acodec', 'none') == 'none' and f.get('height') == 360:
                        size = f.get('filesize') or f.get('filesize_approx')
                        if size is not None and size <= SIZE_LIMIT:
                            if not best_360 or (size > (best_360.get('filesize') or 0)):
                                best_360 = f
                if best_360:
                    selected_format = best_360
                    selected_height = 360
                    selected_type = 'best_360'
            ydl_postprocessors = []
            if selected_format:
                format_id = selected_format.get('format_id')
                display_format_code = f"Video Only {selected_height}p"
            else:
                # None found under size limit, notify user
                await callback_query.edit_message_text("❌ No video-only stream in 480p, 720p, or 360p is available under 180MB. Please try  shorter video.")
                return
        elif format_code == 'audioonly':
            # Log all available audio-only formats for debugging
            audio_only_formats = [
                {
                    'format_id': f.get('format_id'),
                    'ext': f.get('ext'),
                    'filesize': f.get('filesize') or f.get('filesize_approx'),
                    'acodec': f.get('acodec'),
                    'vcodec': f.get('vcodec')
                }
                for f in available_formats
                if f.get('acodec', 'none') != 'none' and f.get('vcodec', 'none') == 'none'
            ]
            logger.info(
                f"[DEBUG] Available audio-only formats for user {user_id}: {audio_only_formats}")

            # Prefer m4a/mp3, fallback to any audio-only under size limit
            preferred_audio = None
            fallback_audio = None
            for f in available_formats:
                if f.get('acodec', 'none') != 'none' and f.get('vcodec', 'none') == 'none':
                    size = f.get('filesize') or f.get('filesize_approx')
                    if size is not None and size <= SIZE_LIMIT:
                        ext = f.get('ext', '')
                        if ext in ('m4a', 'mp3'):
                            if not preferred_audio or (size > (preferred_audio.get('filesize') or 0)):
                                preferred_audio = f
                        else:
                            if not fallback_audio or (size > (fallback_audio.get('filesize') or 0)):
                                fallback_audio = f
            selected_audio = preferred_audio or fallback_audio
            if not selected_audio:
                # Fallback: try 'best' and extract audio from it
                logger.info(
                    f"[DEBUG] No audio-only stream found, falling back to 'best' for audioonly for user {user_id}.")
                format_id = 'best'
                display_format_code = "Audio Only "
            else:
                # Robust format_id selection for audioonly
                ext = selected_audio.get('ext', '')
                format_id = None
                if ext in ('m4a', 'mp3'):
                    # Check if a format with this ext exists in available_formats
                    found = False
                    for f in available_formats:
                        if f.get('acodec', 'none') != 'none' and f.get('vcodec', 'none') == 'none' and f.get('ext') == ext:
                            format_id = f.get('format_id')
                            found = True
                            break
                    if not found:
                        # Fallback to bestaudio[vcodec=none] or actual format_id
                        format_id = selected_audio.get('format_id')
                else:
                    format_id = selected_audio.get('format_id')
                logger.info(
                    f"[DEBUG] Selected audio format for user {user_id}: ext={ext}, format_id={format_id}")
                display_format_code = "Audio Only"
            # Always convert to mp3 for Telegram
            ydl_postprocessors = [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }
            ]
        elif format_code != 'videoonly':
            ydl_postprocessors = []

        # Helper for video+audio existence, always define before use
        def has_video_audio(res, ext_prefer=None):
            for v in available_formats:
                if v.get('height') == res and v.get('vcodec', 'none') != 'none' and (ext_prefer is None or v.get('ext') == ext_prefer):
                    for a in available_formats:
                        if a.get('acodec', 'none') != 'none' and a.get('vcodec', 'none') == 'none':
                            v_size = v.get('filesize') or v.get(
                                'filesize_approx')
                            a_size = a.get('filesize') or a.get(
                                'filesize_approx')
                            if v_size and a_size and v_size + a_size <= SIZE_LIMIT:
                                return True
            return False

        def has_bestvideo_audio(res):
            for f in available_formats:
                if f.get('height') == res and f.get('vcodec', 'none') != 'none' and f.get('acodec', 'none') != 'none':
                    return True
            return False

        def has_any_video(res):
            for f in available_formats:
                if f.get('height') == res and f.get('vcodec', 'none') != 'none':
                    return True
            return False

        fallback_to_best = False
        if format_code == '720p':
            # Ensure both video and audio streams exist for 720p
            if has_video_audio(720, 'mp4'):
                format_id = 'bestvideo[ext=mp4][height<=720]+bestaudio/best[height<=720]'
                logger.info(
                    f"[DEBUG] Selected format_id for 720p: {format_id} (mp4 video+audio)")
            elif has_video_audio(720):
                format_id = 'bestvideo[height<=720]+bestaudio/best[height<=720]'
                logger.info(
                    f"[DEBUG] Selected format_id for 720p: {format_id} (any video+audio)")
            elif has_video_audio(480, 'mp4'):
                format_id = 'bestvideo[ext=mp4][height<=480]+bestaudio/best[height<=480]'
                used_lower_best = True
                logger.info(
                    f"[DEBUG] Selected format_id for 720p: {format_id} (fallback to 480p mp4)")
            elif has_video_audio(480):
                format_id = 'bestvideo[height<=480]+bestaudio/best[height<=480]'
                used_lower_best = True
                logger.info(
                    f"[DEBUG] Selected format_id for 720p: {format_id} (fallback to 480p any)")
            else:
                # Fallback to worst 720p (not 360p)
                worst_720 = get_worst_format(720)
                if worst_720:
                    worst_720_size = worst_720.get(
                        'filesize') or worst_720.get('filesize_approx')
                    if worst_720_size is not None and worst_720_size <= SIZE_LIMIT:
                        format_id = f"{worst_720.get('format_id')}"
                        used_worst = True
                        logger.info(
                            f"[DEBUG] Selected format_id for 720p: {format_id} (worst 720p")
                    else:
                        logger.info(
                            f"[DEBUG] No suitable 720p version with audio under 180MB. Trying fallback to 'best'.")
                        fallback_to_best = True
                else:
                    logger.info(
                        f"[DEBUG] No suitable 720p version found. Trying fallback to 'best'.")
                    fallback_to_best = True

        elif format_code == '480p':
            # Ensure both video and audio streams exist for 480p
            if has_video_audio(480, 'mp4'):
                format_id = 'bestvideo[ext=mp4][height<=480]+bestaudio/best[height<=480]'
                logger.info(
                    f"[DEBUG] Selected format_id for 480p: {format_id} (mp4 video+audio)")
            elif has_video_audio(480):
                format_id = 'bestvideo[height<=480]+bestaudio/best[height<=480]'
                logger.info(
                    f"[DEBUG] Selected format_id for 480p: {format_id} (any video+audio)")
            else:
                # Fallback to worst 480p
                worst_480 = get_worst_format(480)
                if worst_480:
                    worst_480_size = worst_480.get(
                        'filesize') or worst_480.get('filesize_approx')
                    if worst_480_size is not None and worst_480_size <= SIZE_LIMIT:
                        format_id = f"{worst_480.get('format_id')}"
                        used_worst = True
                        logger.info(
                            f"[DEBUG] Selected format_id for 480p: {format_id} (worst 480p")
                    else:
                        logger.info(
                            f"[DEBUG] No suitable 480p or any other format found under 180MB. Trying fallback to 'best'.")
                        fallback_to_best = True
                else:
                    logger.info(
                        f"[DEBUG] No suitable 480p or any other format found under 180MB (no worst_480). Trying fallback to 'best'.")
                    fallback_to_best = True

            if used_lower_best:
                display_format_code = f"{format_code}"
            elif used_worst:
                display_format_code = f"{format_code}"

        elif format_code == '360p':
            # Ensure both video and audio streams exist for 360p
            if has_video_audio(360, 'mp4'):
                format_id = 'bestvideo[ext=mp4][height<=360]+bestaudio/best[height<=360]'
                logger.info(
                    f"[DEBUG] Selected format_id for 360p: {format_id} (mp4 video+audio)")
            elif has_video_audio(360):
                format_id = 'bestvideo[height<=360]+bestaudio/best[height<=360]'
                logger.info(
                    f"[DEBUG] Selected format_id for 360p: {format_id} (any video+audio)")
            else:
                # Fallback to worst 360p only
                worst_360 = get_worst_format(360)
                if worst_360:
                    worst_360_size = worst_360.get(
                        'filesize') or worst_360.get('filesize_approx')
                    if worst_360_size is not None and worst_360_size <= SIZE_LIMIT:
                        format_id = f"{worst_360.get('format_id')}"
                        used_worst = True
                        logger.info(
                            f"[DEBUG] Selected format_id for 360p: {format_id} (worst 360p")
                    else:
                        logger.info(
                            f"[DEBUG] No suitable 360p version with audio under 180MB. Trying fallback to 'best'.")
                        fallback_to_best = True
                else:
                    logger.info(
                        f"[DEBUG] No suitable 360p version found. Trying fallback to 'best'.")
                    fallback_to_best = True

            if used_worst:
                display_format_code = f"{format_code}"

        # Fallback: If no suitable format was found, try yt-dlp's 'best' format
        if fallback_to_best:
            format_id = 'best'
            display_format_code = "best available"

        await callback_query.edit_message_text(
            f"🔄 **Preparing Download...**\n\n"
            f"📺 **Video:** {video_info.get('title', 'Unknown')[:50]}...\n"
            f"📏 **Format:** {display_format_code}\n\n"
            f"⏳ *Setting up download...*"
        )

        # Create user-specific directory
        downloads_dir = os.path.join("downloads", str(user_id))
        os.makedirs(downloads_dir, exist_ok=True)

        # Create safe filename
        safe_title = "".join(c for c in title if c.isalnum()
                             or c in (' ', '-', '_'))[:30]
        timestamp = int(time.time())
        filename = f"{safe_title}_{timestamp}.%(ext)s"
        filepath_template = os.path.join(downloads_dir, filename)

        # Download options with progress hook
        ydl_opts = {
            'format': format_id,
            'cookies': 'cookies.txt',
            'outtmpl': filepath_template,
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'prefer_insecure': True,
            'concurrent_fragment_downloads': 1,
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
            'prefer_ffmpeg': False,
            'postprocessors': ydl_postprocessors,
            'cachedir': False
        }

        # Start download in a thread to avoid blocking
        def download_in_thread():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                return True
            except Exception as e:
                logging.error(f"Download thread error: {e}")
                return False

        # Start progress updater task before download
        progress_task = asyncio.create_task(
            update_progress(callback_query, user_id, start_time))
        logger.info(
            f"[PROGRESS_TASK_CREATED] Progress updater task created for user_id={user_id}")

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

        # Find downloaded file (for audioonly, look for mp3; else, any ext)
        if format_code == 'audioonly':
            downloaded_files = glob.glob(os.path.join(
                downloads_dir, f"{safe_title}_{timestamp}.mp3"))
            # fallback: if not found, try m4a or ogg
            if not downloaded_files:
                downloaded_files = glob.glob(os.path.join(
                    downloads_dir, f"{safe_title}_{timestamp}.*"))
        else:
            downloaded_files = glob.glob(os.path.join(
                downloads_dir, f"{safe_title}_{timestamp}.*"))

        if not downloaded_files:
            await callback_query.edit_message_text("❌ Download completed but file not found.")
            limits.complete_download(user_id, success=False)
            return

        filepath = downloaded_files[0]

        # If videoonly fallback, strip audio using ffmpeg after download
        if format_code == 'videoonly' and display_format_code == 'Video Only ':
            # Output file path for audio-stripped video, re-encoded for Telegram compatibility
            base, _ = os.path.splitext(filepath)
            stripped_filepath = base + '_videoonly.mp4'
            import subprocess
            ffmpeg_cmd = [
                'ffmpeg', '-y', '-i', filepath, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-an', '-movflags', '+faststart', stripped_filepath
            ]
            try:
                subprocess.run(ffmpeg_cmd, check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                # Remove original file, use stripped file for sending
                os.remove(filepath)
                filepath = stripped_filepath
            except Exception:
                await callback_query.edit_message_text(
                    "❌ Failed to process video. Please try again later.")
                limits.complete_download(user_id, success=False)
                return

        if os.path.exists(filepath):
            file_size = os.path.getsize(filepath)
            file_size_mb = file_size / (1024 * 1024)

            # Check file size limit for free plan
            if file_size_mb > 180:
                await callback_query.edit_message_text(
                    f"❌ **File Too Large**\n\n"
                    f"📏 **File Size:** {file_size_mb:.1f}MB\n"
                    f"🚫 **Limit:** 50MB \n\n"
                )
                os.remove(filepath)
                limits.complete_download(user_id, success=False)
                return

            # Show upload progress
            upload_text = (
                f"⬆️ **Uploading to Telegram...**\n\n"
                f"📁 **File:** {title[:40]}...\n"
                f"📏 **Size:** {file_size_mb:.1f}MB\n"
                f"📂 **Format:** {display_format_code}\n\n"
                f"⏳ *Uploading...*"
            )
            await callback_query.edit_message_text(upload_text)

            # Upload to Telegram
            try:
                duration_str = f"{int(duration) // 60}:{int(duration) % 60:02d}" if duration else "Unknown"
                if format_code == 'audioonly':
                    # Always send as audio file
                    await client.send_audio(
                        chat_id=callback_query.from_user.id,
                        audio=filepath,
                        caption=f"🎵 **{title[:100]}**\n\n"
                                f"📏 **Size:** {file_size_mb:.1f}MB\n"
                                f"⏳ **Duration:** {duration_str}\n"
                                f"📂 **Format:** {display_format_code}\n\n"
                                f"✅ **Downloaded!**",
                        duration=int(duration) if duration else None
                    )
                else:
                    # For video only, force Telegram to treat as video (not GIF)
                    ffprobe_cmd = [
                        'ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'json', filepath
                    ]
                    width, height = None, None
                    try:
                        ffprobe_result = subprocess.run(
                            ffprobe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                        ffprobe_json = json.loads(ffprobe_result.stdout)
                        if 'streams' in ffprobe_json and len(ffprobe_json['streams']) > 0:
                            width = ffprobe_json['streams'][0].get('width')
                            height = ffprobe_json['streams'][0].get('height')
                    except Exception:
                        pass
                    send_video_kwargs = {
                        'chat_id': callback_query.from_user.id,
                        'video': filepath,
                        'caption': f"🎥 **{title[:100]}**\n\n"
                                   f"📏 **Size:** {file_size_mb:.1f}MB\n"
                                   f"⏳ **Duration:** {duration_str}\n"
                                   f"📂 **Format:** {display_format_code}\n\n"
                                   f"✅ **Downloaded!**",
                        'duration': int(duration) if duration else None,
                        'supports_streaming': True,
                    }
                    if width and height:
                        send_video_kwargs['width'] = width
                        send_video_kwargs['height'] = height
                    await client.send_video(**send_video_kwargs)

                # Success - save video/audio data
                video_data = {
                    'url': url,
                    'title': title,
                    'duration': duration,
                    'format': display_format_code,
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
                    f"✅ **{'Audio' if format_code == 'audioonly' else 'Video'} sent successfully**\n\n"
                    f"📊 **Your Remaining Downloads Today:** {user_remaining}\n"
                )
                await callback_query.edit_message_text(success_text)

            except Exception as upload_error:
                await callback_query.edit_message_text(
                    f"❌ **Upload Failed**\n\n"
                    f"🚫 **Error:** {str(upload_error)}\n\n"
                )
                limits.complete_download(user_id, success=False)

                # Save failed video/audio data
                video_data = {
                    'url': url,
                    'title': title,
                    'duration': duration,
                    'format': display_format_code,
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
        app.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped gracefully")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")


if __name__ == "__main__":
    main()
