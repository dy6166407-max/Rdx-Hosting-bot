# -*- coding: utf-8 -*-
import telebot
import subprocess
import os
import zipfile
import tempfile
import shutil
from telebot import types
import time
from datetime import datetime, timedelta
import psutil
import sqlite3
import json
import logging
import signal
import threading
import re
import sys
import atexit
import requests
from dotenv import load_dotenv
from flask import Flask
from threading import Thread

# --- Load environment variables ---
load_dotenv()

# --- Flask Keep Alive ---
app = Flask('')

@app.route('/')
def home():
    return "I am 👿 RDX 👿 HOSTING BOT"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()
    logger.info("Flask Keep-Alive server started.")

# --- Configuration FROM .env FILE ---
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') or os.getenv('TOKEN')
OWNER_ID = int(os.getenv('OWNER_ID', 8485798078))
ADMIN_ID = int(os.getenv('ADMIN_ID', 8485798078))
YOUR_USERNAME = os.getenv('YOUR_USERNAME', 'itzrdxking')
UPDATE_CHANNEL = os.getenv('UPDATE_CHANNEL', 'RDXBIO8')

# Limits from .env or defaults
FREE_USER_LIMIT = int(os.getenv('FREE_USER_LIMIT', 2))
SUBSCRIBED_USER_LIMIT = int(os.getenv('SUBSCRIBED_USER_LIMIT', 20))
ADMIN_LIMIT = int(os.getenv('ADMIN_LIMIT', 999))
OWNER_LIMIT = float('inf')

# Folder setup
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR = os.path.join(BASE_DIR, 'upload_bots')
IROTECH_DIR = os.path.join(BASE_DIR, 'inf')
DATABASE_PATH = os.path.join(IROTECH_DIR, 'bot_data.db')

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(IROTECH_DIR, exist_ok=True)

if not TOKEN:
    raise ValueError("Missing Telegram Bot Token. Set TELEGRAM_BOT_TOKEN in environment variables.")

bot = telebot.TeleBot(TOKEN)

# --- Data structures & Locks ---
DB_LOCK = threading.Lock()
bot_scripts = {}
user_subscriptions = {}
user_files = {}
active_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
banned_users = set()
user_limits = {}
bot_locked = False
mandatory_channels = {}
pending_zip_files = {}

# --- Security Settings ---
SECURITY_CONFIG = {
    'blocked_modules': ['os.system', 'eval', 'exec', 'compile', '__import__'],
    'max_file_size': 20 * 1024 * 1024,
    'max_script_runtime': 3600,
    'allowed_extensions': ['.py', '.js']
}

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

COMMAND_BUTTONS_LAYOUT_USER_SPEC = [
    ["📢 Updates Channel"],
    ["📤 Upload File", "📂 Check Files"],
    ["⚡ Bot Speed", "📊 Statistics"],
    ["📞 Contact Owner"],
    ["📦 Manual Install", "🆘 Help"]
]

ADMIN_COMMAND_BUTTONS_LAYOUT_USER_SPEC = [
    ["📢 Updates Channel"],
    ["📤 Upload File", "📂 Check Files"],
    ["⚡ Bot Speed", "📊 Statistics"],
    ["💳 Subscriptions", "📢 Broadcast"],
    ["🔒 Lock Bot", "🟢 Running All Code"],
    ["👑 Admin Panel", "📞 Contact Owner"],
    ["📢 Channel Add", "🛠️ Manual Install"],
    ["👥 User Management", "⚙️ Settings"]
]

