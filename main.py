import sqlite3
import os
import json
import shutil
import asyncio
from datetime import datetime, timedelta
from contextlib import contextmanager
from fastapi import FastAPI, Form, Request, Response, HTTPException, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
from typing import Optional
from passlib.context import CryptContext
import secrets
import qrcode
import io
import requests
import re
from html import escape as html_escape
from urllib.parse import urlparse
from functools import wraps

# --- НАСТРОЙКИ БЕЗОПАСНОСТИ ---
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# --- СТАТИЧЕСКИЕ ФАЙЛЫ ---
UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# Монтируем папку uploads как статическую директорию для обычных файлов
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# --- ГЛОБАЛЬНЫЙ СПИСОК ОНЛАЙН ПОЛЬЗОВАТЕЛЕЙ ---
online_users = {}

def add_online_user(room_id, username):
    if room_id not in online_users:
        online_users[room_id] = set()
    online_users[room_id].add(username)

def remove_online_user(room_id, username):
    if room_id in online_users:
        online_users[room_id].discard(username)
        if not online_users[room_id]:
            del online_users[room_id]

def is_user_online(room_id, username):
    return room_id in online_users and username in online_users[room_id]

def is_user_globally_online(username):
    # check room presence
    for users in online_users.values():
        if username in users:
            return True
    # check private connections in manager
    try:
        for key, conns in manager.private_connections.items():
            if conns and username in key.split(":"):
                return True
    except Exception:
        pass
    return False

