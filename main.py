# main.py
import os
import logging
import sqlite3
import imghdr
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import requests
import json
import asyncio

# Load environment variables
load_dotenv()

# Bot configuration
BOT_TOKEN = os.getenv('BOT_TOKEN', '8366535001:AAFyVWNNRATsI_XqIUiT_Qqa-PAjGcVAyDU')
FORCE_JOIN_CHANNEL = "@ronjumodz"
ADMIN_USER_ID = 7755338110
CONTACT_USERNAME = "@Ronju360"

# API Base URLs (added / replaced)
INDIA_PHONE_API_BASE_URL = "https://subhxmouktik-number-api.onrender.com/api?key=SHATIR&type=mobile&term="
PAKISTAN_PHONE_API_BASE_URL = "https://legendxdata.site/Api/simdata.php?phone="
AADHAAR_API_BASE_URL = "https://happy-ration-info.vercel.app/fetch?key=paidchx&aadhaar="
VEHICLE_API_BASE_URL = "https://vehicle-info.itxkaal.workers.dev/?num="
UPI_API_BASE_URL = "https://upi-info.vercel.app/api/upi?key=456&upi_id="

# Policy: combined lookup limit
DAILY_LOOKUP_LIMIT = 5  # 5 lookups per 24h
BONUS_BATCH_SIZE = 5    # number of extra lookups unlocked per 2 referrals
REFERRALS_REQUIRED_FOR_BONUS = 2