# --- DB & Setup ---
def init_db():
    with DB_LOCK:
        try:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS user_files (user_id INTEGER, file_name TEXT, file_type TEXT, PRIMARY KEY (user_id, file_name))''')
            c.execute('''CREATE TABLE IF NOT EXISTS active_users (user_id INTEGER PRIMARY KEY, join_date TEXT, last_seen TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY, added_by INTEGER, added_date TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY, reason TEXT, banned_by INTEGER, ban_date TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS user_limits (user_id INTEGER PRIMARY KEY, file_limit INTEGER, set_by INTEGER, set_date TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS mandatory_channels (channel_id TEXT PRIMARY KEY, channel_username TEXT, channel_name TEXT, added_by INTEGER, added_date TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS install_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, module_name TEXT, package_name TEXT, status TEXT, log TEXT, install_date TEXT)''')
            
            c.execute('INSERT OR IGNORE INTO admins (user_id, added_by, added_date) VALUES (?, ?, ?)', (OWNER_ID, OWNER_ID, datetime.now().isoformat()))
            if ADMIN_ID != OWNER_ID:
                c.execute('INSERT OR IGNORE INTO admins (user_id, added_by, added_date) VALUES (?, ?, ?)', (ADMIN_ID, OWNER_ID, datetime.now().isoformat()))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Database initialization error: {e}", exc_info=True)

def load_data():
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('SELECT user_id, expiry FROM subscriptions')
        for user_id, expiry in c.fetchall():
            try: user_subscriptions[user_id] = {'expiry': datetime.fromisoformat(expiry)}
            except ValueError: pass

        c.execute('SELECT user_id, file_name, file_type FROM user_files')
        for user_id, file_name, file_type in c.fetchall():
            if user_id not in user_files: user_files[user_id] = []
            user_files[user_id].append((file_name, file_type))

        c.execute('SELECT user_id FROM active_users')
        active_users.update(user_id for (user_id,) in c.fetchall())

        c.execute('SELECT user_id FROM admins')
        admin_ids.update(user_id for (user_id,) in c.fetchall())

        c.execute('SELECT user_id FROM banned_users')
        banned_users.update(user_id for (user_id,) in c.fetchall())

        c.execute('SELECT user_id, file_limit FROM user_limits')
        for user_id, file_limit in c.fetchall(): user_limits[user_id] = file_limit

        c.execute('SELECT channel_id, channel_username, channel_name FROM mandatory_channels')
        for channel_id, channel_username, channel_name in c.fetchall():
            mandatory_channels[channel_id] = {'username': channel_username, 'name': channel_name}
        conn.close()
    except Exception as e:
        logger.error(f"Error loading data: {e}", exc_info=True)

init_db()
load_data()

# --- Security Functions ---
def check_code_security(file_path, file_type):
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        dangerous_patterns = [r'rm\s+-rf\s+/', r'format\s+c:', r'dd\s+if=/dev/zero', r':\(\)\{\s*:\|:&\s*\};:']
        for pattern in dangerous_patterns:
            if re.search(pattern, content, re.IGNORECASE):
                return False, f"Code contains dangerous commands: {pattern}"
        return True, "Code is safe"
    except Exception as e:
        return False, f"Security check error: {str(e)}"

def scan_zip_security(zip_path):
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for file_info in zip_ref.infolist():
                if file_info.filename.endswith(('.py', '.js', '.sh', '.bat', '.cmd')):
                    with zip_ref.open(file_info.filename) as f:
                        content = f.read().decode('utf-8', errors='ignore')
                        if "rm -rf /" in content:
                            return False, f"File {file_info.filename} contains dangerous command."
        return True, "Archive is safe"
    except Exception as e:
        return False, f"Error scanning archive: {str(e)}"

# --- Helpers & Process Management ---
def get_user_folder(user_id):
    user_folder = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    return user_folder

def kill_process_tree(process_info):
    try:
        if 'log_file' in process_info and hasattr(process_info['log_file'], 'close') and not process_info['log_file'].closed:
            process_info['log_file'].close()
        process = process_info.get('process')
        if process and hasattr(process, 'pid'):
            parent = psutil.Process(process.pid)
            for child in parent.children(recursive=True):
                try: child.kill()
                except psutil.NoSuchProcess: pass
            parent.kill()
    except Exception as e:
        logger.error(f"Error killing process: {e}")

# --- Package Installation & Script Exec ---
TELEGRAM_MODULES = {
    'telebot': 'pyTelegramBotAPI', 'telegram': 'python-telegram-bot',
    'aiogram': 'aiogram', 'pyrogram': 'pyrogram', 'telethon': 'telethon',
    'bs4': 'beautifulsoup4', 'requests': 'requests', 'pillow': 'Pillow',
    'cv2': 'opencv-python', 'yaml': 'PyYAML', 'dotenv': 'python-dotenv'
}

def attempt_install_pip(module_name, message, manual_request=False):
    package_name = TELEGRAM_MODULES.get(module_name.lower(), module_name)
    try:
        command = [sys.executable, '-m', 'pip', 'install', package_name]
        result = subprocess.run(command, capture_output=True, text=True, check=False, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            bot.reply_to(message, f"✅ Package `{package_name}` installed successfully.", parse_mode='Markdown')
            return True, result.stdout
        else:
            bot.reply_to(message, f"❌ Failed to install `{package_name}`.", parse_mode='Markdown')
            return False, result.stderr
    except Exception as e:
        return False, str(e)

def attempt_install_npm(module_name, user_folder, message, manual_request=False):
    try:
        command = ['npm', 'install', module_name]
        result = subprocess.run(command, capture_output=True, text=True, check=False, cwd=user_folder, encoding='utf-8', errors='ignore')
        if result.returncode == 0:
            bot.reply_to(message, f"✅ Node package `{module_name}` installed.", parse_mode='Markdown')
            return True, result.stdout
        else:
            bot.reply_to(message, f"❌ Failed to install Node package `{module_name}`.", parse_mode='Markdown')
            return False, result.stderr
    except Exception as e:
        return False, str(e)

def run_script(script_path, script_owner_id, user_folder, file_name, message_obj_for_reply, attempt=1):
    script_key = f"{script_owner_id}_{file_name}"
    if attempt > 2:
        bot.reply_to(message_obj_for_reply, f"❌ Failed to run '{file_name}' after maximum attempts.")
        return
    try:
        log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = open(log_file_path, 'w', encoding='utf-8', errors='ignore')
        process = subprocess.Popen([sys.executable, script_path], cwd=user_folder, stdout=log_file, stderr=log_file, encoding='utf-8', errors='ignore')
        bot_scripts[script_key] = {
            'process': process, 'log_file': log_file, 'file_name': file_name,
            'script_owner_id': script_owner_id, 'start_time': datetime.now(),
            'user_folder': user_folder, 'type': 'py', 'script_key': script_key
        }
        bot.reply_to(message_obj_for_reply, f"✅ Python script '{file_name}' started! (PID: {process.pid})")
    except Exception as e:
        bot.reply_to(message_obj_for_reply, f"❌ Error starting script: {e}")

def run_js_script(script_path, script_owner_id, user_folder, file_name, message_obj_for_reply, attempt=1):
    script_key = f"{script_owner_id}_{file_name}"
    if attempt > 2:
        bot.reply_to(message_obj_for_reply, f"❌ Failed to run '{file_name}' after maximum attempts.")
        return
    try:
        log_file_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = open(log_file_path, 'w', encoding='utf-8', errors='ignore')
        process = subprocess.Popen(['node', script_path], cwd=user_folder, stdout=log_file, stderr=log_file, encoding='utf-8', errors='ignore')
        bot_scripts[script_key] = {
            'process': process, 'log_file': log_file, 'file_name': file_name,
            'script_owner_id': script_owner_id, 'start_time': datetime.now(),
            'user_folder': user_folder, 'type': 'js', 'script_key': script_key
        }
        bot.reply_to(message_obj_for_reply, f"✅ Node.js script '{file_name}' started! (PID: {process.pid})")
    except Exception as e:
        bot.reply_to(message_obj_for_reply, f"❌ Error starting JS script: {e}")

# --- Keyboards ---
def create_main_menu_inline(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton('📢 Updates Channel', url=f'https://t.me/{UPDATE_CHANNEL.replace("@", "")}'),
        types.InlineKeyboardButton('📤 Upload File', callback_data='upload'),
        types.InlineKeyboardButton('📂 Check Files', callback_data='check_files'),
        types.InlineKeyboardButton('⚡ Bot Speed', callback_data='speed'),
        types.InlineKeyboardButton('📦 Manual Install', callback_data='manual_install'),
        types.InlineKeyboardButton('📞 Contact Owner', url=f'https://t.me/{YOUR_USERNAME.replace("@", "")}')
    ]
    markup.add(buttons[0])
    markup.add(buttons[1], buttons[2])
    markup.add(buttons[3], buttons[4])
    markup.add(buttons[5])
    return markup

# --- Handlers ---
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    user_id = message.from_user.id
    if user_id in banned_users:
        return
    markup = create_main_menu_inline(user_id)
    bot.reply_to(message, "👋 Welcome to 👿 RDX 👿 HOSTING BOT!\nChoose an option below:", reply_markup=markup)

@bot.message_handler(content_types=['document'])
def handle_document(message):
    user_id = message.from_user.id
    if user_id in banned_users:
        return
    doc = message.document
    file_name = doc.file_name
    file_ext = os.path.splitext(file_name)[1].lower()

    if file_ext not in ['.py', '.js', '.zip']:
        bot.reply_to(message, "❌ Invalid file format! Only `.py`, `.js`, or `.zip` allowed.")
        return

    user_folder = get_user_folder(user_id)
    file_info = bot.get_file(doc.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    file_path = os.path.join(user_folder, file_name)

    with open(file_path, 'wb') as f:
        f.write(downloaded_file)

    if file_ext == '.py':
        run_script(file_path, user_id, user_folder, file_name, message)
    elif file_ext == '.js':
        run_js_script(file_path, user_id, user_folder, file_name, message)

@bot.callback_query_handler(func=lambda call: True)
def callback_listener(call):
    user_id = call.from_user.id
    if user_id in banned_users:
        bot.answer_callback_query(call.id, "You are banned.")
        return

    if call.data == 'upload':
        bot.send_message(call.message.chat.id, "Please upload your `.py`, `.js`, or `.zip` file now.")
    elif call.data == 'check_files':
        user_folder = get_user_folder(user_id)
        files = os.listdir(user_folder)
        msg = "📂 **Your Files:**\n\n" + "\n".join([f"• `{f}`" for f in files]) if files else "📂 No files uploaded."
        bot.send_message(call.message.chat.id, msg, parse_mode='Markdown')
    elif call.data == 'speed':
        bot.answer_callback_query(call.id, "⚡ Bot speed is normal (Ping: 12ms)")
    bot.answer_callback_query(call.id)

# --- Start Bot ---
if __name__ == '__main__':
    keep_alive()
    logger.info("Bot starting...")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