# --- 1. БАЗА ДАННЫХ ---
@contextmanager
def get_db_connection():
    conn = sqlite3.connect("users.db")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def _extract_og_metadata(url: str):
    if not url:
        return {"title": "", "description": "", "image": ""}
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; LinkPreview/1.0)"}
        resp = requests.get(url, headers=headers, timeout=8)
        resp.raise_for_status()
        html = resp.text
        title = ""
        description = ""
        image = ""
        for pattern, key in [
            (r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', 'title'),
            (r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']', 'title'),
            (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', 'description'),
            (r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:description["\']', 'description'),
            (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', 'image'),
            (r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', 'image'),
        ]:
            match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if match:
                value = re.sub(r'\s+', ' ', match.group(1)).strip()
                if key == 'title': title = value
                if key == 'description': description = value
                if key == 'image': image = value
        if not title:
            match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
            if match:
                title = re.sub(r'\s+', ' ', match.group(1)).strip()
        if image and not image.startswith('http'):
            image = urlparse(url)._replace(path='', query='').geturl().rstrip('/') + '/' + image.lstrip('/') if not image.startswith('/') else urlparse(url)._replace(path=image, query='').geturl()
        return {"title": title, "description": description, "image": image}
    except Exception:
        return {"title": "", "description": "", "image": ""}


def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                username TEXT UNIQUE, 
                password_hash TEXT, 
                avatar TEXT, 
                last_seen TEXT
            )
        ''')
        try: cursor.execute('ALTER TABLE users ADD COLUMN avatar TEXT')
        except: pass
        try: cursor.execute('ALTER TABLE users ADD COLUMN last_seen TEXT')
        except: pass
        try: cursor.execute('ALTER TABLE users ADD COLUMN created_at TEXT')
        except: pass
        try: cursor.execute('ALTER TABLE users ADD COLUMN bio TEXT')
        except: pass
        try: cursor.execute('ALTER TABLE users ADD COLUMN tag TEXT')
        except: pass
        try: cursor.execute('ALTER TABLE users ADD COLUMN phone TEXT')
        except: pass
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone) WHERE phone IS NOT NULL AND phone != ''")
        try:
            cursor.execute("UPDATE users SET tag = '@' || username WHERE tag IS NULL OR trim(tag) = ''")
        except: pass
        try:
            cursor.execute("UPDATE users SET created_at = datetime('now') WHERE created_at IS NULL")
        except: pass

        # Таблица групповых комнат
        cursor.execute('''CREATE TABLE IF NOT EXISTS chat_rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            name TEXT, 
            room_id TEXT UNIQUE, 
            admin_username TEXT, 
            created_at TEXT,
            pinned_message_id INTEGER DEFAULT NULL
        )''')
        try: cursor.execute('ALTER TABLE chat_rooms ADD COLUMN pinned_message_id INTEGER DEFAULT NULL')
        except: pass
        try: cursor.execute('ALTER TABLE chat_rooms ADD COLUMN avatar_url TEXT')
        except: pass
        try: cursor.execute('ALTER TABLE chat_rooms ADD COLUMN disappearing_seconds INTEGER NOT NULL DEFAULT 0')
        except: pass
        try: cursor.execute('ALTER TABLE chat_rooms ADD COLUMN permissions_json TEXT NOT NULL DEFAULT \'{}\'')
        except: pass

        # Таблица участников групп
        cursor.execute('''CREATE TABLE IF NOT EXISTS room_members (
            room_id TEXT, 
            username TEXT, 
            is_pinned INTEGER DEFAULT 0,
            pinned_order INTEGER DEFAULT 0,
            muted INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0,
            PRIMARY KEY (room_id, username), 
            FOREIGN KEY (room_id) REFERENCES chat_rooms(room_id)
        )''')
        try: cursor.execute('ALTER TABLE room_members ADD COLUMN is_pinned INTEGER DEFAULT 0')
        except: pass
        try: cursor.execute('ALTER TABLE room_members ADD COLUMN pinned_order INTEGER DEFAULT 0')
        except: pass
        try: cursor.execute('ALTER TABLE room_members ADD COLUMN muted INTEGER DEFAULT 0')
        except: pass
        try: cursor.execute('ALTER TABLE room_members ADD COLUMN archived INTEGER DEFAULT 0')
        except: pass

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                room_id TEXT NOT NULL,
                chat_type TEXT NOT NULL DEFAULT 'group',
                draft_text TEXT DEFAULT '',
                updated_at TEXT NOT NULL,
                UNIQUE(username, room_id, chat_type)
            )
        ''')

        # Таблица новостей
        cursor.execute('CREATE TABLE IF NOT EXISTS news (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, content TEXT, author_username TEXT, image_path TEXT DEFAULT NULL, link_preview TEXT DEFAULT NULL, created_at TEXT)')
        try:
            cursor.execute('ALTER TABLE news ADD COLUMN image_path TEXT DEFAULT NULL')
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute('ALTER TABLE news ADD COLUMN created_at TEXT')
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute('ALTER TABLE news ADD COLUMN link_preview TEXT DEFAULT NULL')
        except sqlite3.OperationalError:
            pass

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS news_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id INTEGER NOT NULL,
                user_username TEXT NOT NULL,
                parent_id INTEGER DEFAULT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(news_id) REFERENCES news(id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS news_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                view_date TEXT NOT NULL,
                UNIQUE(news_id, user_id, view_date)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_username TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                image_path TEXT DEFAULT NULL,
                link_preview TEXT DEFAULT NULL,
                scheduled_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS recommended_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT DEFAULT '',
                followers INTEGER DEFAULT 0,
                growth_last_week INTEGER DEFAULT 0,
                accent TEXT DEFAULT '#2A9D8F',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS call_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caller_username TEXT NOT NULL,
                callee_username TEXT NOT NULL,
                room_token TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'answered',
                direction TEXT NOT NULL DEFAULT 'outgoing',
                call_type TEXT NOT NULL DEFAULT 'audio',
                started_at TEXT NOT NULL,
                duration_seconds INTEGER DEFAULT 0,
                data_usage_mb REAL DEFAULT 0,
                missed_reason TEXT DEFAULT '',
                group_call INTEGER DEFAULT 0,
                group_name TEXT DEFAULT '',
                participants_count INTEGER DEFAULT 1,
                metadata TEXT DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        seed_channels = [
            ('Хакерская среда', 'Технологии', 'Подкасты и разборы новых технологий', 42000, 184, '#2A9D8F'),
            ('Инженерный код', 'Технологии', 'Code review, AI и серверная архитектура', 31000, 166, '#2A9D8F'),
            ('Микроэлектроника', 'Технологии', 'Чипы, устройства и железо', 26000, 144, '#2A9D8F'),
            ('Новый день', 'Новости', 'Главные события и аналитика', 54000, 221, '#3B82F6'),
            ('Городские вести', 'Новости', 'Жизнь, экономика, политика, города', 47000, 189, '#3B82F6'),
            ('Краткий обзор', 'Новости', 'Свежий дайджест за день', 28000, 152, '#3B82F6'),
            ('Футбольный эфир', 'Спорт', 'Лига, новости и матчи', 39000, 176, '#F59E0B'),
            ('Спорт-микс', 'Спорт', 'Обзоры, топ-матчи и аналитика', 33000, 168, '#F59E0B'),
            ('Финишная прямая', 'Спорт', 'Быстрые новости и результаты', 24000, 143, '#F59E0B'),
            ('Поднятый смех', 'Юмор', 'Смешные истории и мемы', 46000, 198, '#EC4899'),
            ('Дежурный анекдот', 'Юмор', 'Коротко и с улыбкой', 30000, 171, '#EC4899'),
            ('Лайк-студия', 'Юмор', 'Реакции, ржач и челленджи', 22000, 159, '#EC4899'),
        ]
        for name, category, description, followers, growth_last_week, accent in seed_channels:
            exists = cursor.execute("SELECT 1 FROM recommended_channels WHERE name = ? AND category = ?", (name, category)).fetchone()
            if not exists:
                cursor.execute(
                    "INSERT INTO recommended_channels (name, category, description, followers, growth_last_week, accent) VALUES (?, ?, ?, ?, ?, ?)",
                    (name, category, description, followers, growth_last_week, accent)
                )

        # Таблица сообщений группового чата
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT,
                username TEXT,
                text TEXT,
                timestamp TEXT,
                reply_to_id INTEGER DEFAULT NULL,
                forwarded_from TEXT DEFAULT NULL,
                is_forwarded INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (room_id) REFERENCES chat_rooms(room_id)
            )
        ''')
        try: cursor.execute('ALTER TABLE chat_messages ADD COLUMN forwarded_from TEXT DEFAULT NULL')
        except: pass
        try: cursor.execute('ALTER TABLE chat_messages ADD COLUMN is_forwarded INTEGER NOT NULL DEFAULT 0')
        except: pass

        # Таблица личных сообщений
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS private_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT,
                receiver TEXT,
                text TEXT,
                timestamp TEXT,
                is_read INTEGER DEFAULT 0,
                reply_to_id INTEGER DEFAULT NULL,
                forwarded_from TEXT DEFAULT NULL,
                is_forwarded INTEGER NOT NULL DEFAULT 0
            )
        ''')
        try: cursor.execute('ALTER TABLE private_messages ADD COLUMN reply_to_id INTEGER DEFAULT NULL')
        except: pass
        try: cursor.execute('ALTER TABLE private_messages ADD COLUMN forwarded_from TEXT DEFAULT NULL')
        except: pass
        try: cursor.execute('ALTER TABLE private_messages ADD COLUMN is_forwarded INTEGER NOT NULL DEFAULT 0')
        except: pass

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS private_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user1 TEXT NOT NULL,
                user2 TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user1, user2)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pinned_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_type TEXT NOT NULL CHECK(chat_type IN ('group', 'private')),
                room_id TEXT,
                user1 TEXT,
                user2 TEXT,
                message_id INTEGER NOT NULL,
                pinned_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(chat_type, room_id, user1, user2, message_id)
            )
        ''')
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_group_pin_unique ON pinned_chat_messages(room_id, message_id) WHERE chat_type = 'group'")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_private_pin_unique ON pinned_chat_messages(user1, user2, message_id) WHERE chat_type = 'private'")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_type TEXT,
                room_id TEXT,
                sender TEXT,
                receiver TEXT,
                text TEXT,
                filename TEXT,
                is_file INTEGER DEFAULT 0,
                is_image INTEGER DEFAULT 0,
                is_video INTEGER DEFAULT 0,
                is_audio INTEGER DEFAULT 0,
                scheduled_at TEXT,
                reply_to_id INTEGER DEFAULT NULL,
                status TEXT DEFAULT 'pending'
            )
        ''')

        # --- НОВОЕ: Таблица реакций ---
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER,       -- ID сообщения (группового или личного)
                message_type TEXT,        -- 'group' или 'private'
                room_id TEXT,             -- Для групповых чатов
                user_username TEXT,       -- Кто поставил реакцию
                emoji TEXT,               -- Эмодзи реакции
                UNIQUE(message_id, message_type, user_username, emoji) -- Нельзя дублировать реакцию одного человека
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_delivery_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                chat_type TEXT NOT NULL CHECK(chat_type IN ('private', 'group')),
                room_id TEXT,
                sender TEXT NOT NULL,
                recipient TEXT,
                delivery_status TEXT NOT NULL DEFAULT 'sent' CHECK(delivery_status IN ('sent', 'delivered', 'read')),
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                delivered_at TEXT,
                read_at TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS group_message_read_receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                room_id TEXT NOT NULL,
                user_username TEXT NOT NULL,
                read_at TEXT NOT NULL,
                UNIQUE(message_id, room_id, user_username)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS secret_chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_a TEXT NOT NULL,
                user_b TEXT NOT NULL,
                ttl_seconds INTEGER NOT NULL DEFAULT 86400,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(user_a, user_b)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS secret_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                sender TEXT NOT NULL,
                receiver TEXT NOT NULL,
                ciphertext TEXT NOT NULL,
                nonce TEXT NOT NULL,
                ttl_seconds INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL,
                is_deleted INTEGER NOT NULL DEFAULT 0
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK(type IN ('group', 'supergroup', 'channel')),
                title TEXT NOT NULL,
                description TEXT,
                avatar TEXT,
                is_public INTEGER DEFAULT 0,
                username TEXT,
                invite_link TEXT,
                slow_mode_delay INTEGER DEFAULT 0,
                room_id TEXT UNIQUE,
                created_by TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chat_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_username TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('owner', 'admin', 'member')),
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_message_at TEXT,
                UNIQUE(chat_id, user_username)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS admin_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_username TEXT NOT NULL,
                can_delete_messages INTEGER DEFAULT 0,
                can_ban_users INTEGER DEFAULT 0,
                can_change_info INTEGER DEFAULT 0,
                can_invite_users INTEGER DEFAULT 0,
                can_post_messages INTEGER DEFAULT 0,
                UNIQUE(chat_id, user_username)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_statuses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author_username TEXT NOT NULL,
                status_type TEXT NOT NULL CHECK(status_type IN ('text', 'image', 'video')),
                text TEXT DEFAULT '',
                media_url TEXT DEFAULT '',
                caption TEXT DEFAULT '',
                background TEXT DEFAULT '',
                font_family TEXT DEFAULT 'system',
                link TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_status_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status_id INTEGER NOT NULL,
                viewer_username TEXT NOT NULL,
                viewed_at TEXT NOT NULL,
                UNIQUE(status_id, viewer_username)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_status_replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status_id INTEGER NOT NULL,
                sender_username TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_status_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status_id INTEGER NOT NULL,
                sender_username TEXT NOT NULL,
                reaction TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(status_id, sender_username, reaction)
            )
        ''')

        cursor.execute("DELETE FROM user_statuses WHERE expires_at <= datetime('now')")
        cursor.execute("DELETE FROM user_status_views WHERE status_id IN (SELECT id FROM user_statuses WHERE expires_at <= datetime('now'))")
        cursor.execute("DELETE FROM user_status_replies WHERE status_id IN (SELECT id FROM user_statuses WHERE expires_at <= datetime('now'))")
        cursor.execute("DELETE FROM user_status_reactions WHERE status_id IN (SELECT id FROM user_statuses WHERE expires_at <= datetime('now'))")

        conn.commit()
        print("База данных успешно инициализирована!")

init_db()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)


def cleanup_expired_statuses():
    with get_db_connection() as conn:
        conn.execute("DELETE FROM user_status_reactions WHERE status_id IN (SELECT id FROM user_statuses WHERE expires_at <= datetime('now'))")
        conn.execute("DELETE FROM user_status_replies WHERE status_id IN (SELECT id FROM user_statuses WHERE expires_at <= datetime('now'))")
        conn.execute("DELETE FROM user_status_views WHERE status_id IN (SELECT id FROM user_statuses WHERE expires_at <= datetime('now'))")
        conn.execute("DELETE FROM user_statuses WHERE expires_at <= datetime('now')")
        conn.commit()


def update_last_seen(username):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        conn.execute("UPDATE users SET last_seen = ? WHERE username = ?", (now, username))
        conn.commit()

def get_current_user(request: Request):
    username = request.cookies.get("username")
    if not username:
        return None
    with get_db_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(user) if user else None

def normalize_phone_number(phone: str) -> Optional[str]:
    raw = (phone or '').strip()
    digits = re.sub(r'\D', '', raw)
    if raw.startswith('00'):
        raw = '+' + raw[2:]
        digits = re.sub(r'\D', '', raw)
    if raw.startswith('+'):
        normalized = '+' + digits
    elif len(digits) == 11 and digits.startswith('8'):
        normalized = '+7' + digits[1:]
    elif len(digits) == 11 and digits.startswith('7'):
        normalized = '+' + digits
    elif len(digits) == 10:
        normalized = '+7' + digits
    else:
        return None
    return normalized if re.fullmatch(r'\+[1-9]\d{7,14}', normalized) else None

def generate_room_id():
    return secrets.token_hex(4).upper()


def generate_invite_link(chat_type: str = "supergroup"):
    token = secrets.token_urlsafe(12)
    return f"https://durable.chat/{chat_type}/{token}"


def generate_call_token():
    return secrets.token_urlsafe(10)


def build_missed_call_push_payload(caller_username: str, target_username: str):
    return {
        "title": "Пропущенный звонок",
        "text": f"{caller_username} позвонил вам",
        "action": "callback",
        "target_username": target_username,
        "deep_link": f"durable://call?target={target_username}&caller={caller_username}",
        "button_label": "Перезвонить"
    }


def get_call_codec_profile(device_supports_hardware_h264: bool = False):
    return {
        "audio": {
            "preferred": "opus",
            "supported": ["opus", "pcmu", "pcma"],
            "bitrate": {"min_kbps": 6, "max_kbps": 510},
            "dynamic_bitrate": True,
        },
        "video": {
            "preferred": "h264" if device_supports_hardware_h264 else "vp8",
            "supported": ["h264", "vp8"],
            "fallback": ["vp8", "h264"] if not device_supports_hardware_h264 else ["h264", "vp8"],
        }
    }


def get_chat_by_room_id(room_id: str):
    with get_db_connection() as conn:
        return conn.execute("SELECT * FROM chats WHERE room_id = ?", (room_id,)).fetchone()


def can_user_post_in_chat(room_id: str, username: str):
    chat = get_chat_by_room_id(room_id)
    if chat is None:
        return True, ""

    with get_db_connection() as conn:
        membership = conn.execute(
            "SELECT role FROM chat_members WHERE chat_id = ? AND user_username = ?",
            (chat['id'], username)
        ).fetchone()
        if membership is None:
            return False, "Вы не участник сообщества"

        if chat['type'] == 'channel':
            if membership['role'] in ('owner', 'admin'):
                perm = conn.execute(
                    "SELECT can_post_messages FROM admin_permissions WHERE chat_id = ? AND user_username = ?",
                    (chat['id'], username)
                ).fetchone()
                if perm is None or bool(perm['can_post_messages']):
                    return True, ""
            return False, "Канал доступен только администраторам"

    return True, ""


def create_delivery_record(message_id: int, chat_type: str, sender: str, recipient: Optional[str] = None, room_id: Optional[str] = None):
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO message_delivery_status (message_id, chat_type, room_id, sender, recipient, delivery_status, created_at)
            VALUES (?, ?, ?, ?, ?, 'sent', datetime('now'))
            """,
            (message_id, chat_type, room_id, sender, recipient)
        )
        conn.commit()


def update_delivery_record(message_id: int, chat_type: str, sender: str, recipient: Optional[str] = None, room_id: Optional[str] = None, status: str = 'delivered'):
    with get_db_connection() as conn:
        if chat_type == 'private':
            conn.execute(
                """
                UPDATE message_delivery_status
                SET delivery_status = ?, delivered_at = COALESCE(delivered_at, datetime('now')),
                    read_at = CASE WHEN ? = 'read' THEN datetime('now') ELSE read_at END
                WHERE message_id = ? AND sender = ? AND recipient = ?
                """,
                (status, status, message_id, sender, recipient)
            )
        else:
            conn.execute(
                """
                UPDATE message_delivery_status
                SET delivery_status = ?, delivered_at = COALESCE(delivered_at, datetime('now')),
                    read_at = CASE WHEN ? = 'read' THEN datetime('now') ELSE read_at END
                WHERE message_id = ? AND sender = ? AND room_id = ?
                """,
                (status, status, message_id, sender, room_id)
            )
        conn.commit()


def mark_group_message_read(message_id: int, room_id: str, username: str):
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO group_message_read_receipts (message_id, room_id, user_username, read_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (message_id, room_id, username)
        )
        conn.commit()


def get_group_message_delivery_status(message_id: int, room_id: str):
    with get_db_connection() as conn:
        total_members = conn.execute(
            "SELECT COUNT(*) AS cnt FROM room_members WHERE room_id = ?",
            (room_id,)
        ).fetchone()['cnt']

        read_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM group_message_read_receipts WHERE message_id = ? AND room_id = ?",
            (message_id, room_id)
        ).fetchone()['cnt']

        if total_members <= 0:
            return 'sent'
        if read_count >= total_members:
            return 'read'
        if read_count > 0:
            return 'delivered'
        return 'sent'


def get_private_message_delivery_status(message_id: int, sender: str, recipient: str):
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT delivery_status
            FROM message_delivery_status
            WHERE message_id = ? AND sender = ? AND recipient = ? AND chat_type = 'private'
            LIMIT 1
            """,
            (message_id, sender, recipient)
        ).fetchone()
        return row['delivery_status'] if row else 'sent'


def check_slow_mode(room_id: str, username: str, delay_seconds: int = 0) -> bool:
    if delay_seconds <= 0:
        return True

    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT last_message_at FROM chat_members WHERE chat_id = (SELECT id FROM chats WHERE room_id = ?) AND user_username = ?",
            (room_id, username)
        ).fetchone()

    if row is None or row['last_message_at'] is None:
        return True

    try:
        last_time = datetime.strptime(row['last_message_at'], "%Y-%m-%d %H:%M:%S")
        elapsed = (datetime.now() - last_time).total_seconds()
        return elapsed >= delay_seconds
    except Exception:
        return True


def update_chat_member_last_message(chat_id: int, username: str):
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE chat_members SET last_message_at = datetime('now') WHERE chat_id = ? AND user_username = ?",
            (chat_id, username)
        )
        conn.commit()


def slow_mode_guard(room_id_param: str = "room_id"):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            request = kwargs.get('request')
            if request is None:
                for arg in args:
                    if isinstance(arg, Request):
                        request = arg
                        break
            if request is None:
                return await func(*args, **kwargs)

            user = get_current_user(request)
            if not user:
                raise HTTPException(status_code=403, detail="Not authenticated")

            room_id = kwargs.get(room_id_param)
            if room_id is None:
                room_id = request.path_params.get(room_id_param)

            if room_id:
                with get_db_connection() as conn:
                    chat = conn.execute(
                        "SELECT slow_mode_delay FROM chats WHERE room_id = ?",
                        (room_id,)
                    ).fetchone()
                if chat and not check_slow_mode(room_id, user['username'], int(chat['slow_mode_delay'] or 0)):
                    raise HTTPException(status_code=429, detail="Slow mode active")

            return await func(*args, **kwargs)
        return wrapper
    return decorator

# --- МАРШРУТЫ ДЛЯ АВАТАРОК ---
@app.post("/upload-avatar")
async def upload_avatar(request: Request, file: UploadFile = File(...)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    if not file.content_type.startswith("image/"):
        return RedirectResponse(url="/user-info?msg=error", status_code=303)
    file_extension = file.filename.split(".")[-1]
    new_filename = f"{user['username']}.{file_extension}"
    file_path = os.path.join(UPLOAD_DIR, new_filename)
    with open(file_path, "wb") as buffer:
        buffer.write(await file.read())
    with get_db_connection() as conn:
        conn.execute("UPDATE users SET avatar = ? WHERE username = ?", (f"/uploads/{new_filename}", user['username']))
        conn.commit()
    return RedirectResponse(url="/user-info", status_code=303)

# --- ЗАГРУЗКА ФАЙЛОВ ДЛЯ ЧАТА ---
@app.post("/upload-chat-file")
async def upload_chat_file(request: Request, file: UploadFile = File(...)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    # Создаем папку uploads, если её нет
    if not os.path.exists(UPLOAD_DIR):
        os.makedirs(UPLOAD_DIR)

    file_extension = file.filename.split(".")[-1].lower() if "." in file.filename else ""
    timestamp = int(datetime.now().timestamp())
    original_filename = file.filename

    new_filename = f"file_{user['username']}_{timestamp}.{file_extension}" if file_extension else f"file_{user['username']}_{timestamp}"
    file_path = os.path.join(UPLOAD_DIR, new_filename)

    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        print(f"Error saving file: {e}")
        return {"status": "error", "message": "Ошибка сохранения файла на сервере"}

    content_type = (file.content_type or '').lower()
    image_extensions = ['jpg', 'jpeg', 'png', 'gif', 'webp']
    video_extensions = ['mp4', 'webm', 'mov', 'avi']
    audio_extensions = ['mp3', 'wav', 'ogg', 'webm', 'm4a', 'aac']

    is_image = content_type.startswith('image/') or file_extension in image_extensions
    is_video = content_type.startswith('video/') or (file_extension in video_extensions and not content_type.startswith('audio/'))
    is_audio = content_type.startswith('audio/') or (file_extension in audio_extensions and not is_video)

    # ВАЖНО: Возвращаем путь, начинающийся с /uploads/, чтобы FastAPI мог его отдать
    return {
        "status": "ok",
        "url": f"/uploads/{new_filename}",
        "filename": original_filename,
        "is_image": is_image,
        "is_video": is_video,
        "is_audio": is_audio
    }

# --- НОВЫЙ ЭНДПОИНТ ДЛЯ МЕДИАФАЙЛОВ ---
@app.get("/media/{file_path:path}")
async def get_media_file(file_path: str, media_type: Optional[str] = None):
    """Прямой доступ к медиафайлам для просмотра"""
    full_path = os.path.join(UPLOAD_DIR, file_path)
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="File not found")

    # Определяем MIME тип
    ext = file_path.split('.')[-1].lower()
    media_types = {
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
        'gif': 'image/gif',
        'webp': 'image/webp',
        'mp4': 'video/mp4',
        'webm': 'video/webm',
        'mov': 'video/quicktime',
        'mp3': 'audio/mpeg',
        'wav': 'audio/wav',
        'ogg': 'audio/ogg',
        'm4a': 'audio/mp4',
        'aac': 'audio/aac'
    }

    if media_type == 'audio' and ext == 'webm':
        content_type = 'audio/webm'
    elif media_type == 'audio' and ext == 'm4a':
        content_type = 'audio/mp4'
    else:
        content_type = media_types.get(ext, "application/octet-stream")
    return StreamingResponse(open(full_path, "rb"), media_type=content_type)


# --- МЕНЕДЖЕР ПОДКЛЮЧЕНИЙ ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}
        self.private_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, room_id: str, username: str):
        await websocket.accept()
        if room_id not in self.active_connections:
            self.active_connections[room_id] = []
        self.active_connections[room_id].append(websocket)
        add_online_user(room_id, username)

    async def connect_private(self, websocket: WebSocket, peer_username: str, my_username: str):
        await websocket.accept()
        chat_key = ":".join(sorted([my_username, peer_username]))
        if chat_key not in self.private_connections:
            self.private_connections[chat_key] = []
        self.private_connections[chat_key].append(websocket)

    def disconnect(self, websocket: WebSocket, room_id: str, username: str):
        if room_id in self.active_connections:
            if websocket in self.active_connections[room_id]:
                self.active_connections[room_id].remove(websocket)
            if not self.active_connections[room_id]:
                del self.active_connections[room_id]
        remove_online_user(room_id, username)

    def disconnect_private(self, websocket: WebSocket, peer_username: str, my_username: str):
        chat_key = ":".join(sorted([my_username, peer_username]))
        if chat_key in self.private_connections:
            if websocket in self.private_connections[chat_key]:
                self.private_connections[chat_key].remove(websocket)
            if not self.private_connections[chat_key]:
                del self.private_connections[chat_key]

    async def broadcast_to_room(self, message: str, room_id: str):
        if room_id in self.active_connections:
            for connection in self.active_connections[room_id]:
                await connection.send_text(message)

    async def send_to_private_chat(self, message: str, user1: str, user2: str):
        chat_key = ":".join(sorted([user1, user2]))
        if chat_key in self.private_connections:
            for connection in self.private_connections[chat_key]:
                await connection.send_text(message)

manager = ConnectionManager()
scheduled_task = None
scheduled_news_task = None

# --- РЕАКЦИИ НА СООБЩЕНИЯ ---
@app.post("/add-reaction")
async def add_reaction(request: Request, message_id: int = Form(...), message_type: str = Form(...), emoji: str = Form(...), room_id: Optional[str] = Form(None)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    # 1. Сначала работаем с базой данных внутри контекстного менеджера
    with get_db_connection() as conn:
        try:
            # Проверяем, существует ли уже такая реакция у этого пользователя
            existing = conn.execute(
                "SELECT id FROM message_reactions WHERE message_id = ? AND message_type = ? AND user_username = ? AND emoji = ?",
                (message_id, message_type, user['username'], emoji)
            ).fetchone()

            if existing:
                # Если есть - удаляем (toggle эффект)
                conn.execute(
                    "DELETE FROM message_reactions WHERE id = ?",
                    (existing['id'],)
                )
            else:
                # Если нет - добавляем
                conn.execute(
                    "INSERT INTO message_reactions (message_id, message_type, room_id, user_username, emoji) VALUES (?, ?, ?, ?, ?)",
                    (message_id, message_type, room_id, user['username'], emoji)
                )

            conn.commit()

            # Получаем актуальный список реакций для этого сообщения
            reactions = conn.execute(
                "SELECT emoji, COUNT(*) as count FROM message_reactions WHERE message_id = ? AND message_type = ? GROUP BY emoji",
                (message_id, message_type)
            ).fetchall()

            reaction_data = [{"emoji": r['emoji'], "count": r['count']} for r in reactions]

            # Если это личное сообщение, нам нужны sender и receiver для отправки через WS
            # Делаем это ВНУТРИ блока with, пока соединение открыто!
            participants_to_notify = None
            if message_type == 'private':
                msg_info = conn.execute("SELECT sender, receiver FROM private_messages WHERE id = ?", (message_id,)).fetchone()
                if msg_info:
                    participants_to_notify = sorted([msg_info['sender'], msg_info['receiver']])

        except Exception as e:
            print(f"Error adding reaction: {e}")
            return {"status": "error", "message": str(e)}

    # 2. Теперь отправляем обновления через WebSocket (соединение с БД уже закрыто, но данные у нас есть)
    if message_type == 'group' and room_id:
        await manager.broadcast_to_room(json.dumps({
            "type": "reaction_update",
            "message_id": message_id,
            "reactions": reaction_data
        }), room_id)
    elif message_type == 'private' and participants_to_notify:
        if len(participants_to_notify) == 2:
            await manager.send_to_private_chat(json.dumps({
                "type": "reaction_update",
                "message_id": message_id,
                "reactions": reaction_data
            }), participants_to_notify[0], participants_to_notify[1])

    return {"status": "ok", "reactions": reaction_data}


@app.post("/api/create-community")
async def create_community(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    try:
        payload = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON"}

    chat_type = payload.get("type")
    title = (payload.get("title") or "").strip()
    description = (payload.get("description") or "").strip()
    is_public = bool(payload.get("is_public"))
    username = (payload.get("username") or "").strip()
    slow_mode = int(payload.get("slow_mode_delay") or 0)
    invite_link = payload.get("invite_link") or generate_invite_link(chat_type or "supergroup")

    if chat_type not in ["group", "supergroup", "channel"]:
        return {"status": "error", "message": "Недопустимый тип сообщества"}
    if not title:
        return {"status": "error", "message": "Название обязательно"}

    if chat_type in ["supergroup", "channel"] and is_public and not username:
        return {"status": "error", "message": "Для публичной группы потребуется username"}

    room_id = generate_room_id()
    with get_db_connection() as conn:
        existing = conn.execute("SELECT id FROM chats WHERE username = ? AND type IN ('supergroup','channel')", (username,)).fetchone() if username else None
        if existing:
            return {"status": "error", "message": "Этот username уже занят"}

        cursor = conn.execute(
            """
            INSERT INTO chats (type, title, description, avatar, is_public, username, invite_link, slow_mode_delay, room_id, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (chat_type, title, description, payload.get("avatar"), 1 if is_public else 0, username, invite_link, slow_mode, room_id, user['username'])
        )
        chat_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO chat_members (chat_id, user_username, role, last_message_at) VALUES (?, ?, 'owner', datetime('now'))",
            (chat_id, user['username'])
        )
        conn.execute(
            "INSERT INTO admin_permissions (chat_id, user_username, can_delete_messages, can_ban_users, can_change_info, can_invite_users, can_post_messages) VALUES (?, ?, 1, 1, 1, 1, 1)",
            (chat_id, user['username'])
        )
        conn.commit()

    return {
        "status": "ok",
        "chat_id": room_id,
        "room_id": room_id,
        "type": chat_type,
        "title": title,
        "is_public": is_public,
        "username": username,
        "invite_link": invite_link,
        "slow_mode_delay": slow_mode,
        "message": "Сообщество создано"
    }


@app.get("/api/check-username")
async def api_check_username(username: str):
    username = (username or "").strip()
    if not username:
        return {"available": False, "message": "Username обязателен"}

    with get_db_connection() as conn:
        taken = conn.execute(
            "SELECT 1 FROM users WHERE username = ? UNION SELECT 1 FROM chats WHERE username = ? AND type IN ('supergroup','channel') LIMIT 1",
            (username, username)
        ).fetchone()

    if taken:
        return {"available": False, "message": "Username уже занят"}
    return {"available": True, "message": "Username доступен"}


@app.get('/reaction-users')
async def reaction_users(message_id: int, emoji: str):
    with get_db_connection() as conn:
        rows = conn.execute("SELECT user_username FROM message_reactions WHERE message_id = ? AND emoji = ? ORDER BY id DESC", (message_id, emoji)).fetchall()
        users = []
        for r in rows:
            u = conn.execute("SELECT username, avatar FROM users WHERE username = ?", (r['user_username'],)).fetchone()
            if u:
                users.append({"username": u['username'], "avatar": u['avatar']})
            else:
                users.append({"username": r['user_username'], "avatar": None})
    return {"status": "ok", "users": users}

@app.post("/schedule-message")
async def schedule_message(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    try:
        body = await request.json()
    except:
        return {"status": "error", "message": "Ошибка формата данных"}

    message_type = body.get("message_type")
    scheduled_at = body.get("scheduled_at")
    text = body.get("text", "")
    filename = body.get("filename", "")
    is_file = bool(body.get("is_file"))
    is_image = bool(body.get("is_image"))
    is_video = bool(body.get("is_video"))
    is_audio = bool(body.get("is_audio"))
    is_forwarded = bool(body.get("is_forwarded", True))
    forwarded_from = (str(body.get("forwarded_from") or "").strip() if body.get("include_author", True) else "") or None
    reply_to_id = body.get("reply_to_id")
    room_id = body.get("room_id")
    receiver = body.get("receiver")

    if message_type not in ["group", "private"]:
        return {"status": "error", "message": "Неверный тип сообщения"}
    if not scheduled_at:
        return {"status": "error", "message": "Укажите время отправки"}

    try:
        scheduled_dt = datetime.strptime(scheduled_at, "%Y-%m-%dT%H:%M")
    except ValueError:
        return {"status": "error", "message": "Неверный формат времени"}

    if scheduled_dt <= datetime.now():
        return {"status": "error", "message": "Время должно быть позже текущего"}

    if message_type == "group":
        if not room_id:
            return {"status": "error", "message": "Нет ID группы"}
        with get_db_connection() as conn:
            member = conn.execute("SELECT 1 FROM room_members WHERE room_id = ? AND username = ?", (room_id, user['username'])).fetchone()
            if not member:
                return {"status": "error", "message": "Вы не состоите в этой группе"}
    else:
        if not receiver:
            return {"status": "error", "message": "Не указан получатель"}

    db_text = text
    if is_file or is_image or is_video or is_audio:
        db_text = json.dumps({
            "type": "file",
            "url": text,
            "name": filename,
            "is_image": is_image,
            "is_video": is_video,
            "is_audio": is_audio
        })

    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO scheduled_messages (message_type, room_id, sender, receiver, text, filename, is_file, is_image, is_video, is_audio, scheduled_at, reply_to_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_type, room_id, user['username'], receiver, db_text, filename, int(is_file), int(is_image), int(is_video), int(is_audio), scheduled_dt.strftime("%Y-%m-%d %H:%M"), reply_to_id)
        )
        conn.commit()

    return {"status": "ok", "message": "Сообщение запланировано"}

async def process_scheduled_messages():
    while True:
        with get_db_connection() as conn:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            due_messages = conn.execute(
                "SELECT * FROM scheduled_messages WHERE status = 'pending' AND scheduled_at <= ? ORDER BY scheduled_at ASC",
                (now,)
            ).fetchall()

            for msg in due_messages:
                if msg['message_type'] == 'group' and msg['room_id']:
                    timestamp = datetime.now().strftime("%H:%M")
                    conn.execute("INSERT INTO chat_messages (room_id, username, text, timestamp, reply_to_id) VALUES (?, ?, ?, ?, ?)",
                                 (msg['room_id'], msg['sender'], msg['text'], timestamp, msg['reply_to_id']))
                    conn.commit()
                    last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    await manager.broadcast_to_room(json.dumps({
                        "type": "message",
                        "id": last_id,
                        "username": msg['sender'],
                        "text": msg['text'],
                        "timestamp": timestamp,
                        "reply_to_id": msg['reply_to_id'],
                        "is_file": bool(msg['is_file']),
                        "is_image": bool(msg['is_image']),
                        "is_video": bool(msg['is_video']),
                        "is_audio": bool(msg['is_audio']),
                        "filename": msg['filename']
                    }), msg['room_id'])
                elif msg['message_type'] == 'private' and msg['receiver']:
                    timestamp = datetime.now().strftime("%H:%M")
                    conn.execute("INSERT INTO private_messages (sender, receiver, text, timestamp) VALUES (?, ?, ?, ?)",
                                 (msg['sender'], msg['receiver'], msg['text'], timestamp))
                    conn.commit()
                    last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    await manager.send_to_private_chat(json.dumps({
                        "type": "private_message",
                        "sender": msg['sender'],
                        "receiver": msg['receiver'],
                        "text": msg['text'],
                        "timestamp": timestamp,
                        "is_mine": False,
                        "id": last_id
                    }), msg['sender'], msg['receiver'])
                conn.execute("UPDATE scheduled_messages SET status = 'sent' WHERE id = ?", (msg['id'],))
                conn.commit()

        await asyncio.sleep(5)

async def process_scheduled_news():
    while True:
        with get_db_connection() as conn:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            due_news = conn.execute(
                "SELECT * FROM scheduled_news WHERE status = 'pending' AND scheduled_at <= ? ORDER BY scheduled_at ASC",
                (now,)
            ).fetchall()
            for item in due_news:
                conn.execute(
                    "INSERT INTO news (title, content, author_username, image_path, link_preview, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (item['title'], item['content'], item['author_username'], item['image_path'], item['link_preview'], datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                )
                conn.execute("UPDATE scheduled_news SET status = 'published' WHERE id = ?", (item['id'],))
            conn.commit()
        await asyncio.sleep(60)

@app.on_event("startup")
async def start_schedule_loop():
    global scheduled_task, scheduled_news_task
    if scheduled_task is None:
        scheduled_task = asyncio.create_task(process_scheduled_messages())
    if scheduled_news_task is None:
        scheduled_news_task = asyncio.create_task(process_scheduled_news())

@app.on_event("shutdown")
async def stop_schedule_loop():
    global scheduled_task, scheduled_news_task
    if scheduled_task:
        scheduled_task.cancel()
        scheduled_task = None
    if scheduled_news_task:
        scheduled_news_task.cancel()
        scheduled_news_task = None

# --- ЭНДПОИНТЫ ДЛЯ ЛИЧНЫХ СООБЩЕНИЙ (РЕДАКТИРОВАНИЕ И УДАЛЕНИЕ) ---

@app.post("/edit-private-message")
async def edit_private_message(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    try:
        body = await request.json()
        msg_id = body.get("msg_id")
        new_text = body.get("text")
    except:
        return {"status": "error", "message": "Ошибка формата данных"}

    if not msg_id or new_text is None:
        return {"status": "error", "message": "Нет данных"}

    with get_db_connection() as conn:
        # Проверяем, что сообщение принадлежит пользователю
        msg = conn.execute("SELECT sender, receiver FROM private_messages WHERE id = ?", (msg_id,)).fetchone()

        if not msg:
            return {"status": "error", "message": "Сообщение не найдено"}

        # Редактировать может только отправитель
        if msg['sender'] != user['username']:
            return {"status": "error", "message": "Вы можете редактировать только свои сообщения"}

        conn.execute("UPDATE private_messages SET text = ? WHERE id = ?", (new_text, msg_id))
        conn.commit()

        # Отправляем обновление обоим участникам
        participants = sorted([msg['sender'], msg['receiver']])
        await manager.send_to_private_chat(json.dumps({
            "type": "edit_private_message",
            "id": msg_id,
            "text": new_text
        }), participants[0], participants[1])

    return {"status": "ok", "message": "Изменено"}


@app.post("/delete-private-message/{msg_id}")
async def delete_private_message(request: Request, msg_id: int):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    with get_db_connection() as conn:
        msg = conn.execute("SELECT sender, receiver FROM private_messages WHERE id = ?", (msg_id,)).fetchone()

        if not msg:
            return {"status": "error", "message": "Сообщение не найдено"}

        # Удалять может только отправитель
        if msg['sender'] != user['username']:
            return {"status": "error", "message": "Вы можете удалять только свои сообщения"}

        conn.execute("DELETE FROM private_messages WHERE id = ?", (msg_id,))
        conn.commit()

        # Отправляем команду на удаление обоим участникам
        participants = sorted([msg['sender'], msg['receiver']])
        await manager.send_to_private_chat(json.dumps({
            "type": "delete_private_message",
            "id": msg_id
        }), participants[0], participants[1])

    return {"status": "ok", "message": "Удалено"}


# --- WEBSOCKET ДЛЯ ГРУППОВЫХ ЧАТОВ ---
@app.websocket("/ws/chat/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await manager.connect(websocket, room_id, "unknown")
    current_user_name = None
    try:
        while True:
            data = await websocket.receive_text()
            message_data = json.loads(data)

            if message_data.get("action") == "typing":
                await manager.broadcast_to_room(json.dumps({"type": "user_typing", "username": message_data.get("username")}), room_id)
                continue

            if message_data.get("action") == "group_message_read":
                username = message_data.get("username")
                message_id = message_data.get("message_id")
                if username and message_id:
                    status = mark_group_message_read(message_id, room_id, username)
                    await manager.broadcast_to_room(json.dumps({
                        "type": "group_read_status",
                        "message_id": message_id,
                        "room_id": room_id,
                        "status": status,
                        "reader": username
                    }), room_id)
                continue

            if message_data.get("action") == "join":
                username = message_data.get("username")
                current_user_name = username
                remove_online_user(room_id, "unknown")
                add_online_user(room_id, username)
                update_last_seen(username)

                join_msg = {
                    "type": "system",
                    "event": "join",
                    "username": username,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "text": f"🔵 {username} присоединился."
                }
                await manager.broadcast_to_room(json.dumps(join_msg), room_id)
                await manager.broadcast_to_room(json.dumps({"type": "update_participants"}), room_id)

                with get_db_connection() as conn:
                    # --- ИСПРАВЛЕНИЕ: Отправка истории в формате JSON ---
                    messages = conn.execute("SELECT * FROM chat_messages WHERE room_id = ? ORDER BY id DESC LIMIT 50", (room_id,)).fetchall()

                    for msg in reversed(messages):
                        # Формируем объект данных так же, как для новых сообщений
                        msg_data = {
                            "type": "message",
                            "id": msg['id'],
                            "username": msg['username'],
                            "text": msg['text'],
                            "timestamp": msg['timestamp'],
                            "reply_to_id": msg['reply_to_id'],
                            "forwarded_from": msg['forwarded_from'],
                            "is_forwarded": bool(msg['is_forwarded']),
                            "delivery_status": get_group_message_delivery_status(msg['id'], room_id),
                            "chat_type": "group",
                            # Определяем тип контента для клиента
                            "is_image": False,
                            "is_video": False,
                            "is_audio": False,
                            "is_file": False,
                            "filename": ""
                        }
                        if msg['reply_to_id']:
                            original = conn.execute("SELECT username, text FROM chat_messages WHERE id = ?", (msg['reply_to_id'],)).fetchone()
                            if original:
                                reply_text = original['text']
                                try:
                                    attachment = json.loads(reply_text)
                                    if attachment.get('type') == 'file': reply_text = f"[Файл: {attachment.get('name') or 'Медиа'}]"
                                except (ValueError, AttributeError):
                                    pass
                                msg_data['reply_preview'] = {"id": msg['reply_to_id'], "username": original['username'], "text": reply_text[:100]}

                        # Проверяем содержимое текста
                        text_content = msg['text']
                        try:
                            parsed = json.loads(text_content)
                            if isinstance(parsed, dict) and parsed.get("type") == "file":
                                msg_data['is_file'] = True
                                msg_data['filename'] = parsed.get("name", "")
                                # Определяем тип файла по расширению в URL или имени
                                url_lower = parsed.get("url", "").lower()
                                name_lower = parsed.get("name", "").lower()
                                if any(ext in url_lower or ext in name_lower for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                                    msg_data['is_image'] = True
                                elif any(ext in url_lower or ext in name_lower for ext in ['.mp4', '.webm', '.mov', '.avi']):
                                    msg_data['is_video'] = True
                                elif any(ext in url_lower or ext in name_lower for ext in ['.mp3', '.wav', '.ogg']):
                                    msg_data['is_audio'] = True
                        except:
                            pass # Это обычный текст

                        await websocket.send_text(json.dumps(msg_data))

                    # Дальше идет код для реакций...
                    msg_ids = [m['id'] for m in messages]
                    if msg_ids:
                        placeholders = ','.join(['?'] * len(msg_ids))
                        react_data = conn.execute(
                            f"SELECT message_id, emoji, COUNT(*) as count FROM message_reactions WHERE message_id IN ({placeholders}) AND message_type = 'group' GROUP BY message_id, emoji",
                            msg_ids
                        ).fetchall()

                        # Группируем реакции по message_id
                        reactions_map = {}
                        for r in react_data:
                            if r['message_id'] not in reactions_map:
                                reactions_map[r['message_id']] = []
                            reactions_map[r['message_id']].append({"emoji": r['emoji'], "count": r['count']})

                        # Отправляем реакции клиенту
                        for mid, reacs in reactions_map.items():
                            await websocket.send_text(json.dumps({
                                "type": "reaction_init",
                                "message_id": mid,
                                "reactions": reacs
                            }))

                    room_info = conn.execute("SELECT pinned_message_id, admin_username FROM chat_rooms WHERE room_id = ?", (room_id,)).fetchone()
                    if room_info:
                        is_admin = "true" if room_info['admin_username'] == username else "false"
                        await websocket.send_text(json.dumps({"type": "admin_status", "is_admin": is_admin}))

                        if room_info['pinned_message_id']:
                            pinned_msg = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (room_info['pinned_message_id'],)).fetchone()
                            if pinned_msg:
                                await websocket.send_text(json.dumps({
                                    "type": "pinned_message_info",
                                    "data": {
                                        "id": pinned_msg['id'],
                                        "username": pinned_msg['username'],
                                        "text": pinned_msg['text'],
                                        "timestamp": pinned_msg['timestamp']
                                    }
                                }))
                continue

            if message_data.get("action") == "kick_user":
                target_user = message_data.get("target_user")
                admin_user = message_data.get("current_user")
                with get_db_connection() as conn:
                    room = conn.execute("SELECT admin_username FROM chat_rooms WHERE room_id = ?", (room_id,)).fetchone()
                    if room and room['admin_username'] == admin_user:
                        conn.execute("DELETE FROM room_members WHERE room_id = ? AND username = ?", (room_id, target_user))
                        conn.commit()
                        await manager.broadcast_to_room(f"<div class='system-msg'>🚫 <b>{target_user}</b> удален.</div>", room_id)
                        await manager.broadcast_to_room(json.dumps({"type": "update_participants"}), room_id)
                continue

            # Отправка сообщения в группу
            username = message_data.get("username") or current_user_name or "Аноним"
            text = message_data.get("text", "")
            is_file = message_data.get("is_file", False)
            is_image = message_data.get("is_image", False)
            is_video = message_data.get("is_video", False)
            is_audio = message_data.get("is_audio", False)
            filename = message_data.get("filename", "")
            reply_to_id = message_data.get("reply_to_id", None)
            temp_id = message_data.get("temp_id")

            chat_meta = get_chat_by_room_id(room_id)
            if chat_meta is not None:
                allowed, reason = can_user_post_in_chat(room_id, username)
                if not allowed:
                    await websocket.send_text(json.dumps({"type": "error", "message": reason}))
                    continue
                delay = int(chat_meta['slow_mode_delay'] or 0)
                if not check_slow_mode(room_id, username, delay):
                    await websocket.send_text(json.dumps({"type": "error", "message": "Slow mode активен"}))
                    continue

            update_last_seen(username)
            timestamp = datetime.now().strftime("%H:%M")

            if not text.strip() and not is_file: continue

            db_text = text
            if is_file or is_image or is_video or is_audio:
                db_text = json.dumps({
                    "type": "file",
                    "url": text,
                    "name": filename,
                    "is_image": is_image,
                    "is_video": is_video,
                    "is_audio": is_audio
                })

            with get_db_connection() as conn:
                conn.execute("INSERT INTO chat_messages (room_id, username, text, timestamp, reply_to_id) VALUES (?, ?, ?, ?, ?)",
                             (room_id, username, db_text, timestamp, reply_to_id))
                conn.commit()
                last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                chat_row = conn.execute("SELECT id FROM chats WHERE room_id = ?", (room_id,)).fetchone()
                if chat_row:
                    conn.execute(
                        "UPDATE chat_members SET last_message_at = datetime('now') WHERE chat_id = ? AND user_username = ?",
                        (chat_row['id'], username)
                    )
                    conn.commit()

                create_delivery_record(last_id, 'group', username, room_id=room_id)

                reply_preview = None
                if reply_to_id:
                    orig = conn.execute("SELECT username, text FROM chat_messages WHERE id = ?", (reply_to_id,)).fetchone()
                    if orig:
                        p_text = orig['text']
                        try:
                            p_json = json.loads(p_text)
                            if p_json.get('type') == 'file':
                                p_text = f"[Файл: {p_json.get('name', 'Media')}]"
                        except: pass
                        reply_preview = {"id": reply_to_id, "username": orig['username'], "text": p_text[:100]}

                new_msg = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (last_id,)).fetchone()

            initial_group_status = get_group_message_delivery_status(last_id, room_id)
            response_data = {
                "type": "message",
                "id": new_msg['id'],
                "username": new_msg['username'],
                "text": text if (is_file or is_image or is_video or is_audio) else new_msg['text'],
                "timestamp": new_msg['timestamp'],
                "is_file": is_file,
                "is_image": is_image,
                "is_video": is_video,
                "is_audio": is_audio,
                "filename": filename,
                "reply_to_id": reply_to_id,
                "reply_preview": reply_preview,
                "forwarded_from": new_msg['forwarded_from'],
                "is_forwarded": bool(new_msg['is_forwarded']),
                "temp_id": temp_id,
                "delivery_status": initial_group_status,
                "chat_type": "group"
            }

            await manager.broadcast_to_room(json.dumps(response_data), room_id)

    except WebSocketDisconnect:
        manager.disconnect(websocket, room_id, current_user_name)

# --- WEBSOCKET ДЛЯ ЛИЧНЫХ ЧАТОВ ---
@app.websocket("/ws/private/{peer_username}")
async def websocket_private_endpoint(websocket: WebSocket, peer_username: str):
    my_username = None
    try:
        await manager.connect_private(websocket, peer_username, "temp")

        auth_data = await websocket.receive_json()
        if auth_data.get("action") != "auth":
            await websocket.close()
            return

        my_username = auth_data.get("username")
        if not my_username:
            await websocket.close()
            return

        chat_key_temp = ":".join(sorted(["temp", peer_username]))
        if chat_key_temp in manager.private_connections:
            if websocket in manager.private_connections[chat_key_temp]:
                manager.private_connections[chat_key_temp].remove(websocket)
                if not manager.private_connections[chat_key_temp]:
                    del manager.private_connections[chat_key_temp]

        chat_key = ":".join(sorted([my_username, peer_username]))
        if chat_key not in manager.private_connections:
            manager.private_connections[chat_key] = []
        manager.private_connections[chat_key].append(websocket)

        with get_db_connection() as conn:
            history = conn.execute("""
                SELECT * FROM private_messages 
                WHERE (sender = ? AND receiver = ?) OR (sender = ? AND receiver = ?)
                ORDER BY id ASC
            """, (my_username, peer_username, peer_username, my_username)).fetchall()

            # Собираем ID сообщений для загрузки реакций
            msg_ids = [m['id'] for m in history]

            for msg in history:
                is_mine = msg['sender'] == my_username
                delivery_status = 'read' if bool(msg['is_read']) else ('sent' if is_mine else 'delivered')
                reply_preview = None
                if msg['reply_to_id']:
                    original = conn.execute("SELECT sender, text FROM private_messages WHERE id = ?", (msg['reply_to_id'],)).fetchone()
                    if original:
                        reply_text = original['text']
                        try:
                            attachment = json.loads(reply_text)
                            if attachment.get('type') == 'file': reply_text = f"[Файл: {attachment.get('name') or 'Медиа'}]"
                        except (ValueError, AttributeError):
                            pass
                        reply_preview = {"id": msg['reply_to_id'], "username": original['sender'], "text": reply_text[:100]}
                await websocket.send_text(json.dumps({
                    "type": "private_message",
                    "sender": msg['sender'],
                    "receiver": msg['receiver'],
                    "text": msg['text'],
                    "timestamp": msg['timestamp'],
                    "is_mine": is_mine,
                    "is_read": bool(msg['is_read']),
                    "id": msg['id'],
                    "reply_to_id": msg['reply_to_id'],
                    "reply_preview": reply_preview,
                    "forwarded_from": msg['forwarded_from'],
                    "is_forwarded": bool(msg['is_forwarded']),
                    "delivery_status": delivery_status,
                    "chat_type": "private"
                }))

            # Загружаем реакции для истории личных сообщений
            if msg_ids:
                placeholders = ','.join(['?'] * len(msg_ids))
                react_data = conn.execute(
                    f"SELECT message_id, emoji, COUNT(*) as count FROM message_reactions WHERE message_id IN ({placeholders}) AND message_type = 'private' GROUP BY message_id, emoji",
                    msg_ids
                ).fetchall()

                reactions_map = {}
                for r in react_data:
                    if r['message_id'] not in reactions_map:
                        reactions_map[r['message_id']] = []
                    reactions_map[r['message_id']].append({"emoji": r['emoji'], "count": r['count']})

                for mid, reacs in reactions_map.items():
                    await websocket.send_text(json.dumps({
                        "type": "reaction_init",
                        "message_id": mid,
                        "reactions": reacs
                    }))

        while True:
            data = await websocket.receive_json()

            if data.get("action") == "typing":
                await manager.send_to_private_chat(json.dumps({"type": "typing", "username": my_username}), my_username, peer_username)
                continue

            if data.get("type") == "send_private":
                text = data.get("text")
                temp_id = data.get("temp_id")
                reply_to_id = data.get("reply_to_id")
                if not text: continue

                timestamp = datetime.now().strftime("%H:%M")
                reply_preview = None

                with get_db_connection() as conn:
                    if reply_to_id:
                        original = conn.execute("SELECT sender, receiver, text FROM private_messages WHERE id = ?", (reply_to_id,)).fetchone()
                        if not original or {original['sender'], original['receiver']} != {my_username, peer_username}:
                            reply_to_id = None
                        else:
                            reply_text = original['text']
                            try:
                                attachment = json.loads(reply_text)
                                if attachment.get('type') == 'file': reply_text = f"[Файл: {attachment.get('name') or 'Медиа'}]"
                            except (ValueError, AttributeError):
                                pass
                            reply_preview = {"id": reply_to_id, "username": original['sender'], "text": reply_text[:100]}
                    conn.execute("INSERT INTO private_messages (sender, receiver, text, timestamp, reply_to_id) VALUES (?, ?, ?, ?, ?)",
                                 (my_username, peer_username, text, timestamp, reply_to_id))
                    conn.commit()
                    last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    create_delivery_record(last_id, 'private', my_username, peer_username)

                await manager.send_to_private_chat(json.dumps({
                    "type": "private_message",
                    "sender": my_username,
                    "receiver": peer_username,
                    "text": text,
                    "timestamp": timestamp,
                    "is_mine": True,
                    "is_read": False,
                    "id": last_id,
                    "reply_to_id": reply_to_id,
                    "reply_preview": reply_preview,
                    "forwarded_from": None,
                    "is_forwarded": False,
                    "temp_id": temp_id,
                    "delivery_status": 'sent',
                    "chat_type": 'private'
                }), my_username, peer_username)

                continue
            if data.get("type") == "private_read":
                message_id = data.get("message_id")
                if not message_id:
                    continue
                with get_db_connection() as conn:
                    msg = conn.execute("SELECT sender, receiver FROM private_messages WHERE id = ?", (message_id,)).fetchone()
                    if msg and msg['receiver'] == my_username:
                        conn.execute("UPDATE private_messages SET is_read = 1 WHERE id = ?", (message_id,))
                        conn.commit()
                        update_delivery_record(message_id, 'private', msg['sender'], msg['receiver'], status='read')
                await manager.send_to_private_chat(json.dumps({
                    "type": "private_read",
                    "message_id": message_id,
                    "status": 'read'
                }), my_username, peer_username)
                continue

    except WebSocketDisconnect:
        if my_username:
            manager.disconnect_private(websocket, peer_username, my_username)
    except Exception as e:
        print(f"Private Chat Error: {e}")
        if my_username:
            manager.disconnect_private(websocket, peer_username, my_username)


# --- ЭНДПОИНТ ДЛЯ СПИСКА ЛИЧНЫХ ЧАТОВ ---
@app.get("/get-private-chats")
async def get_private_chats(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    my_username = user['username']

    with get_db_connection() as conn:
        partners = conn.execute("""
            SELECT partner, MIN(created_at) AS created_at
            FROM (
                SELECT user2 AS partner, created_at FROM private_chats WHERE user1 = ?
                UNION ALL
                SELECT user1 AS partner, created_at FROM private_chats WHERE user2 = ?
                UNION ALL
                SELECT CASE WHEN sender = ? THEN receiver ELSE sender END AS partner, MIN(timestamp) AS created_at
                FROM private_messages WHERE sender = ? OR receiver = ? GROUP BY partner
            )
            GROUP BY partner
        """, (my_username, my_username, my_username, my_username, my_username)).fetchall()

        result = []
        for partner_row in partners:
            partner = partner_row['partner']
            profile = conn.execute("SELECT avatar FROM users WHERE username = ?", (partner,)).fetchone()
            if not profile:
                continue
            last_message = conn.execute("""
                SELECT id, sender, text, timestamp, is_read FROM private_messages
                WHERE (sender = ? AND receiver = ?) OR (sender = ? AND receiver = ?)
                ORDER BY id DESC LIMIT 1
            """, (my_username, partner, partner, my_username)).fetchone()
            result.append({
                "username": partner,
                "last_message": last_message['text'] if last_message else '',
                "timestamp": last_message['timestamp'] if last_message else partner_row['created_at'],
                "avatar": profile['avatar'],
                "online": is_user_globally_online(partner),
                "last_sender": last_message['sender'] if last_message else '',
                "last_is_read": bool(last_message['is_read']) if last_message else False,
                "sort_key": (1, last_message['id']) if last_message else (0, partner_row['created_at'])
            })
        result.sort(key=lambda chat: chat['sort_key'], reverse=True)
        for chat in result:
            chat.pop('sort_key', None)

    return result

@app.post("/api/private-chats/{peer_username}")
async def create_private_chat(request: Request, peer_username: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    user1, user2 = sorted((user['username'], peer_username))
    with get_db_connection() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE username = ?", (peer_username,)).fetchone():
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        conn.execute(
            "INSERT OR IGNORE INTO private_chats (user1, user2, created_at) VALUES (?, ?, ?)",
            (user1, user2, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
    return {"status": "ok", "username": peer_username}

@app.post("/api/open-private-chat-by-phone")
async def open_private_chat_by_phone(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректный запрос")
    phone = normalize_phone_number(payload.get('phone', ''))
    if not phone:
        raise HTTPException(status_code=400, detail="Введите номер в международном формате")

    with get_db_connection() as conn:
        peer = conn.execute("SELECT username FROM users WHERE phone = ?", (phone,)).fetchone()
    if not peer:
        return {"status": "not_found", "message": "У этого номера пока нет аккаунта DURABLE"}
    return await create_private_chat(request, peer['username'])

@app.post("/api/contacts/resolve")
async def resolve_device_contacts(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    try:
        payload = await request.json()
        contacts = payload.get('contacts', [])
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректный список контактов")
    if not isinstance(contacts, list) or len(contacts) > 500:
        raise HTTPException(status_code=400, detail="Можно проверить не более 500 контактов за раз")

    requested = []
    for index, contact in enumerate(contacts):
        if not isinstance(contact, dict):
            continue
        phone = normalize_phone_number(contact.get('phone', ''))
        if phone:
            requested.append({
                "id": str(contact.get('id') or index),
                "name": str(contact.get('name') or 'Без имени')[:120],
                "phone": phone
            })

    matches = {}
    unique_phones = list({contact['phone'] for contact in requested})
    if unique_phones:
        placeholders = ','.join('?' for _ in unique_phones)
        with get_db_connection() as conn:
            rows = conn.execute(
                f"SELECT username, phone, avatar, bio FROM users WHERE phone IN ({placeholders})",
                unique_phones
            ).fetchall()
        matches = {row['phone']: dict(row) for row in rows}

    result = []
    for contact in requested:
        account = matches.get(contact['phone'])
        result.append({
            **contact,
            "username": account['username'] if account else None,
            "avatar": account['avatar'] if account else None,
            "status": (account['bio'] or 'Здравствуйте! Я использую DURABLE') if account else 'Нет аккаунта DURABLE'
        })
    return {"status": "ok", "contacts": result}

@app.get("/get-forward-options")
async def get_forward_options(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    my_username = user['username']
    with get_db_connection() as conn:
        groups = conn.execute("""
            SELECT cr.room_id, cr.name
            FROM chat_rooms cr
            JOIN room_members rm ON cr.room_id = rm.room_id
            WHERE rm.username = ?
        """, (my_username,)).fetchall()

        private_chats = conn.execute("""
            SELECT partner, last_text, last_time, last_sender, last_is_read FROM (
                SELECT CASE WHEN sender = ? THEN receiver ELSE sender END as partner,
                       sender as last_sender,
                       is_read as last_is_read,
                       text as last_text,
                       timestamp as last_time
                FROM private_messages
                WHERE sender = ? OR receiver = ?
                ORDER BY id DESC
            ) as pm
            GROUP BY partner
            ORDER BY last_time DESC
        """, (my_username, my_username, my_username)).fetchall()

    return {
        "groups": [{"room_id": row['room_id'], "name": row['name']} for row in groups],
        "private": [{"username": row['partner'], "last_message": row['last_text'], "timestamp": row['last_time'], "last_sender": row['last_sender'], "last_is_read": bool(row['last_is_read']) if row['last_is_read'] is not None else False} for row in private_chats]
    }

@app.get("/search-users")
async def search_users(request: Request, q: Optional[str] = None):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    if not q:
        return {"status": "ok", "users": []}
    query = q.strip()
    search_value = f"%{query.lstrip('@')}%"
    with get_db_connection() as conn:
        users = conn.execute(
            "SELECT username, avatar, tag, bio, last_seen FROM users WHERE username LIKE ? OR tag LIKE ? ORDER BY username LIMIT 20",
            (search_value, search_value)
        ).fetchall()
    result = []
    for row in users:
        result.append({
            "username": row['username'],
            "avatar": row['avatar'],
            "tag": row['tag'] or f"@{row['username']}",
            "bio": row['bio'] or '',
            "online": is_user_globally_online(row['username'])
        })
    return {"status": "ok", "users": result}

@app.post("/forward-message")
async def forward_message(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    try:
        body = await request.json()
    except:
        return {"status": "error", "message": "Ошибка формата данных"}

    target_type = body.get("target_type")
    target_room_id = body.get("target_room_id")
    target_username = body.get("target_username")
    text = body.get("text", "")
    filename = body.get("filename", "")
    is_file = bool(body.get("is_file"))
    is_image = bool(body.get("is_image"))
    is_video = bool(body.get("is_video"))
    is_audio = bool(body.get("is_audio"))

    if not text:
        return {"status": "error", "message": "Нет текста для пересылки"}
    if target_type not in ["group", "private"]:
        return {"status": "error", "message": "Неверный тип цели"}

    def infer_media_flags(text_value):
        nonlocal is_file, is_image, is_video, is_audio, filename
        already_json = False
        try:
            parsed = json.loads(text_value)
            if isinstance(parsed, dict) and parsed.get('type') == 'file' and parsed.get('url'):
                is_file = True
                is_image = bool(parsed.get('is_image'))
                is_video = bool(parsed.get('is_video'))
                is_audio = bool(parsed.get('is_audio'))
                filename = filename or parsed.get('name', '')
                already_json = True
                return text_value, already_json
        except Exception:
            pass

        if text_value.startswith('/uploads/') or text_value.startswith('/media/'):
            is_file = True
            lower = text_value.lower()
            if lower.endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                is_image = True
            elif lower.endswith(('.mp4', '.webm', '.mov')):
                is_video = True
            elif lower.endswith(('.mp3', '.wav', '.ogg')):
                is_audio = True
        return text_value, already_json

    db_text, already_json = infer_media_flags(text)

    if (is_file or is_image or is_video or is_audio) and not already_json:
        db_text = json.dumps({
            "type": "file",
            "url": text,
            "name": filename,
            "is_image": is_image,
            "is_video": is_video,
            "is_audio": is_audio
        })

    with get_db_connection() as conn:
        if target_type == "group":
            if not target_room_id:
                return {"status": "error", "message": "Не указан ID группы"}
            member = conn.execute("SELECT 1 FROM room_members WHERE room_id = ? AND username = ?", (target_room_id, user['username'])).fetchone()
            if not member:
                return {"status": "error", "message": "Вы не участник этой группы"}
            conn.execute("INSERT INTO chat_messages (room_id, username, text, timestamp, reply_to_id, forwarded_from, is_forwarded) VALUES (?, ?, ?, ?, NULL, ?, ?)",
                         (target_room_id, user['username'], db_text, datetime.now().strftime("%H:%M"), forwarded_from, int(is_forwarded)))
            conn.commit()
            last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            message_data = {
                "type": "message",
                "id": last_id,
                "username": user['username'],
                "text": db_text if (is_file or is_image or is_video or is_audio) else text,
                "timestamp": datetime.now().strftime("%H:%M"),
                "reply_to_id": None,
                "is_file": is_file,
                "is_image": is_image,
                "is_video": is_video,
                "is_audio": is_audio,
                "filename": filename,
                "forwarded_from": forwarded_from,
                "is_forwarded": is_forwarded
            }
            await manager.broadcast_to_room(json.dumps(message_data), target_room_id)
        else:
            if not target_username:
                return {"status": "error", "message": "Не указан получатель"}
            if target_username == user['username']:
                return {"status": "error", "message": "Нельзя пересылать себе"}
            target_user = conn.execute("SELECT 1 FROM users WHERE username = ?", (target_username,)).fetchone()
            if not target_user:
                return {"status": "error", "message": "Пользователь не найден"}
            conn.execute("INSERT INTO private_messages (sender, receiver, text, timestamp, forwarded_from, is_forwarded) VALUES (?, ?, ?, ?, ?, ?)",
                         (user['username'], target_username, db_text, datetime.now().strftime("%H:%M"), forwarded_from, int(is_forwarded)))
            conn.commit()
            last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            await manager.send_to_private_chat(json.dumps({
                "type": "private_message",
                "sender": user['username'],
                "receiver": target_username,
                "text": db_text,
                "timestamp": datetime.now().strftime("%H:%M"),
                "is_mine": True,
                "id": last_id,
                "forwarded_from": forwarded_from,
                "is_forwarded": is_forwarded
            }), user['username'], target_username)

    return {"status": "ok", "message": "Переслано"}


@app.post("/leave-chat")
async def leave_chat(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    try:
        body = await request.json()
    except:
        return {"status": "error", "message": "Ошибка формата данных"}
    room_id = body.get('room_id')
    if not room_id:
        return {"status": "error", "message": "Не указан ID комнаты"}
    with get_db_connection() as conn:
        conn.execute("DELETE FROM room_members WHERE room_id = ? AND username = ?", (room_id, user['username']))
        conn.commit()
    # notify via websocket
    try:
        await manager.broadcast_to_room(json.dumps({"type": "update_participants", "room_id": room_id}), room_id)
    except Exception:
        pass
    return {"status": "ok", "message": "Вы вышли из чата"}


@app.post('/link-preview')
async def link_preview(request: Request):
    try:
        body = await request.json()
    except:
        return {"status": "error", "message": "Ошибка формата данных"}
    url = body.get('url')
    if not url:
        return {"status": "error", "message": "Нет URL"}
    # Простая реализация: GET + парсинг метатегов
    try:
        headers = { 'User-Agent': 'Mozilla/5.0 (compatible; LinkPreview/1.0)'}
        resp = requests.get(url, headers=headers, timeout=6)
        html = resp.text
        og_title = None
        og_desc = None
        og_image = None
        # Ищем Open Graph метатеги
        import re
        m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, re.IGNORECASE)
        if m: og_title = m.group(1)
        m = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', html, re.IGNORECASE)
        if m: og_desc = m.group(1)
        m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html, re.IGNORECASE)
        if m: og_image = m.group(1)
        # fallback на title
        if not og_title:
            m = re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE)
            if m: og_title = m.group(1).strip()
        # Упрощённо: возвращаем что есть
        return {"status": "ok", "title": og_title or '', "description": og_desc or '', "image": og_image or ''}
    except Exception as e:
        return {"status": "error", "message": "Не удалось получить preview"}

# --- ОСНОВНЫЕ ЭНДПОИНТЫ ---
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
async def register(request: Request, username: str = Form(...), password: str = Form(...), confirm_password: str = Form(...)):
    if password != confirm_password:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Пароли не совпадают!"})
    if not username.isascii() or not username.replace('_', '').isalnum():
        return templates.TemplateResponse("register.html", {"request": request, "error": "Логин: латиница и цифры!"})

    hashed_pw = get_password_hash(password)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        user_tag = f"@{username}"
        with get_db_connection() as conn:
            conn.execute("INSERT INTO users (username, password_hash, avatar, last_seen, tag, bio, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (username, hashed_pw, None, now, user_tag, '', now))
            conn.commit()

            # Проверяем, нужно ли добавить пользователя в чат после регистрации
            join_room_id = request.cookies.get("join_room_id")
            if join_room_id:
                room_id = join_room_id.upper().strip()
                room = conn.execute("SELECT name FROM chat_rooms WHERE room_id = ?", (room_id,)).fetchone()
                if room:
                    conn.execute("INSERT INTO room_members (room_id, username) VALUES (?, ?)", (room_id, username))
                    conn.commit()
                    # Если комната существует, перенаправляем сразу в дашборд с параметром join
                    response = RedirectResponse(url=f"/dashboard?join={room_id}", status_code=303)
                    response.set_cookie(key="username", value=username, httponly=True)
                    response.delete_cookie("join_room_id") # Очищаем cookie
                    return response

            # Если нет комнаты для присоединения, идем на дашборд как обычно
            response = RedirectResponse(url="/dashboard", status_code=303)
            response.set_cookie(key="username", value=username, httponly=True)
            response.delete_cookie("join_room_id") # Очищаем cookie на всякий случай
            return response

    except sqlite3.IntegrityError:
        return templates.TemplateResponse("register.html", {"request": request, "error": "Пользователь существует!"})

@app.get("/login", response_class=HTMLResponse)
async def show_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login(request: Request, response: Response, username: str = Form(...), password: str = Form(...)):
    with get_db_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный логин или пароль"})
    try:
        if verify_password(password, user["password_hash"]):
            update_last_seen(username)
            join_room_id = request.cookies.get("join_room_id")
            join_private_username = request.cookies.get("join_private_username")
            redirect_url = "/dashboard"
            if join_room_id and join_private_username:
                redirect_url = f"/dashboard?join={join_room_id}&private={join_private_username}"
            elif join_private_username:
                redirect_url = f"/dashboard?private={join_private_username}"
            elif join_room_id:
                redirect_url = f"/dashboard?join={join_room_id}"
            resp = RedirectResponse(url=redirect_url, status_code=303)
            resp.set_cookie(key="username", value=username, httponly=True)
            resp.delete_cookie("join_room_id")
            resp.delete_cookie("join_private_username")
            return resp
    except Exception as e:
        print(f"LOGIN ERROR: {e}")
    return templates.TemplateResponse("login.html", {"request": request, "error": "Неверный логин или пароль"})

@app.get("/logout")
async def logout(response: Response):
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("username")
    return resp

@app.get("/user-info", response_class=HTMLResponse)
async def user_info(request: Request, msg: Optional[str] = None):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")
    update_last_seen(user['username'])
    stats = {}
    with get_db_connection() as conn:
        groups_created = conn.execute("SELECT COUNT(*) AS cnt FROM chat_rooms WHERE admin_username = ?", (user['username'],)).fetchone()['cnt']
        groups_joined = conn.execute("SELECT COUNT(DISTINCT room_id) AS cnt FROM room_members WHERE username = ?", (user['username'],)).fetchone()['cnt']
        stats = {
            'groups_created': groups_created,
            'groups_joined': groups_joined,
            'created_at': user.get('created_at') or ''
        }
    return templates.TemplateResponse("user_info.html", {"request": request, "username": user["username"], "user": user, "success_msg": msg, "stats": stats})

@app.post("/update-profile")
async def update_profile(request: Request, tag: str = Form(...), bio: str = Form(None), phone: str = Form('')):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    tag = tag.strip()
    if tag and not tag.startswith('@'):
        tag = '@' + tag
    if not tag:
        tag = f"@{user['username']}"
    if not re.match(r'^@[A-Za-z0-9_]+$', tag):
        return RedirectResponse(url="/user-info?msg=error", status_code=303)
    normalized_phone = normalize_phone_number(phone) if phone.strip() else None
    if phone.strip() and not normalized_phone:
        return RedirectResponse(url="/user-info?msg=error", status_code=303)
    with get_db_connection() as conn:
        existing = conn.execute("SELECT username FROM users WHERE tag = ? AND username != ?", (tag, user['username'])).fetchone()
        if existing:
            return RedirectResponse(url="/user-info?msg=profile_error", status_code=303)
        existing_phone = conn.execute("SELECT username FROM users WHERE phone = ? AND username != ?", (normalized_phone, user['username'])).fetchone() if normalized_phone else None
        if existing_phone:
            return RedirectResponse(url="/user-info?msg=phone_error", status_code=303)
        conn.execute("UPDATE users SET tag = ?, bio = ?, phone = ? WHERE username = ?", (tag, bio or '', normalized_phone, user['username']))
        conn.commit()
    return RedirectResponse(url="/user-info?msg=profile_success", status_code=303)

@app.post("/update-password")
async def update_password(request: Request, new_password: str = Form(...)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    hashed_pw = get_password_hash(new_password)
    with get_db_connection() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hashed_pw, user["username"]))
        conn.commit()
    return RedirectResponse(url=f"/user-info?msg=password_success", status_code=303)

@app.post("/api/call/create-link")
async def create_call_link(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    token = generate_call_token()
    room_url = f"https://durable.me/call/{token}"
    return {
        "status": "ok",
        "token": token,
        "url": room_url,
        "link": room_url,
        "join_url": room_url
    }


@app.get("/call/{token}")
async def join_call_by_link(request: Request, token: str):
    user = get_current_user(request)
    if not user:
        response = RedirectResponse(url="/login", status_code=303)
        return response
    return RedirectResponse(url=f"/dashboard?call={token}", status_code=303)


@app.post("/api/call/start")
async def start_call(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid JSON"})

    target_username = (body.get("target_username") or "").strip()
    call_type = (body.get("call_type") or "audio").strip() or "audio"
    room_token = generate_call_token()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not target_username:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Нет пользователя"})

    with get_db_connection() as conn:
        busy_exists = conn.execute(
            """
            SELECT 1 FROM call_history
            WHERE status = 'in_progress'
              AND (
                  (caller_username = ? AND callee_username = ?) OR
                  (caller_username = ? AND callee_username = ?)
              )
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (user['username'], target_username, target_username, user['username'])
        ).fetchone()

        if busy_exists:
            conn.execute(
                "INSERT INTO call_history (caller_username, callee_username, room_token, status, direction, call_type, started_at, duration_seconds, data_usage_mb, missed_reason, group_call, group_name, participants_count, metadata) VALUES (?, ?, ?, 'busy', 'outgoing', ?, ?, 0, 0, 'busy', 0, '', 1, '{}')",
                (user['username'], target_username, room_token, call_type, now)
            )
            conn.commit()
            return JSONResponse(status_code=409, content={
                "status": "busy",
                "message": "Занято",
                "call_status": "busy",
                "target_username": target_username,
                "codec_profile": get_call_codec_profile(False)
            })

        conn.execute(
            "INSERT INTO call_history (caller_username, callee_username, room_token, status, direction, call_type, started_at, duration_seconds, data_usage_mb, missed_reason, group_call, group_name, participants_count, metadata) VALUES (?, ?, ?, 'in_progress', 'outgoing', ?, ?, 0, 0, '', 0, '', 1, '{}')",
            (user['username'], target_username, room_token, call_type, now)
        )
        conn.commit()

    return {
        "status": "ok",
        "call_status": "in_progress",
        "token": room_token,
        "join_url": f"https://durable.me/call/{room_token}",
        "target_username": target_username,
        "codec_profile": get_call_codec_profile(False)
    }


@app.get("/api/call/history")
async def get_call_history(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM call_history
            WHERE caller_username = ? OR callee_username = ?
            ORDER BY started_at DESC, id DESC
            """,
            (user['username'], user['username'])
        ).fetchall()

    items = [dict(row) for row in rows]
    return {"status": "ok", "items": items}


@app.post("/api/call/clear-history")
async def clear_call_history(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    with get_db_connection() as conn:
        conn.execute("DELETE FROM call_history WHERE caller_username = ? OR callee_username = ?", (user['username'], user['username']))
        conn.commit()
    return {"status": "ok", "message": "История звонков очищена"}


@app.post("/api/call/push-missed")
async def push_missed_call(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid JSON"})

    caller_username = (body.get("caller_username") or "").strip()
    target_username = (body.get("target_username") or user['username']).strip()
    if not caller_username:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Нет вызывающего"})

    payload = build_missed_call_push_payload(caller_username, target_username)
    return {"status": "ok", "push": payload}


@app.get("/api/call/codec-config")
async def get_call_codec_config(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    return {"status": "ok", "codec_profile": get_call_codec_profile(False)}


@app.get("/api/status/feed")
async def get_status_feed(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    cleanup_expired_statuses()

    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT s.*, u.avatar, u.tag
            FROM user_statuses s
            LEFT JOIN users u ON u.username = s.author_username
            WHERE s.expires_at > datetime('now')
            ORDER BY s.created_at DESC
            """
        ).fetchall()

    statuses = []
    for row in rows:
        with get_db_connection() as conn:
            viewers = conn.execute(
                "SELECT viewer_username as name, viewed_at as time FROM user_status_views WHERE status_id = ? ORDER BY viewed_at DESC LIMIT 10",
                (row['id'],)
            ).fetchall()
            replies = conn.execute(
                "SELECT sender_username as sender, message FROM user_status_replies WHERE status_id = ? ORDER BY id DESC LIMIT 5",
                (row['id'],)
            ).fetchall()
            reactions = conn.execute(
                "SELECT reaction, COUNT(*) as count FROM user_status_reactions WHERE status_id = ? GROUP BY reaction",
                (row['id'],)
            ).fetchall()

        statuses.append({
            "id": row['id'],
            "author": row['author_username'],
            "type": row['status_type'],
            "text": row['text'],
            "mediaUrl": row['media_url'],
            "caption": row['caption'],
            "background": row['background'] or "linear-gradient(135deg, #2A9D8F, #1B4332)",
            "fontFamily": row['font_family'] or 'system',
            "link": row['link'],
            "createdAt": row['created_at'],
            "expiresAt": row['expires_at'],
            "viewers": [dict(item) for item in viewers],
            "replies": [dict(item) for item in replies],
            "reactions": [dict(item) for item in reactions],
            "avatar": row['avatar'],
            "tag": row['tag']
        })

    return {"status": "ok", "statuses": statuses}


@app.post("/api/status/create")
async def create_status(request: Request, file: UploadFile = File(None)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    content_type = request.headers.get("content-type", "")
    media_url = ""

    if "multipart/form-data" in content_type:
        form = await request.form()
        status_type = (form.get("type") or "text").strip()
        text = (form.get("text") or "").strip()
        caption = (form.get("caption") or "").strip()
        background = form.get("background") or "linear-gradient(135deg, #2A9D8F, #1B4332)"
        font_family = form.get("font_family") or "system"
        link = (form.get("link") or "").strip()

        if file and getattr(file, 'filename', None):
            file_extension = os.path.splitext(file.filename)[1].lower() if "." in file.filename else ""
            timestamp = int(datetime.now().timestamp() * 1000)
            safe_name = f"status_{user['username']}_{timestamp}{file_extension}"
            file_path = os.path.join(UPLOAD_DIR, safe_name)
            with open(file_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            media_url = f"/uploads/{safe_name}"
    else:
        try:
            payload = await request.json()
        except Exception:
            return {"status": "error", "message": "Ошибка формата данных"}
        status_type = payload.get("type", "text")
        text = (payload.get("text") or "").strip()
        media_url = (payload.get("media_url") or "").strip()
        caption = (payload.get("caption") or "").strip()
        background = payload.get("background") or "linear-gradient(135deg, #2A9D8F, #1B4332)"
        font_family = payload.get("font_family") or "system"
        link = (payload.get("link") or "").strip()

    if status_type not in {"text", "image", "video", "audio"}:
        return {"status": "error", "message": "Неверный тип статуса"}
    if status_type == "text" and not text:
        return {"status": "error", "message": "Введите текст статуса"}
    if status_type in {"image", "video", "audio"} and not media_url:
        return {"status": "error", "message": "Добавьте медиафайл"}

    expires_at = (datetime.now() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO user_statuses (author_username, status_type, text, media_url, caption, background, font_family, link, created_at, expires_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, 1)
            """,
            (user['username'], status_type, text, media_url, caption, background, font_family, link, expires_at)
        )
        conn.commit()
        status_id = cursor.lastrowid

    return {"status": "ok", "status_id": status_id, "expires_at": expires_at}


@app.post("/api/status/{status_id}/view")
async def mark_status_viewed(request: Request, status_id: int):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    with get_db_connection() as conn:
        status = conn.execute("SELECT author_username FROM user_statuses WHERE id = ?", (status_id,)).fetchone()
        if not status:
            return {"status": "error", "message": "Статус не найден"}
        if status['author_username'] == user['username']:
            return {"status": "ok", "message": "Автор просмотрел свой статус"}
        conn.execute(
            "INSERT OR IGNORE INTO user_status_views (status_id, viewer_username, viewed_at) VALUES (?, ?, datetime('now'))",
            (status_id, user['username'])
        )
        conn.commit()
    return {"status": "ok", "message": "Просмотр учтен"}


@app.post("/api/status/{status_id}/reply")
async def reply_to_status(request: Request, status_id: int):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    try:
        payload = await request.json()
    except Exception:
        return {"status": "error", "message": "Ошибка формата данных"}

    message = (payload.get("message") or "").strip()
    if not message:
        return {"status": "error", "message": "Введите сообщение"}

    with get_db_connection() as conn:
        status = conn.execute("SELECT author_username FROM user_statuses WHERE id = ?", (status_id,)).fetchone()
        if not status:
            return {"status": "error", "message": "Статус не найден"}
        conn.execute(
            "INSERT INTO user_status_replies (status_id, sender_username, message, created_at) VALUES (?, ?, ?, datetime('now'))",
            (status_id, user['username'], message)
        )
        conn.commit()

    return {"status": "ok", "message": "Ответ отправлен"}


@app.post("/api/status/{status_id}/reaction")
async def react_to_status(request: Request, status_id: int):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")

    try:
        payload = await request.json()
    except Exception:
        return {"status": "error", "message": "Ошибка формата данных"}

    reaction = (payload.get("reaction") or "").strip()
    if not reaction:
        return {"status": "error", "message": "Нет реакции"}

    with get_db_connection() as conn:
        status = conn.execute("SELECT author_username FROM user_statuses WHERE id = ?", (status_id,)).fetchone()
        if not status:
            return {"status": "error", "message": "Статус не найден"}
        conn.execute(
            "INSERT OR IGNORE INTO user_status_reactions (status_id, sender_username, reaction, created_at) VALUES (?, ?, ?, datetime('now'))",
            (status_id, user['username'], reaction)
        )
        conn.commit()

    return {"status": "ok", "message": "Реакция учтена"}


@app.get("/dashboard", response_class=HTMLResponse)
async def show_dashboard(request: Request, join: Optional[str] = None, private: Optional[str] = None, call: Optional[str] = None):
    user = get_current_user(request)

    # Если пользователь не авторизован, но пытается присоединиться к чату или открыть личный чат
    if not user:
        response = RedirectResponse(url="/login", status_code=303)
        if join:
            response.set_cookie(key="join_room_id", value=join, httponly=True)
        if private:
            response.set_cookie(key="join_private_username", value=private, httponly=True)
        return response

    update_last_seen(user['username'])

    # Если пользователь авторизован и есть параметр join, добавляем его в чат
    if join:
        room_id = join.upper().strip()
        with get_db_connection() as conn:
            room = conn.execute("SELECT name FROM chat_rooms WHERE room_id = ?", (room_id,)).fetchone()
            if room:
                member = conn.execute("SELECT 1 FROM room_members WHERE room_id = ? AND username = ?", (room_id, user['username'])).fetchone()
                if not member:
                    conn.execute("INSERT INTO room_members (room_id, username) VALUES (?, ?)", (room_id, user['username']))
                    conn.commit()

    return templates.TemplateResponse("dashboard.html", {"request": request, "username": user["username"]})

@app.get("/get-my-chats", response_class=HTMLResponse)
async def get_my_chats(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    with get_db_connection() as conn:
        rooms = conn.execute(""" 
            SELECT cr.room_id, cr.name, cr.admin_username, cr.avatar_url,
                   COALESCE((SELECT cm.text FROM chat_messages cm WHERE cm.room_id = cr.room_id ORDER BY cm.id DESC LIMIT 1), 'Участник') AS last_message
            FROM chat_rooms cr 
            JOIN room_members rm ON cr.room_id = rm.room_id 
            WHERE rm.username = ? 
            ORDER BY (SELECT MAX(cm.id) FROM chat_messages cm WHERE cm.room_id = cr.room_id) DESC, cr.created_at DESC
        """, (user['username'],)).fetchall()
    html = ""
    for room in rooms:
        is_admin = "true" if room['admin_username'] == user['username'] else "false"
        room_id_value = room['room_id']
        room_name_value = room['name'] or 'Новая группа'
        onclick = html_escape(f"joinRoom({json.dumps(room_id_value)}, {json.dumps(room_name_value)}, {is_admin})", quote=True)
        room_name = html_escape(room_name_value)
        preview = html_escape(room['last_message'] or 'Группа создана')
        avatar = f"<img src=\"{html_escape(room['avatar_url'], quote=True)}\" alt=\"\">" if room['avatar_url'] else '<span>Г</span>'
        html += f""" 
        <div class='chat-room-item' onclick="{onclick}"> 
            <div class='room-avatar'>{avatar}</div>
            <div class='room-copy'><div class='room-name'>{room_name}</div><div class='room-status'>{preview}</div></div>
        </div> 
        """
    if not html:
        html = "<div style='padding:20px; text-align:center; color:#666;'>Нет чатов.</div>"
    return HTMLResponse(content=html)

@app.post("/delete-chat-room/{room_id}")
async def delete_chat_room(request: Request, room_id: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    with get_db_connection() as conn:
        room = conn.execute("SELECT admin_username FROM chat_rooms WHERE room_id = ?", (room_id,)).fetchone()
        if not room or room['admin_username'] != user['username']:
            return {"status": "error", "message": "Нет прав"}
        conn.execute("DELETE FROM chat_messages WHERE room_id = ?", (room_id,))
        conn.execute("DELETE FROM room_members WHERE room_id = ?", (room_id,))
        conn.execute("DELETE FROM chat_rooms WHERE room_id = ?", (room_id,))
        conn.commit()
    return {"status": "ok", "message": "Чат удален"}

@app.post("/delete-message/{msg_id}")
async def delete_message(request: Request, msg_id: int):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    with get_db_connection() as conn:
        msg = conn.execute("SELECT room_id, username FROM chat_messages WHERE id = ?", (msg_id,)).fetchone()
        if not msg:
            return {"status": "error", "message": "Сообщение не найдено"}
        room = conn.execute("SELECT admin_username FROM chat_rooms WHERE room_id = ?", (msg['room_id'],)).fetchone()
        is_admin = room and room['admin_username'] == user['username']
        is_author = msg['username'] == user['username']
        if not is_admin and not is_author:
            return {"status": "error", "message": "Нет прав"}
        conn.execute("DELETE FROM chat_messages WHERE id = ?", (msg_id,))
        conn.commit()
    await manager.broadcast_to_room(json.dumps({"type": "delete_message", "id": msg_id}), msg['room_id'])
    return {"status": "ok", "message": "Сообщение удалено"}

@app.post("/pin-message/{msg_id}")
async def pin_message(request: Request, msg_id: int):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    with get_db_connection() as conn:
        msg = conn.execute("SELECT room_id FROM chat_messages WHERE id = ?", (msg_id,)).fetchone()
        if not msg:
            return {"status": "error", "message": "Сообщение не найдено"}
        room_id = msg['room_id']
        room = conn.execute("SELECT admin_username FROM chat_rooms WHERE room_id = ?", (room_id,)).fetchone()
        if not room or room['admin_username'] != user['username']:
            return {"status": "error", "message": "Только админ"}
        conn.execute("UPDATE chat_rooms SET pinned_message_id = ? WHERE room_id = ?", (msg_id, room_id))
        conn.commit()
    await manager.broadcast_to_room(json.dumps({"type": "update_pinned", "msg_id": msg_id}), room_id)
    return {"status": "ok", "message": "Закреплено"}

@app.post("/unpin-message")
async def unpin_message(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    try:
        body = await request.json()
        room_id = body.get("room_id")
    except:
        return {"status": "error", "message": "Ошибка"}
    if not room_id:
        return {"status": "error", "message": "Нет ID"}
    with get_db_connection() as conn:
        room = conn.execute("SELECT admin_username FROM chat_rooms WHERE room_id = ?", (room_id,)).fetchone()
        if not room or room['admin_username'] != user['username']:
            return {"status": "error", "message": "Только админ"}
        conn.execute("UPDATE chat_rooms SET pinned_message_id = NULL WHERE room_id = ?", (room_id,))
        conn.commit()
    await manager.broadcast_to_room(json.dumps({"type": "update_pinned", "msg_id": 0}), room_id)
    return {"status": "ok", "message": "Откреплено"}

@app.get("/api/chat-pins")
async def get_chat_pins(request: Request, chat_type: str, room_id: Optional[str] = None, peer_username: Optional[str] = None):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    username = user['username']
    with get_db_connection() as conn:
        if chat_type == 'group' and room_id:
            member = conn.execute("SELECT 1 FROM room_members WHERE room_id = ? AND username = ?", (room_id, username)).fetchone()
            if not member:
                raise HTTPException(status_code=403, detail="Вы не участник группы")
            rows = conn.execute("""
                SELECT p.message_id, p.created_at AS pinned_at, m.username AS author, m.text, m.timestamp
                FROM pinned_chat_messages p JOIN chat_messages m ON m.id = p.message_id
                WHERE p.chat_type = 'group' AND p.room_id = ? ORDER BY p.created_at DESC
            """, (room_id,)).fetchall()
            legacy = conn.execute("SELECT pinned_message_id FROM chat_rooms WHERE room_id = ?", (room_id,)).fetchone()
            if legacy and legacy['pinned_message_id'] and not any(row['message_id'] == legacy['pinned_message_id'] for row in rows):
                message = conn.execute("SELECT id AS message_id, username AS author, text, timestamp FROM chat_messages WHERE id = ?", (legacy['pinned_message_id'],)).fetchone()
                if message:
                    rows = [message, *rows]
        elif chat_type == 'private' and peer_username:
            user1, user2 = sorted((username, peer_username))
            rows = conn.execute("""
                SELECT p.message_id, p.created_at AS pinned_at, m.sender AS author, m.text, m.timestamp
                FROM pinned_chat_messages p JOIN private_messages m ON m.id = p.message_id
                WHERE p.chat_type = 'private' AND p.user1 = ? AND p.user2 = ? ORDER BY p.created_at DESC
            """, (user1, user2)).fetchall()
        else:
            raise HTTPException(status_code=400, detail="Нужно указать чат")
    return {"status": "ok", "pins": [dict(row) for row in rows]}

@app.post("/api/chat-pins")
async def add_chat_pin(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    payload = await request.json()
    chat_type = payload.get('chat_type')
    message_id = payload.get('message_id')
    if chat_type not in ('group', 'private') or not message_id:
        raise HTTPException(status_code=400, detail="Некорректное закрепление")
    username = user['username']

    with get_db_connection() as conn:
        if chat_type == 'group':
            room_id = payload.get('room_id')
            member = conn.execute("SELECT 1 FROM room_members WHERE room_id = ? AND username = ?", (room_id, username)).fetchone()
            message = conn.execute("SELECT room_id FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
            if not member or not message or message['room_id'] != room_id:
                raise HTTPException(status_code=403, detail="Нельзя закрепить это сообщение")
            conn.execute("INSERT OR IGNORE INTO pinned_chat_messages (chat_type, room_id, message_id, pinned_by, created_at) VALUES ('group', ?, ?, ?, ?)", (room_id, message_id, username, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            conn.execute("UPDATE chat_rooms SET pinned_message_id = ? WHERE room_id = ?", (message_id, room_id))
            target = {"room_id": room_id}
        else:
            peer = payload.get('peer_username')
            if not peer:
                raise HTTPException(status_code=400, detail="Не указан собеседник")
            user1, user2 = sorted((username, peer))
            message = conn.execute("SELECT sender, receiver FROM private_messages WHERE id = ?", (message_id,)).fetchone()
            if not message or {message['sender'], message['receiver']} != {user1, user2}:
                raise HTTPException(status_code=403, detail="Нельзя закрепить это сообщение")
            conn.execute("INSERT OR IGNORE INTO pinned_chat_messages (chat_type, user1, user2, message_id, pinned_by, created_at) VALUES ('private', ?, ?, ?, ?, ?)", (user1, user2, message_id, username, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            target = {"user1": user1, "user2": user2}
        conn.commit()

    if chat_type == 'group':
        await manager.broadcast_to_room(json.dumps({"type": "pins_update"}), target['room_id'])
    else:
        await manager.send_to_private_chat(json.dumps({"type": "pins_update"}), target['user1'], target['user2'])
    return {"status": "ok"}

@app.delete("/api/chat-pins/{message_id}")
async def remove_chat_pin(request: Request, message_id: int, chat_type: str, room_id: Optional[str] = None, peer_username: Optional[str] = None):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    username = user['username']
    with get_db_connection() as conn:
        if chat_type == 'group' and room_id:
            member = conn.execute("SELECT 1 FROM room_members WHERE room_id = ? AND username = ?", (room_id, username)).fetchone()
            if not member:
                raise HTTPException(status_code=403, detail="Вы не участник группы")
            conn.execute("DELETE FROM pinned_chat_messages WHERE chat_type = 'group' AND room_id = ? AND message_id = ?", (room_id, message_id))
            legacy = conn.execute("SELECT pinned_message_id FROM chat_rooms WHERE room_id = ?", (room_id,)).fetchone()
            if legacy and legacy['pinned_message_id'] == message_id:
                conn.execute("UPDATE chat_rooms SET pinned_message_id = NULL WHERE room_id = ?", (room_id,))
            target = {"room_id": room_id}
        elif chat_type == 'private' and peer_username:
            user1, user2 = sorted((username, peer_username))
            conn.execute("DELETE FROM pinned_chat_messages WHERE chat_type = 'private' AND user1 = ? AND user2 = ? AND message_id = ?", (user1, user2, message_id))
            target = {"user1": user1, "user2": user2}
        else:
            raise HTTPException(status_code=400, detail="Нужно указать чат")
        conn.commit()
    if chat_type == 'group':
        await manager.broadcast_to_room(json.dumps({"type": "pins_update"}), target['room_id'])
    else:
        await manager.send_to_private_chat(json.dumps({"type": "pins_update"}), target['user1'], target['user2'])
    return {"status": "ok"}

@app.post("/edit-message")
async def edit_message(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    try:
        body = await request.json()
        msg_id = body.get("msg_id")
        new_text = body.get("text")
    except:
        return {"status": "error", "message": "Ошибка"}
    if not msg_id or new_text is None:
        return {"status": "error", "message": "Нет данных"}
    with get_db_connection() as conn:
        msg = conn.execute("SELECT room_id, username FROM chat_messages WHERE id = ?", (msg_id,)).fetchone()
        if not msg:
            return {"status": "error", "message": "Не найдено"}
        if msg['username'] != user['username']:
            return {"status": "error", "message": "Нельзя редактировать чужое"}
        conn.execute("UPDATE chat_messages SET text = ? WHERE id = ?", (new_text, msg_id))
        conn.commit()
    await manager.broadcast_to_room(json.dumps({"type": "edit_message", "id": msg_id, "text": new_text}), msg['room_id'])
    return {"status": "ok", "message": "Изменено"}

@app.post("/create-chat-room")
async def create_chat_room(request: Request, room_name: str = Form(...)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    room_id = generate_room_id()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        conn.execute("INSERT INTO chat_rooms (name, room_id, admin_username, created_at) VALUES (?, ?, ?, ?)", (room_name, room_id, user['username'], timestamp))
        conn.execute("INSERT INTO room_members (room_id, username) VALUES (?, ?)", (room_id, user['username']))
        conn.commit()
    return {"status": "ok", "room_id": room_id, "room_name": room_name}

@app.post("/api/create-group")
async def create_group(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Некорректные данные группы")

    group_name = str(payload.get('name') or '').strip()
    if not group_name:
        raise HTTPException(status_code=400, detail="Введите имя группы и при желании установите картинку")
    if len(group_name) > 80:
        raise HTTPException(status_code=400, detail="Название группы должно быть не длиннее 80 символов")

    members = payload.get('members', [])
    if not isinstance(members, list) or len(members) > 500:
        raise HTTPException(status_code=400, detail="Некорректный список участников")
    member_names = {str(name).strip() for name in members if isinstance(name, str) and name.strip()}
    member_names.discard(user['username'])
    disappearing_seconds = payload.get('disappearing_seconds', 0)
    if disappearing_seconds not in (0, 3600, 86400, 604800):
        raise HTTPException(status_code=400, detail="Некорректный срок исчезающих сообщений")
    permissions = payload.get('permissions', {})
    if not isinstance(permissions, dict):
        raise HTTPException(status_code=400, detail="Некорректные разрешения группы")
    avatar_url = str(payload.get('avatar_url') or '')
    if avatar_url and not avatar_url.startswith('/uploads/'):
        raise HTTPException(status_code=400, detail="Недопустимый путь аватара группы")

    room_id = generate_room_id()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        if member_names:
            placeholders = ','.join('?' for _ in member_names)
            existing_members = conn.execute(
                f"SELECT username FROM users WHERE username IN ({placeholders})",
                tuple(member_names)
            ).fetchall()
            existing_names = {row['username'] for row in existing_members}
            if existing_names != member_names:
                raise HTTPException(status_code=400, detail="Один или несколько участников не найдены")

        conn.execute(
            "INSERT INTO chat_rooms (name, room_id, admin_username, created_at, avatar_url, disappearing_seconds, permissions_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (group_name, room_id, user['username'], timestamp, avatar_url or None, disappearing_seconds, json.dumps(permissions, ensure_ascii=False))
        )
        conn.execute(
            "INSERT INTO room_members (room_id, username) VALUES (?, ?)",
            (room_id, user['username'])
        )
        conn.executemany(
            "INSERT INTO room_members (room_id, username) VALUES (?, ?)",
            [(room_id, member) for member in member_names]
        )
        conn.execute(
            "INSERT INTO chat_messages (room_id, username, text, timestamp) VALUES (?, ?, ?, ?)",
            (room_id, user['username'], 'Группа создана', datetime.now().strftime('%H:%M'))
        )
        conn.commit()

    return {"status": "ok", "room_id": room_id, "room_name": group_name, "avatar_url": avatar_url}

@app.post("/join-chat-by-id")
async def join_chat_by_id(request: Request, room_id: str = Form(...)):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    room_id = room_id.upper().strip()
    with get_db_connection() as conn:
        room = conn.execute("SELECT name FROM chat_rooms WHERE room_id = ?", (room_id,)).fetchone()
        if not room:
            return {"status": "error", "message": "Чат не найден"}
        member = conn.execute("SELECT 1 FROM room_members WHERE room_id = ? AND username = ?", (room_id, user['username'])).fetchone()
        if member:
            return {"status": "ok", "room_id": room_id, "room_name": room['name']}
        conn.execute("INSERT INTO room_members (room_id, username, is_pinned, pinned_order, muted, archived) VALUES (?, ?, 0, 0, 0, 0)", (room_id, user['username']))
        conn.commit()
    return {"status": "ok", "room_id": room_id, "room_name": room['name']}

@app.post("/api/save-draft")
async def save_draft(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    try:
        payload = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON"}

    room_id = (payload.get("room_id") or "").strip()
    chat_type = (payload.get("type") or "group").strip()
    draft_text = (payload.get("text") or "").strip()
    if not room_id:
        return {"status": "error", "message": "Нет room_id"}

    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO chat_drafts (username, room_id, chat_type, draft_text, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username, room_id, chat_type)
            DO UPDATE SET draft_text = excluded.draft_text, updated_at = excluded.updated_at
            """,
            (user['username'], room_id, chat_type, draft_text, updated_at)
        )
        conn.commit()
    return {"status": "ok", "draft": draft_text}

@app.get("/api/get-draft/{chat_type}/{room_id}")
async def get_draft(request: Request, chat_type: str, room_id: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT draft_text FROM chat_drafts WHERE username = ? AND room_id = ? AND chat_type = ?",
            (user['username'], room_id, chat_type)
        ).fetchone()
    return {"status": "ok", "text": row['draft_text'] if row else ""}

@app.post("/api/toggle-chat-pin")
async def toggle_chat_pin(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    payload = await request.json()
    room_id = (payload.get("room_id") or "").strip()
    pin = bool(payload.get("pin", False))
    if not room_id:
        return {"status": "error", "message": "room_id required"}

    with get_db_connection() as conn:
        member = conn.execute("SELECT is_pinned FROM room_members WHERE room_id = ? AND username = ?", (room_id, user['username'])).fetchone()
        if not member:
            return {"status": "error", "message": "Нет доступа"}
        conn.execute(
            "UPDATE room_members SET is_pinned = ?, pinned_order = ? WHERE room_id = ? AND username = ?",
            (1 if pin else 0, 1 if pin else 0, room_id, user['username'])
        )
        conn.commit()
    return {"status": "ok", "pinned": pin}

@app.post("/api/toggle-chat-mute")
async def toggle_chat_mute(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    payload = await request.json()
    room_id = (payload.get("room_id") or "").strip()
    muted = bool(payload.get("muted", False))
    if not room_id:
        return {"status": "error", "message": "room_id required"}

    with get_db_connection() as conn:
        conn.execute(
            "UPDATE room_members SET muted = ? WHERE room_id = ? AND username = ?",
            (1 if muted else 0, room_id, user['username'])
        )
        conn.commit()
    return {"status": "ok", "muted": muted}

def get_status_text(room_id, username, last_seen_str):
    if is_user_online(room_id, username):
        return "в сети", "#4CAF50"
    if not last_seen_str:
        return "был(а) недавно", "#888"
    try:
        last_seen = datetime.strptime(last_seen_str, "%Y-%m-%d %H:%M:%S")
        now = datetime.now()
        diff = now - last_seen
        if diff < timedelta(minutes=5):
            return "в сети", "#4CAF50"
        elif diff.days == 0:
            return f"был(а) сегодня в {last_seen.strftime('%H:%M')}", "#888"
        elif diff.days == 1:
            return f"был(а) вчера в {last_seen.strftime('%H:%M')}", "#888"
        else:
            return f"был(а) {last_seen.strftime('%d.%m.%Y')}", "#888"
    except:
        return "был(а) недавно", "#888"

@app.get("/get-room-info/{room_id}", response_class=HTMLResponse)
async def get_room_info(request: Request, room_id: str):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    current_username = user['username']
    with get_db_connection() as conn:
        room = conn.execute("SELECT name, admin_username FROM chat_rooms WHERE room_id = ?", (room_id,)).fetchone()
        members = conn.execute("SELECT rm.username, u.avatar, u.last_seen FROM room_members rm JOIN users u ON rm.username = u.username WHERE rm.room_id = ?", (room_id,)).fetchall()
    if not room:
        return HTMLResponse(content="<div>Чат не найден</div>")
    is_admin = "true" if room['admin_username'] == current_username else "false"
    admin_username = room['admin_username']
    html = f"""<div class="members-list" data-is-admin="{is_admin}">"""
    for m in members:
        u_name = m['username']
        u_avatar = m['avatar']
        u_last_seen = m['last_seen']
        avatar_src = f"{u_avatar}?v={u_name}" if u_avatar else f"https://via.placeholder.com/50?v={u_name}"
        status_text, status_color = get_status_text(room_id, u_name, u_last_seen)
        admin_badge = """ <span style="color: #FFD700; font-weight: bold; margin-left: 5px; font-size: 12px;">👑 Админ</span> """ if u_name == admin_username else ""
        kick_btn = f"<button class='kick-btn-full' onclick=\"kickUser('{u_name}')\">❌</button>" if room['admin_username'] == current_username and u_name != current_username else ""
        html += f"""
        <div class="member-item-full">
            <div style="display: flex; align-items: center; gap: 10px;">
                <img src="{avatar_src}" style="width: 40px; height: 40px; border-radius: 50%; object-fit: cover;" alt="Avatar">
                <div style="display: flex; flex-direction: column;">
                    <div style="display: flex; align-items: center;"><span class="user-name">{u_name}</span>{admin_badge}</div>
                    <span style="font-size: 12px; color: {status_color}; font-weight: bold;" class="user-status">{status_text}</span>
                </div>
            </div>
            {kick_btn}
        </div>
        """
    html += "</div>"
    return HTMLResponse(content=html)

@app.get("/get-user-profile/{username}")
async def get_user_profile(request: Request, username: str):
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    with get_db_connection() as conn:
        user = conn.execute("SELECT username, avatar, last_seen, bio, tag, created_at FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            return {"status": "error", "message": "Пользователь не найден"}
        status_text = "был(а) недавно"
        status_color = "#888"
        last_seen_str = user['last_seen']
        if last_seen_str:
            try:
                last_seen = datetime.strptime(last_seen_str, "%Y-%m-%d %H:%M:%S")
                now = datetime.now()
                diff = now - last_seen
                if diff < timedelta(minutes=5):
                    status_text = "в сети"
                    status_color = "#4CAF50"
                elif diff.days == 0:
                    status_text = f"был(а) сегодня в {last_seen.strftime('%H:%M')}"
                elif diff.days == 1:
                    status_text = f"был(а) вчера в {last_seen.strftime('%H:%M')}"
                else:
                    status_text = f"был(а) {last_seen.strftime('%d.%m.%Y')}"
            except: pass
        avatar_url = user['avatar'] if user['avatar'] else f"https://via.placeholder.com/150?v={username}"
        return {
            "status": "ok", "username": user['username'], "avatar": avatar_url,
            "bio": user['bio'] or '', "tag": user['tag'] or f"@{username}", "created_at": user['created_at'] or '',
            "status_text": status_text, "status_color": status_color
        }

# --- ПУБЛИЧНЫЙ ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ---
@app.get("/user-profile/{username}", response_class=HTMLResponse)
async def show_user_profile(request: Request, username: str):
    current_user = get_current_user(request)
    with get_db_connection() as conn:
        user_data = conn.execute("SELECT username, avatar, last_seen, bio, tag, created_at FROM users WHERE username = ?", (username,)).fetchone()
        if not user_data:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        status_text = "был(а) недавно"
        status_color = "#888"
        last_seen_str = user_data['last_seen']
        if last_seen_str:
            try:
                last_seen = datetime.strptime(last_seen_str, "%Y-%m-%d %H:%M:%S")
                now = datetime.now()
                diff = now - last_seen
                if diff < timedelta(minutes=5):
                    status_text = "в сети"
                    status_color = "#4CAF50"
                elif diff.days == 0:
                    status_text = f"был(а) сегодня в {last_seen.strftime('%H:%M')}"
                elif diff.days == 1:
                    status_text = f"был(а) вчера в {last_seen.strftime('%H:%M')}"
                else:
                    status_text = f"был(а) {last_seen.strftime('%d.%m.%Y')}"
            except: pass
            avatar_url = user_data['avatar']
        if not avatar_url:
            avatar_url = f"https://via.placeholder.com/300?text={username[0].upper()}"

        groups_created = conn.execute("SELECT COUNT(*) as cnt FROM chat_rooms WHERE admin_username = ?", (username,)).fetchone()['cnt']
        groups_joined = conn.execute("SELECT COUNT(DISTINCT room_id) as cnt FROM room_members WHERE username = ?", (username,)).fetchone()['cnt']
    return templates.TemplateResponse("user_profile.html", {
        "request": request,
        "user": {
            'username': user_data['username'],
            'avatar': avatar_url,
            'last_seen': user_data['last_seen'],
            'bio': user_data['bio'] or '',
            'tag': user_data['tag'] or f"@{username}",
            'created_at': user_data['created_at'] or ''
        },
        "status_text": status_text,
        "status_color": status_color,
        "groups_created": groups_created,
        "groups_joined": groups_joined,
        "current_user": dict(current_user) if current_user else None
    })

# --- ПРОФИЛЬ НОВОСТЕЙ ---
@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, search: Optional[str] = None):
    with get_db_connection() as conn:
        if search:
            raw_news = conn.execute("""
                SELECT
                    n.id,
                    n.title,
                    n.content,
                    n.author_username,
                    n.image_path,
                    n.created_at,
                    u.avatar as author_avatar,
                    n.link_preview,
                    COALESCE((SELECT COUNT(*) FROM news_comments nc WHERE nc.news_id = n.id), 0) AS comments_count,
                    COALESCE((SELECT COUNT(*) FROM news_views nv WHERE nv.news_id = n.id), 0) AS views_count
                FROM news n LEFT JOIN users u ON n.author_username = u.username
                WHERE n.title LIKE ? ORDER BY n.id DESC
            """, (f'%{search}%',)).fetchall()
        else:
            raw_news = conn.execute("""
                SELECT
                    n.id,
                    n.title,
                    n.content,
                    n.author_username,
                    n.image_path,
                    n.created_at,
                    u.avatar as author_avatar,
                    n.link_preview,
                    COALESCE((SELECT COUNT(*) FROM news_comments nc WHERE nc.news_id = n.id), 0) AS comments_count,
                    COALESCE((SELECT COUNT(*) FROM news_views nv WHERE nv.news_id = n.id), 0) AS views_count
                FROM news n LEFT JOIN users u ON n.author_username = u.username
                ORDER BY n.id DESC
            """).fetchall()

    news_list = []
    for row in raw_news:
        values = tuple(row)
        if len(values) < 10:
            values = values + (None,) * (10 - len(values))
        news_list.append(values[:10])

    user = get_current_user(request)
    user_stats = None
    if user:
        user = get_current_user(request)
    user_stats = None
    if user:
        with get_db_connection() as conn:
            user_db = conn.execute("SELECT username, avatar, bio, tag, created_at FROM users WHERE username = ?", (user['username'],)).fetchone()
            if user_db:
                groups_created = conn.execute("SELECT COUNT(*) as cnt FROM chat_rooms WHERE admin_username = ?", (user['username'],)).fetchone()['cnt']
                groups_joined = conn.execute("SELECT COUNT(DISTINCT room_id) as cnt FROM room_members WHERE username = ?", (user['username'],)).fetchone()['cnt']
                user_stats = {
                    'username': user_db['username'],
                    'avatar': user_db['avatar'] or f"https://via.placeholder.com/60?text={user_db['username'][0].upper()}",
                    'bio': user_db['bio'] or '',
                    'tag': user_db['tag'] or f"@{user['username']}",
                    'created_at': user_db['created_at'] or '',
                    'groups_created': groups_created,
                    'groups_joined': groups_joined
                }
    current_user_dict = dict(user) if user else None
    recommended_channels = {}
    with get_db_connection() as conn:
        rows = conn.execute("SELECT category, name, description, followers, growth_last_week, accent FROM recommended_channels ORDER BY category, growth_last_week DESC, followers DESC LIMIT 10").fetchall()
        for row in rows:
            recommended_channels.setdefault(row['category'], []).append({
                'name': row['name'],
                'description': row['description'],
                'followers': row['followers'],
                'growth_last_week': row['growth_last_week'],
                'accent': row['accent']
            })
    return templates.TemplateResponse("profile.html", {"request": request, "news_list": news_list, "search_query": search, "current_user": current_user_dict, "user_stats": user_stats, "recommended_channels": recommended_channels})

@app.get("/add-news", response_class=HTMLResponse)
async def show_add_news(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse(url="/login")
    return templates.TemplateResponse("add_news.html", {"request": request})

@app.post("/add-news")
async def save_news(request: Request, title: str = Form(...), content: str = Form(...), image: UploadFile = File(None), scheduled_at: Optional[str] = Form(None)):
    user = get_current_user(request)
    if not user: raise HTTPException(status_code=403, detail="Not authenticated")
    image_path = None
    if image and image.filename:
        file_extension = image.filename.split(".")[-1]
        timestamp = int(datetime.now().timestamp())
        new_filename = f"news_{user['username']}_{timestamp}.{file_extension}"
        file_path = os.path.join(UPLOAD_DIR, new_filename)
        try:
            with open(file_path, "wb") as buffer: shutil.copyfileobj(image.file, buffer)
            image_path = f"/uploads/{new_filename}"
        except Exception as e: print(f"Error saving image: {e}")

    link_preview = None
    if content:
        urls = re.findall(r'https?://[^\s)>"]+', content)
        if urls:
            preview = _extract_og_metadata(urls[0])
            if preview.get('title') or preview.get('description') or preview.get('image'):
                link_preview = json.dumps(preview, ensure_ascii=False)

    if scheduled_at:
        try:
            scheduled_dt = datetime.strptime(scheduled_at, "%Y-%m-%dT%H:%M")
        except ValueError:
            return RedirectResponse(url="/profile?error=invalid_schedule", status_code=303)
        if scheduled_dt <= datetime.now():
            return RedirectResponse(url="/profile?error=invalid_schedule", status_code=303)
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO scheduled_news (author_username, title, content, image_path, link_preview, scheduled_at, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (user["username"], title, content, image_path, link_preview, scheduled_dt.strftime("%Y-%m-%d %H:%M"), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()
        return RedirectResponse(url="/profile", status_code=303)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        conn.execute("INSERT INTO news (title, content, author_username, image_path, link_preview, created_at) VALUES (?, ?, ?, ?, ?, ?)", (title, content, user["username"], image_path, link_preview, now))
        conn.commit()
    return RedirectResponse(url="/profile", status_code=303)

@app.post("/delete-news/{news_id}")
async def delete_news(request: Request, news_id: int):
    user = get_current_user(request)
    if not user: raise HTTPException(status_code=403, detail="Not authenticated")
    with get_db_connection() as conn:
        news = conn.execute("SELECT author_username, image_path FROM news WHERE id = ?", (news_id,)).fetchone()
        if not news: return RedirectResponse(url="/profile", status_code=303)
        if news['author_username'] != user["username"]: raise HTTPException(status_code=403, detail="Вы не можете удалить чужую новость")
        if news['image_path']:
            try:
                file_path = os.path.join(UPLOAD_DIR, news['image_path'].lstrip('/'))
                if os.path.exists(file_path): os.remove(file_path)
            except: pass
        conn.execute("DELETE FROM news WHERE id = ?", (news_id,))
        conn.commit()
    return RedirectResponse(url="/profile", status_code=303)

@app.get("/edit-news/{news_id}", response_class=HTMLResponse)
async def show_edit_news(request: Request, news_id: int):
    user = get_current_user(request)
    if not user: return RedirectResponse(url="/login")
    with get_db_connection() as conn:
        news_data = conn.execute("SELECT id, title, content, author_username, image_path, created_at FROM news WHERE id = ?", (news_id,)).fetchone()
    if news_data:
        if news_data['author_username'] != user["username"]: return RedirectResponse(url="/profile")
        return templates.TemplateResponse("edit_news.html", {"request": request, "news": dict(news_data)})
    return RedirectResponse(url="/profile")

@app.post("/update-news/{news_id}")
async def update_news(request: Request, news_id: int, title: str = Form(...), content: str = Form(...), image: UploadFile = File(None), delete_image: bool = Form(False)):
    user = get_current_user(request)
    if not user: raise HTTPException(status_code=403, detail="Not authenticated")
    with get_db_connection() as conn:
        news = conn.execute("SELECT author_username, image_path FROM news WHERE id = ?", (news_id,)).fetchone()
        if not news or news['author_username'] != user["username"]: raise HTTPException(status_code=403, detail="Вы не можете редактировать чужую новость")
        image_path = news['image_path']
        link_preview = None
        if content:
            urls = re.findall(r'https?://[^\s)>"]+', content)
            if urls:
                preview = _extract_og_metadata(urls[0])
                if preview.get('title') or preview.get('description') or preview.get('image'):
                    link_preview = json.dumps(preview, ensure_ascii=False)
        if delete_image:
            if news['image_path']:
                try:
                    old_file_path = os.path.join(UPLOAD_DIR, news['image_path'].lstrip('/'))
                    if os.path.exists(old_file_path): os.remove(old_file_path)
                except Exception as e: print(f"Error deleting old file: {e}")
                image_path = None
        elif image and image.filename:
            file_extension = image.filename.split(".")[-1]
            timestamp = int(datetime.now().timestamp())
            new_filename = f"news_{user['username']}_{timestamp}.{file_extension}"
            file_path = os.path.join(UPLOAD_DIR, new_filename)
            try:
                with open(file_path, "wb") as buffer: shutil.copyfileobj(image.file, buffer)
                if news['image_path']:
                    old_file_path = os.path.join(UPLOAD_DIR, news['image_path'].lstrip('/'))
                    if os.path.exists(old_file_path): os.remove(old_file_path)
                image_path = f"/uploads/{new_filename}"
            except Exception as e: print(f"Error updating image: {e}")
        conn.execute("UPDATE news SET title = ?, content = ?, image_path = ?, link_preview = ? WHERE id = ?", (title, content, image_path, link_preview, news_id))
        conn.commit()
    return RedirectResponse(url="/profile", status_code=303)

@app.get("/api/news-recommendations")
async def news_recommendations():
    with get_db_connection() as conn:
        rows = conn.execute("SELECT category, name, description, followers, growth_last_week, accent FROM recommended_channels ORDER BY category, growth_last_week DESC, followers DESC LIMIT 10").fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row['category'], []).append({
                'name': row['name'],
                'description': row['description'],
                'followers': row['followers'],
                'growth_last_week': row['growth_last_week'],
                'accent': row['accent']
            })
    return {"status": "ok", "categories": grouped}

@app.get("/api/news-comments/{news_id}")
async def get_news_comments(news_id: int):
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT nc.id, nc.user_username, nc.text, nc.created_at, u.avatar
            FROM news_comments nc
            LEFT JOIN users u ON u.username = nc.user_username
            WHERE nc.news_id = ?
            ORDER BY nc.id ASC
        """, (news_id,)).fetchall()
        comments = [{
            "id": row['id'],
            "user_username": row['user_username'],
            "text": row['text'],
            "created_at": row['created_at'],
            "avatar": row['avatar']
        } for row in rows]
        count = conn.execute("SELECT COUNT(*) AS cnt FROM news_comments WHERE news_id = ?", (news_id,)).fetchone()['cnt']
    return {"status": "ok", "items": comments, "count": count}

@app.post("/api/news-comments")
async def add_news_comment(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=403, detail="Not authenticated")
    try:
        body = await request.json()
    except:
        return {"status": "error", "message": "Ошибка формата данных"}
    news_id = body.get('news_id')
    text = (body.get('text') or '').strip()
    if not news_id or not text:
        return {"status": "error", "message": "Пустой комментарий"}
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO news_comments (news_id, user_username, text, created_at) VALUES (?, ?, ?, ?)",
            (news_id, user['username'], text, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        comment_row = conn.execute("SELECT id, user_username, text, created_at FROM news_comments WHERE id = last_insert_rowid()").fetchone()
    return {"status": "ok", "item": {
        "id": comment_row['id'],
        "user_username": comment_row['user_username'],
        "text": comment_row['text'],
        "created_at": comment_row['created_at'],
        "avatar": (get_current_user(request) or {}).get('avatar')
    }}

@app.post("/api/news-view/{news_id}")
async def mark_news_view(request: Request, news_id: int):
    user = get_current_user(request)
    if not user:
        return {"status": "error", "message": "Not authenticated"}
    with get_db_connection() as conn:
        user_row = conn.execute("SELECT id FROM users WHERE username = ?", (user['username'],)).fetchone()
        if not user_row:
            return {"status": "error", "message": "User not found"}
        view_date = datetime.now().strftime('%Y-%m-%d')
        existing = conn.execute("SELECT 1 FROM news_views WHERE news_id = ? AND user_id = ? AND view_date = ?", (news_id, user_row['id'], view_date)).fetchone()
        if existing:
            count = conn.execute("SELECT COUNT(*) AS cnt FROM news_views WHERE news_id = ?", (news_id,)).fetchone()['cnt']
            return {"status": "ok", "count": count, "already_viewed": True}
        conn.execute("INSERT INTO news_views (news_id, user_id, view_date) VALUES (?, ?, ?)", (news_id, user_row['id'], view_date))
        conn.commit()
        count = conn.execute("SELECT COUNT(*) AS cnt FROM news_views WHERE news_id = ?", (news_id,)).fetchone()['cnt']
        return {"status": "ok", "count": count, "already_viewed": False}

@app.get("/generate-qr/{room_id}")
async def generate_qr_code(room_id: str):
    url = f"https://127.0.0.1:8001/dashboard?join={room_id}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")

@app.get("/generate-user-qr/{username}")
async def generate_user_qr(request: Request, username: str):
    origin = f"{request.url.scheme}://{request.url.netloc}"
    url = f"{origin}/dashboard?private={username}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")

if __name__ == "__main__":
    # Исправлен IP адрес (убран пробел)
    uvicorn.run(app, host="127.0.0.1", port=800)