# Initialize logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class OSINTBot:
    def __init__(self):
        self.init_db()
        self.scheduler = AsyncIOScheduler()
        self.setup_scheduler()

    def init_db(self):
        """Initialize SQLite database safely without deleting existing users."""
        self.conn = sqlite3.connect('users.db', check_same_thread=False)
        self.cursor = self.conn.cursor()

        # Create users table if not exists (keep existing fields)
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                premium_status BOOLEAN DEFAULT FALSE,
                premium_expiry TIMESTAMP,
                force_joined BOOLEAN DEFAULT FALSE,
                free_trial_used BOOLEAN DEFAULT FALSE,
                credits INTEGER DEFAULT 0,
                referrer_id INTEGER DEFAULT NULL,
                referral_code TEXT UNIQUE
            )
        ''')

        # Create admin table
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin (
                id INTEGER PRIMARY KEY,
                broadcast_text TEXT
            )
        ''')

        # Create referral tracking table
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER,
                reward_claimed BOOLEAN DEFAULT FALSE,
                referral_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (referrer_id) REFERENCES users (user_id),
                FOREIGN KEY (referred_id) REFERENCES users (user_id)
            )
        ''')

        # Add safe columns for lookup limiting and referral-bonus flow if not exists
        # lookup_count - how many lookups used in current 24h window
        # last_lookup - timestamp of first lookup in the current 24h window
        # bonus_lookups - extra lookups granted via referrals (consumed before daily limit)
        # pending_referrals - how many successful referred users pending to be "spent" for bonus
        table_info = self.cursor.execute("PRAGMA table_info(users)").fetchall()
        columns = [col[1] for col in table_info]

        if 'lookup_count' not in columns:
            self.cursor.execute("ALTER TABLE users ADD COLUMN lookup_count INTEGER DEFAULT 0")
        if 'last_lookup' not in columns:
            self.cursor.execute("ALTER TABLE users ADD COLUMN last_lookup TIMESTAMP")
        if 'bonus_lookups' not in columns:
            self.cursor.execute("ALTER TABLE users ADD COLUMN bonus_lookups INTEGER DEFAULT 0")
        if 'pending_referrals' not in columns:
            self.cursor.execute("ALTER TABLE users ADD COLUMN pending_referrals INTEGER DEFAULT 0")

        self.conn.commit()
        logger.info("Database initialized/checked successfully")

    def setup_scheduler(self):
        """Setup scheduled tasks (e.g., reset premium expiry check)."""
        self.scheduler.add_job(self.check_premium_expiry, IntervalTrigger(hours=1))
        self.scheduler.start()
        logger.info("Scheduler started")

    async def check_premium_expiry(self):
        """Check and remove expired premium"""
        try:
            current_time = datetime.now()
            self.cursor.execute(
                "SELECT user_id FROM users WHERE premium_status = 1 AND premium_expiry < ?",
                (current_time,)
            )
            expired_users = self.cursor.fetchall()

            for user_id, in expired_users:
                self.cursor.execute(
                    "UPDATE users SET premium_status = 0, premium_expiry = NULL WHERE user_id = ?",
                    (user_id,)
                )
                try:
                    await self.application.bot.send_message(
                        chat_id=user_id,
                        text="❌ Your premium access has expired. Contact @Ronju360 to upgrade to premium."
                    )
                except Exception as e:
                    logger.error(f"Could not send message to user {user_id}: {e}")

            self.conn.commit()
            logger.info(f"Checked premium expiry. {len(expired_users)} users expired")
        except Exception as e:
            logger.error(f"Error in check_premium_expiry: {e}")

    def generate_referral_code(self, user_id):
        """Generate a unique referral code for user"""
        import hashlib
        code = hashlib.md5(f"{user_id}{datetime.now()}".encode()).hexdigest()[:8].upper()
        return code

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user
        user_id = user.id
        username = user.username
        first_name = user.first_name or "User"

        # Check for referral code in args
        referrer_id = None
        if context.args and len(context.args) > 0:
            try:
                referrer_code = context.args[0]
                self.cursor.execute("SELECT user_id FROM users WHERE referral_code = ?", (referrer_code,))
                ref = self.cursor.fetchone()
                if ref:
                    referrer_id = ref[0]
            except Exception as e:
                logger.error(f"Referral parse error: {e}")

        # Ensure user exists (if not, create user safely)
        self.cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        existing = self.cursor.fetchone()
        if not existing:
            # Insert new user
            referral_code = self.generate_referral_code(user_id)
            expiry_time = datetime.now() + timedelta(hours=24)  # 24h trial for aadhaar/vehicle as before
            self.cursor.execute(
                """INSERT INTO users 
                (user_id, username, first_name, premium_status, premium_expiry, free_trial_used, credits, referrer_id, referral_code)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, username, first_name, True, expiry_time, True, 0, referrer_id, referral_code)
            )
            self.conn.commit()

            # If referred, update referrer's pending_referrals
            if referrer_id:
                try:
                    self.cursor.execute(
                        "UPDATE users SET pending_referrals = COALESCE(pending_referrals,0) + 1 WHERE user_id = ?",
                        (referrer_id,)
                    )
                    # Insert referral record
                    self.cursor.execute(
                        "INSERT INTO referrals (referrer_id, referred_id, reward_claimed) VALUES (?, ?, ?)",
                        (referrer_id, user_id, True)
                    )
                    self.conn.commit()

                    # Notify referrer
                    try:
                        await self.application.bot.send_message(
                            chat_id=referrer_id,
                            text=f"🎉 {first_name} joined using your referral link! Pending referrals: {self.get_pending_referrals(referrer_id)}"
                        )
                    except:
                        pass
                except Exception as e:
                    logger.error(f"Error updating referrer pending count: {e}")

            welcome_text = self._welcome_text_new_user(first_name)
        else:
            # Existing user
            self.cursor.execute("SELECT username, first_name, premium_status, premium_expiry, pending_referrals, lookup_count, last_lookup, bonus_lookups, referral_code FROM users WHERE user_id = ?", (user_id,))
            res = self.cursor.fetchone()
            _, first_name_db, premium_status, premium_expiry, pending_referrals, lookup_count, last_lookup, bonus_lookups, referral_code = res
            welcome_text = self._welcome_text_existing_user(first_name_db, premium_status, premium_expiry, lookup_count, bonus_lookups)

        # If user has verified channel already we show menu; otherwise show join request with verify button
        self.cursor.execute("SELECT force_joined FROM users WHERE user_id = ?", (user_id,))
        user_data = self.cursor.fetchone()
        if user_data and user_data[0]:
            await self.show_main_menu(update, context, welcome_text)
        else:
            keyboard = [
                [InlineKeyboardButton("✅ Verify Join", callback_data="verify_join")],
                [InlineKeyboardButton("🔗 Join Channel", url=f"https://t.me/{FORCE_JOIN_CHANNEL[1:]}")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            # Use reply_text if /start, otherwise edit
            if update.message:
                await update.message.reply_text(f"{welcome_text}\n\n⚠️ Please join our channel and click verify:", reply_markup=reply_markup)
            else:
                await update.effective_chat.send_message(f"{welcome_text}\n\n⚠️ Please join our channel and click verify:", reply_markup=reply_markup)

    def _welcome_text_new_user(self, first_name):
        return f"""🔴 *Welcome {first_name}*!

🎁 You received:
• 24 hours FREE premium access for Aadhaar & Vehicle lookups
• Use up to {DAILY_LOOKUP_LIMIT} lookups in 24 hours

📌 Menu available below — tap any button to start.
"""

    def _welcome_text_existing_user(self, first_name, premium_status, premium_expiry, lookup_count, bonus_lookups):
        status = "✅ ACTIVE" if premium_status else "❌ INACTIVE"
        expiry_text = premium_expiry if premium_expiry else "N/A"
        return f"""🔴 *Welcome back {first_name}*!

💎 Premium: {status}
⏰ Premium expiry: {expiry_text}
🔎 Today's lookups used: {lookup_count}/{DAILY_LOOKUP_LIMIT} (bonus: {bonus_lookups})
"""

    async def show_main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text=None):
        """Show main menu with only the requested buttons."""
        user = update.effective_user
        user_id = user.id if user else None

        # Ensure user exists in DB (create minimal if not)
        if user_id:
            self.cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
            if not self.cursor.fetchone():
                referral_code = self.generate_referral_code(user_id)
                self.cursor.execute(
                    "INSERT OR IGNORE INTO users (user_id, username, first_name, referral_code) VALUES (?, ?, ?, ?)",
                    (user_id, user.username, user.first_name or "User", referral_code)
                )
                self.conn.commit()

        if not text:
            text = "🔴 *OSINT Bot - Main Menu*\n\nSelect a service below:"

        # Buttons exactly as requested:
        keyboard = [
            [InlineKeyboardButton("🇮🇳 Indian Number Details", callback_data="service_india_number")],
            [InlineKeyboardButton("🇵🇰 Pakistan Number Details", callback_data="service_pak_number")],
            [InlineKeyboardButton("🆔 Aadhaar Number Details", callback_data="service_aadhaar")],
            [InlineKeyboardButton("🚗 Vehicle Number Details", callback_data="service_vehicle")],
            [InlineKeyboardButton("💳 UPI Details", callback_data="service_upi")],
            [InlineKeyboardButton("👥 Referral", callback_data="service_refer"),
             InlineKeyboardButton("🆘 Help", callback_data="service_help")],
            [InlineKeyboardButton("💰 My Credits / Quota", callback_data="service_mycredits")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # If this call came from a callback, edit message; else send new message.
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
            except Exception:
                # fallback to sending a new message if editing fails
                await update.effective_chat.send_message(text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, reply_markup=reply_markup)

    async def handle_service_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Route button presses to request handlers and show example prompts."""
        query = update.callback_query
        await query.answer()
        data = query.data
        user_id = query.from_user.id

        # Before anything, ensure the user's lookup window is current (reset if older than 24h)
        self._reset_lookup_window_if_needed(user_id)

        mapping = {
            "service_india_number": self.request_india_number,
            "service_pak_number": self.request_pak_number,
            "service_aadhaar": self.request_aadhaar_number,
            "service_vehicle": self.request_vehicle_number,
            "service_upi": self.request_upi,
            "service_mycredits": self.show_my_credits,
            "service_refer": self.show_referral_info,
            "service_help": self.show_help
        }

        handler = mapping.get(data)
        if handler:
            await handler(update, context)
        else:
            await query.edit_message_text("❌ Unknown action. Returning to menu.")
            await asyncio.sleep(1)
            await self.show_main_menu(update, context)

    def _reset_lookup_window_if_needed(self, user_id):
        """Reset lookup_count if last_lookup is older than 24 hours."""
        self.cursor.execute("SELECT last_lookup, lookup_count FROM users WHERE user_id = ?", (user_id,))
        row = self.cursor.fetchone()
        if not row:
            return
        last_lookup, lookup_count = row
        if last_lookup:
            try:
                last_dt = datetime.fromisoformat(last_lookup) if isinstance(last_lookup, str) else last_lookup
                if datetime.now() - last_dt >= timedelta(hours=24):
                    # Reset
                    self.cursor.execute("UPDATE users SET lookup_count = 0, last_lookup = NULL WHERE user_id = ?", (user_id,))
                    self.conn.commit()
            except Exception as e:
                logger.error(f"Error resetting lookup window for {user_id}: {e}")

    async def request_india_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show example and set expecting state for Indian number."""
        text = "📱 *Indian Number Lookup*\n\nExample: `919864136885`\n\nPlease send the Indian mobile number (country code + number or plain)."
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")] ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        context.user_data['expecting'] = 'india_number'

    async def request_pak_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show example and set expecting state for Pakistan number."""
        text = "📱 *Pakistan Number Lookup*\n\nExample: `3003658169`\n\nPlease send the Pakistan mobile number."
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")] ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        context.user_data['expecting'] = 'pak_number'

    async def request_aadhaar_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = "🆔 *Aadhaar Lookup*\n\nExample: `123456789012`\n\nPlease send the 12-digit Aadhaar number."
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        context.user_data['expecting'] = 'aadhaar'

    async def request_vehicle_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = "🚗 *Vehicle Lookup*\n\nExample: `MH02FZ0555`\n\nPlease send the vehicle number."
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        context.user_data['expecting'] = 'vehicle'

    async def request_upi(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = "💳 *UPI Lookup*\n\nExample: `someone@upi` or `9876543210@upi`\n\nPlease send the UPI ID."
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
        context.user_data['expecting'] = 'upi'

    async def show_my_credits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show referral progress and remaining quota / bonus lookups."""
        user_id = update.callback_query.from_user.id
        self.cursor.execute("SELECT lookup_count, last_lookup, bonus_lookups, pending_referrals, referral_code FROM users WHERE user_id = ?", (user_id,))
        row = self.cursor.fetchone()
        if not row:
            await update.callback_query.edit_message_text("❌ Please /start first to initialize your account.")
            return
        lookup_count, last_lookup, bonus_lookups, pending_referrals, referral_code = row
        # Calculate remaining
        remaining = DAILY_LOOKUP_LIMIT - (lookup_count or 0)
        if remaining < 0:
            remaining = 0
        text = f"""💰 *Quota & Referral*

🔴 Daily lookups used: {lookup_count}/{DAILY_LOOKUP_LIMIT}
🔸 Bonus lookups available: {bonus_lookups}
🔸 Pending referrals (to unlock bonus): {pending_referrals}
🔗 Your referral link: `https://t.me/{(await self.application.bot.get_me()).username}?start={referral_code}`

• Refer {REFERRALS_REQUIRED_FOR_BONUS} friends to get {BONUS_BATCH_SIZE} extra lookups immediately.
"""
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

    async def show_referral_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show referral info and pending referrals."""
        user_id = update.callback_query.from_user.id
        self.cursor.execute("SELECT referral_code, pending_referrals, bonus_lookups FROM users WHERE user_id = ?", (user_id,))
        row = self.cursor.fetchone()
        if not row:
            await update.callback_query.edit_message_text("❌ Please /start first.")
            return
        referral_code, pending_referrals, bonus_lookups = row
        bot_username = (await self.application.bot.get_me()).username
        referral_link = f"https://t.me/{bot_username}?start={referral_code}"
        text = f"""👥 *Refer & Earn*

🎁 Invite friends using your referral link:
🔗 {referral_link}

🔸 Pending referrals: {pending_referrals}
🔸 Bonus batches available: {bonus_lookups} (each grants {BONUS_BATCH_SIZE} extra lookups)

To convert pending referrals into a bonus batch, press the button below.
"""
        keyboard = [
            [InlineKeyboardButton("Convert 2 Pending → +5 Lookups", callback_data="convert_referral")],
            [InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = f"""🆘 *Help - OSINT Bot (Red Accent)*

Use the menu to select:
• 🇮🇳 Indian Number Details
• 🇵🇰 Pakistan Number Details
• 🆔 Aadhaar Number Details
• 🚗 Vehicle Number Details
• 💳 UPI Details

Limits:
• You may perform {DAILY_LOOKUP_LIMIT} lookups per 24 hours.
• If you reach the limit, invite {REFERRALS_REQUIRED_FOR_BONUS} friends to unlock {BONUS_BATCH_SIZE} more lookups instantly.

Need admin help? Contact: {CONTACT_USERNAME}
"""
        keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="menu_back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)

    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text input for various services"""
        user_id = update.effective_user.id
        text = update.message.text.strip()

        expecting = context.user_data.get('expecting')
        if not expecting:
            await update.message.reply_text("Please use the menu buttons to select a service.")
            return

        # Ensure user is in DB and reset window if needed
        self._reset_lookup_window_if_needed(user_id)

        if expecting == 'india_number':
            await self._perform_lookup_with_limit(self.process_india_number, update, context, text)
        elif expecting == 'pak_number':
            await self._perform_lookup_with_limit(self.process_pak_number, update, context, text)
        elif expecting == 'aadhaar':
            await self._perform_lookup_with_limit(self.process_aadhaar_lookup, update, context, text)
        elif expecting == 'vehicle':
            await self._perform_lookup_with_limit(self.process_vehicle_lookup, update, context, text)
        elif expecting == 'upi':
            await self._perform_lookup_with_limit(self.process_upi, update, context, text)
        else:
            await update.message.reply_text("Unknown operation.")

        context.user_data['expecting'] = None

    async def _perform_lookup_with_limit(self, lookup_func, update, context, param):
        """Check limits, apply bonus or require referrals, then call actual lookup func."""
        user_id = update.effective_user.id

        # Fetch user's lookup state
        self.cursor.execute("SELECT lookup_count, last_lookup, bonus_lookups, pending_referrals FROM users WHERE user_id = ?", (user_id,))
        row = self.cursor.fetchone()
        if not row:
            await update.message.reply_text("❌ Please /start first.")
            return

        lookup_count, last_lookup, bonus_lookups, pending_referrals = row
        lookup_count = lookup_count or 0
        bonus_lookups = bonus_lookups or 0
        pending_referrals = pending_referrals or 0

        # If user has bonus_lookups > 0, consume them first (they grant extra allowed searches)
        if bonus_lookups > 0:
            # consume one bonus lookup (count does not increment daily limit, but bonus_lookups reduces)
            self.cursor.execute("UPDATE users SET bonus_lookups = bonus_lookups - 1 WHERE user_id = ?", (user_id,))
            self.conn.commit()
            logger.info(f"User {user_id} used a bonus lookup. Remaining bonus decreased.")
            await lookup_func(update, context, param)
            return

        # If daily limit not reached, allow and increment count
        if lookup_count < DAILY_LOOKUP_LIMIT:
            # increment lookup_count; set last_lookup if not present
            if not last_lookup:
                now_iso = datetime.now().isoformat()
                self.cursor.execute("UPDATE users SET lookup_count = lookup_count + 1, last_lookup = ? WHERE user_id = ?", (now_iso, user_id))
            else:
                self.cursor.execute("UPDATE users SET lookup_count = lookup_count + 1 WHERE user_id = ?", (user_id,))
            self.conn.commit()
            await lookup_func(update, context, param)
            return
        else:
            # limit reached; check pending_referrals to possibly convert to bonus
            if pending_referrals >= REFERRALS_REQUIRED_FOR_BONUS:
                # notify user they can convert or auto-convert? We'll ask to convert or convert automatically.
                # We'll auto-convert here to make UX easier: consume required pending referrals and grant bonus batch
                self.cursor.execute(
                    "UPDATE users SET pending_referrals = pending_referrals - ?, bonus_lookups = bonus_lookups + ? WHERE user_id = ?",
                    (REFERRALS_REQUIRED_FOR_BONUS, BONUS_BATCH_SIZE, user_id)
                )
                self.conn.commit()
                await update.message.reply_text(f"🎉 You had {pending_referrals} pending referrals — {REFERRALS_REQUIRED_FOR_BONUS} were converted into +{BONUS_BATCH_SIZE} extra lookups. Trying your request again...")
                # now call recursively to use bonus
                # reset lookup window if necessary and call again
                await self._perform_lookup_with_limit(lookup_func, update, context, param)
                return
            else:
                # Tell user to invite 2 friends or wait 24 hours
                await update.message.reply_text(
                    f"⚠️ You have reached the daily limit of {DAILY_LOOKUP_LIMIT} lookups.\n\n"
                    f"Invite {REFERRALS_REQUIRED_FOR_BONUS} friends using your referral link to unlock {BONUS_BATCH_SIZE} more lookups instantly, or wait 24 hours for the limit to reset."
                )
                return

    # ---- Lookup processors (call external APIs and return formatted card-style output) ----

    async def process_india_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE, phone_number: str):
        user_id = update.effective_user.id
        processing_msg = await update.message.reply_text("🔍 Fetching Indian number information...")
        try:
            phone_number_clean = phone_number.replace(' ', '').replace('+', '')
            api_url = f"{INDIA_PHONE_API_BASE_URL}{phone_number_clean}"
            response = requests.get(api_url, timeout=30)
            if response.status_code == 200:
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    await processing_msg.edit_text("❌ Invalid JSON response from India phone API.")
                    return

                # Format a card-style message (fields may vary by API)
                result_text = "🔴 *Indian Number Information Found*\n\n"
                result_text += f"🔢 Number: `{phone_number_clean}`\n"
                # include some commonly expected keys if present
                for k in ['name', 'operator', 'circle', 'location', 'type']:
                    if k in data:
                        result_text += f"{k.capitalize()}: {data.get(k, 'N/A')}\n"
                # fallback: pretty-print entire JSON small subset
                if not any(k in data for k in ['name','operator','circle','location','type']):
                    # show top-level keys
                    for key, val in list(data.items())[:6]:
                        result_text += f"{key}: {val}\n"

                result_text += "\n⚠️ *Authorized use only*"
                await processing_msg.delete()
                await update.message.reply_text(result_text)
            else:
                await processing_msg.edit_text(f"❌ India Phone API Error: Status code {response.status_code}")
        except requests.exceptions.Timeout:
            await processing_msg.edit_text("⏰ Request timeout. Please try again.")
        except Exception as e:
            logger.error(f"India phone lookup error: {e}")
            await processing_msg.edit_text("❌ An error occurred. Please try again.")
        # After lookup, show menu again
        await self.show_main_menu(update, context)

    async def process_pak_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE, phone_number: str):
        user_id = update.effective_user.id
        processing_msg = await update.message.reply_text("🔍 Fetching Pakistan number information...")
        try:
            phone_number_clean = phone_number.replace(' ', '').replace('+', '')
            api_url = f"{PAKISTAN_PHONE_API_BASE_URL}{phone_number_clean}"
            response = requests.get(api_url, timeout=30)
            if response.status_code == 200:
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    await processing_msg.edit_text("❌ Invalid JSON response from Pakistan phone API.")
                    return

                if data.get('success') and data.get('records'):
                    records = data['records']
                    result_text = "🔴 *Pakistan Phone Information Found*\n\n"
                    result_text += f"🔢 Number: `{phone_number_clean}`\n"
                    result_text += f"📊 Total Records: {len(records)}\n\n"
                    for i, r in enumerate(records, 1):
                        result_text += f"— Record {i} —\n"
                        result_text += f"Name: {r.get('Name','N/A')}\nMobile: {r.get('Mobile','N/A')}\nCNIC: {r.get('CNIC','N/A')}\nAddress: {r.get('Address','N/A')}\n\n"
                else:
                    result_text = f"❌ No phone information found for `{phone_number_clean}`."
                await processing_msg.delete()
                await update.message.reply_text(result_text)
            else:
                await processing_msg.edit_text(f"❌ Pakistan Phone API Error: Status code {response.status_code}")
        except requests.exceptions.Timeout:
            await processing_msg.edit_text("⏰ Request timeout. Please try again.")
        except Exception as e:
            logger.error(f"Pakistan phone lookup error: {e}")
            await processing_msg.edit_text("❌ An error occurred. Please try again.")
        await self.show_main_menu(update, context)

    async def process_aadhaar_lookup(self, update: Update, context: ContextTypes.DEFAULT_TYPE, aadhaar_number: str):
        processing_msg = await update.message.reply_text("🔍 Fetching Aadhaar information...")
        try:
            aadhaar_number_clean = aadhaar_number.strip()
            if not aadhaar_number_clean.isdigit() or len(aadhaar_number_clean) != 12:
                await processing_msg.edit_text("❌ Invalid Aadhaar number. Must be 12 digits.")
                return

            api_url = f"{AADHAAR_API_BASE_URL}{aadhaar_number_clean}"
            response = requests.get(api_url, timeout=30)
            if response.status_code == 200:
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    await processing_msg.edit_text("❌ Invalid JSON response from Aadhaar API.")
                    return

                result_text = "🔴 *Aadhaar Information Found*\n\n"
                result_text += f"🔢 Aadhaar: `{aadhaar_number_clean}`\n"
                result_text += f"Name: {data.get('name','N/A')}\nGender: {data.get('gender','N/A')}\nDOB: {data.get('dob','N/A')}\nPhone: {data.get('phone','N/A')}\nEmail: {data.get('email','N/A')}\nAddress: {data.get('address','N/A')}\n\n⚠️ *Authorized use only*"
                await processing_msg.delete()
                await update.message.reply_text(result_text)
            else:
                await processing_msg.edit_text(f"❌ Aadhaar API Error: Status code {response.status_code}")
        except requests.exceptions.Timeout:
            await processing_msg.edit_text("⏰ Request timeout. Please try again.")
        except Exception as e:
            logger.error(f"Aadhaar lookup error: {e}")
            await processing_msg.edit_text("❌ An error occurred. Please try again.")
        await self.show_main_menu(update, context)

    async def process_vehicle_lookup(self, update: Update, context: ContextTypes.DEFAULT_TYPE, vehicle_number: str):
        processing_msg = await update.message.reply_text("🔍 Fetching Vehicle information...")
        try:
            vehicle_clean = vehicle_number.upper().replace(' ', '')
            if len(vehicle_clean) < 5:
                await processing_msg.edit_text("❌ Invalid vehicle number.")
                return

            api_url = f"{VEHICLE_API_BASE_URL}{vehicle_clean}"
            response = requests.get(api_url, timeout=30)
            if response.status_code == 200:
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    await processing_msg.edit_text("❌ Invalid JSON response from Vehicle API.")
                    return

                if data.get('status') == 'success' or data:
                    result_text = "🔴 *Vehicle Information Found*\n\n"
                    result_text += f"🔢 Vehicle: `{vehicle_clean}`\n"
                    result_text += f"Owner: {data.get('owner','N/A')}\nModel: {data.get('model','N/A')}\nPhone: {data.get('phone','N/A')}\nAddress: {data.get('address','N/A')}\n\n⚠️ *Authorized use only*"
                else:
                    result_text = "❌ No vehicle information found."
                await processing_msg.delete()
                await update.message.reply_text(result_text)
            else:
                await processing_msg.edit_text(f"❌ Vehicle API Error: Status code {response.status_code}")
        except requests.exceptions.Timeout:
            await processing_msg.edit_text("⏰ Request timeout. Please try again.")
        except Exception as e:
            logger.error(f"Vehicle lookup error: {e}")
            await processing_msg.edit_text("❌ An error occurred. Please try again.")
        await self.show_main_menu(update, context)

    async def process_upi(self, update: Update, context: ContextTypes.DEFAULT_TYPE, upi_id: str):
        processing_msg = await update.message.reply_text("🔍 Fetching UPI information...")
        try:
            upi_clean = upi_id.strip()
            api_url = f"{UPI_API_BASE_URL}{upi_clean}"
            response = requests.get(api_url, timeout=30)
            if response.status_code == 200:
                try:
                    data = response.json()
                except json.JSONDecodeError:
                    await processing_msg.edit_text("❌ Invalid JSON response from UPI API.")
                    return

                result_text = "🔴 *UPI Information Found*\n\n"
                result_text += f"🔸 UPI ID: `{upi_clean}`\n"
                # show some likely fields
                for k in ['name', 'vpa', 'bank', 'status']:
                    if k in data:
                        result_text += f"{k.capitalize()}: {data.get(k)}\n"
                # fallback show response head
                if not any(k in data for k in ['name','vpa','bank','status']):
                    for key, val in list(data.items())[:6]:
                        result_text += f"{key}: {val}\n"
                result_text += "\n⚠️ *Authorized use only*"
                await processing_msg.delete()
                await update.message.reply_text(result_text)
            else:
                await processing_msg.edit_text(f"❌ UPI API Error: Status code {response.status_code}")
        except requests.exceptions.Timeout:
            await processing_msg.edit_text("⏰ Request timeout. Please try again.")
        except Exception as e:
            logger.error(f"UPI lookup error: {e}")
            await processing_msg.edit_text("❌ An error occurred. Please try again.")
        await self.show_main_menu(update, context)

    # ---- Referral conversion handler ----
    async def convert_referral(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        self.cursor.execute("SELECT pending_referrals FROM users WHERE user_id = ?", (user_id,))
        row = self.cursor.fetchone()
        if not row:
            await query.edit_message_text("❌ Please /start first.")
            return
        pending = row[0] or 0
        if pending < REFERRALS_REQUIRED_FOR_BONUS:
            await query.edit_message_text(f"❌ You need {REFERRALS_REQUIRED_FOR_BONUS} pending referrals to convert. Pending: {pending}")
            return
        # Convert
        self.cursor.execute("UPDATE users SET pending_referrals = pending_referrals - ?, bonus_lookups = bonus_lookups + ? WHERE user_id = ?",
                            (REFERRALS_REQUIRED_FOR_BONUS, BONUS_BATCH_SIZE, user_id))
        self.conn.commit()
        await query.edit_message_text(f"✅ Converted {REFERRALS_REQUIRED_FOR_BONUS} pending referrals into +{BONUS_BATCH_SIZE} bonus lookups!")
        await asyncio.sleep(1)
        await self.show_main_menu(update, context)

    # ---- Utility functions ----

    def get_pending_referrals(self, user_id):
        self.cursor.execute("SELECT pending_referrals FROM users WHERE user_id = ?", (user_id,))
        r = self.cursor.fetchone()
        return r[0] if r else 0

    async def verify_join(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user_id = query.from_user.id
        try:
            chat_member = await context.bot.get_chat_member(chat_id=FORCE_JOIN_CHANNEL, user_id=user_id)
            if chat_member.status in ['member', 'administrator', 'creator']:
                self.cursor.execute("UPDATE users SET force_joined = TRUE WHERE user_id = ?", (user_id,))
                self.conn.commit()
                await query.edit_message_text("✅ Verification successful! Loading main menu...")
                await asyncio.sleep(1)
                await self.show_main_menu(update, context, "✅ Verification successful! You can now use all bot features.")
            else:
                await query.answer("❌ Please join the channel first!", show_alert=True)
        except Exception as e:
            logger.error(f"Error in verify_join: {e}")
            await query.answer("❌ Error verifying join. Please try again.", show_alert=True)

    async def premium_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ This command is for admin only.")
            return
        if not context.args:
            await update.message.reply_text("❌ Usage: /premium <user_id>")
            return
        try:
            target_user_id = int(context.args[0])
            expiry_time = datetime.now() + timedelta(days=30)
            self.cursor.execute(
                "UPDATE users SET premium_status = TRUE, premium_expiry = ? WHERE user_id = ?",
                (expiry_time, target_user_id)
            )
            if self.cursor.rowcount > 0:
                self.conn.commit()
                await update.message.reply_text(f"✅ Premium granted to user {target_user_id} for 30 days")
                try:
                    await context.bot.send_message(chat_id=target_user_id, text="🎉 You have been granted 30 days premium access!")
                except:
                    pass
            else:
                await update.message.reply_text("❌ User not found")
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID")
        except Exception as e:
            logger.error(f"Premium command error: {e}")
            await update.message.reply_text("❌ Error granting premium")

    async def add_credits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ This command is for admin only.")
            return
        # This bot doesn't use credits, but keep this command for compatibility
        if len(context.args) < 2:
            await update.message.reply_text("❌ Usage: /addcredits <user_id> <amount>")
            return
        try:
            target_user_id = int(context.args[0])
            credit_amount = int(context.args[1])
            self.cursor.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (credit_amount, target_user_id))
            if self.cursor.rowcount > 0:
                self.conn.commit()
                await update.message.reply_text(f"✅ Added {credit_amount} credits to user {target_user_id}")
                try:
                    await context.bot.send_message(chat_id=target_user_id, text=f"🎉 You received {credit_amount} credits!")
                except:
                    pass
            else:
                await update.message.reply_text("❌ User not found")
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID or amount")
        except Exception as e:
            logger.error(f"Add credits error: {e}")
            await update.message.reply_text("❌ Error adding credits")

    async def broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ This command is for admin only.")
            return
        if not context.args:
            await update.message.reply_text("❌ Usage: /broadcast <message>")
            return
        broadcast_text = ' '.join(context.args)
        self.cursor.execute("INSERT OR REPLACE INTO admin (id, broadcast_text) VALUES (1, ?)", (broadcast_text,))
        self.conn.commit()
        self.cursor.execute("SELECT user_id FROM users")
        users = self.cursor.fetchall()
        success_count = 0
        fail_count = 0
        processing_msg = await update.message.reply_text(f"📢 Broadcasting to {len(users)} users...")
        for user_id_row, in users:
            try:
                await context.bot.send_message(chat_id=user_id_row, text=f"📢 Announcement:\n\n{broadcast_text}")
                success_count += 1
            except Exception:
                fail_count += 1
            await asyncio.sleep(0.1)
        await processing_msg.edit_text(f"📊 Broadcast Complete:\n✅ Success: {success_count}\n❌ Failed: {fail_count}")

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ This command is for admin only.")
            return
        self.cursor.execute("SELECT COUNT(*) FROM users")
        total_users = self.cursor.fetchone()[0]
        self.cursor.execute("SELECT COUNT(*) FROM users WHERE premium_status = 1")
        premium_users = self.cursor.fetchone()[0]
        self.cursor.execute("SELECT COUNT(*) FROM users WHERE force_joined = 1")
        verified_users = self.cursor.fetchone()[0]
        self.cursor.execute("SELECT COUNT(*) FROM referrals")
        total_referrals = self.cursor.fetchone()[0]
        self.cursor.execute("SELECT SUM(credits) FROM users")
        total_credits = self.cursor.fetchone()[0] or 0
        stats_text = f"""📊 *Bot Statistics*

👥 Total Users: {total_users}
💎 Premium Users: {premium_users}
✅ Verified Users: {verified_users}
👥 Total Referrals: {total_referrals}
"""
        await update.message.reply_text(stats_text)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # If user is expecting input, handle it
        if context.user_data.get('expecting'):
            await self.handle_text_input(update, context)
        else:
            # Show menu without requiring /start again
            await self.show_main_menu(update, context)

    def run(self):
        if not BOT_TOKEN:
            logger.error("❌ BOT_TOKEN not found! Please set environment variable.")
            return

        self.application = Application.builder().token(BOT_TOKEN).build()

        # Command handlers
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(CommandHandler("premium", self.premium_command))
        self.application.add_handler(CommandHandler("addcredits", self.add_credits))
        self.application.add_handler(CommandHandler("broadcast", self.broadcast))
        self.application.add_handler(CommandHandler("stats", self.stats))

        # Callback handlers for services
        self.application.add_handler(CallbackQueryHandler(self.handle_service_selection, pattern="^service_india_number$"))
        self.application.add_handler(CallbackQueryHandler(self.handle_service_selection, pattern="^service_pak_number$"))
        self.application.add_handler(CallbackQueryHandler(self.handle_service_selection, pattern="^service_aadhaar$"))
        self.application.add_handler(CallbackQueryHandler(self.handle_service_selection, pattern="^service_vehicle$"))
        self.application.add_handler(CallbackQueryHandler(self.handle_service_selection, pattern="^service_upi$"))
        self.application.add_handler(CallbackQueryHandler(self.handle_service_selection, pattern="^service_mycredits$"))
        self.application.add_handler(CallbackQueryHandler(self.handle_service_selection, pattern="^service_refer$"))
        self.application.add_handler(CallbackQueryHandler(self.handle_service_selection, pattern="^service_help$"))
        self.application.add_handler(CallbackQueryHandler(self.convert_referral, pattern="^convert_referral$"))
        self.application.add_handler(CallbackQueryHandler(self.verify_join, pattern="^verify_join$"))
        self.application.add_handler(CallbackQueryHandler(self.handle_service_selection, pattern="^menu_back$"))

        # Message handler for text input
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        logger.info("Bot is starting...")
        self.application.run_polling()

if __name__ == '__main__':
    bot = OSINTBot()
    bot.run()
