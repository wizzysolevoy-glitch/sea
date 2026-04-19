#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OSINT FRAMEWORK v15.2 ENTERPRISE
✅ Fixed: Nick search rate-limiting & accuracy
✅ Fixed: EXIF now supports documents/files
✅ Added: /ban & /unban admin commands
✅ Added: Global ban check & improved balance
✅ Optimized: DB connections, error handling, state management
"""
import asyncio
import logging
import time
import re
import os
import json
import secrets
import hashlib
import base64
import random
import string
import traceback
import io
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Union, Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from collections import defaultdict
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut
import aiohttp
import phonenumbers
from phonenumbers import geocoder, carrier, timezone, NumberParseException
import aiosqlite
from PIL import Image, ExifTags

# ==========================================================
# 🔧 ГЛОБАЛЬНАЯ КОНФИГУРАЦИЯ
# ==========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8586981878:AAH8w1r9CqLB9QndBjdpSXWc_b9uk_qC3c4")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "8449965783").split(",")]
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "MOosa2010")
DB_PATH = os.getenv("DB_PATH", "osint_framework.db")
DATABASE_FOLDER = Path(os.getenv("DB_FOLDER", "database"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "100"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))
FREE_REQUESTS = int(os.getenv("FREE_REQUESTS", "10"))
REFERRAL_BONUS = int(os.getenv("REFERRAL_BONUS", "5"))
MIRROR_BONUS = int(os.getenv("MIRROR_BONUS", "5"))
PORT = int(os.getenv("PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("osint_bot.log", encoding="utf-8", mode="a"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("OSINT_v15.2")

# ==========================================================
# 📊 СИСТЕМА МЕТРИК И СТАТИСТИКИ
# ==========================================================
class SystemMetrics:
    def __init__(self):
        self.start_time = datetime.now()
        self.total_requests = 0
        self.total_errors = 0
        self.active_sessions = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.http_requests = 0
        self.plugin_stats = defaultdict(int)
        self.user_activity = defaultdict(list)

    def log_request(self, plugin: str = None, user_id: int = None):
        self.total_requests += 1
        if plugin:
            self.plugin_stats[plugin] += 1
        if user_id:
            self.user_activity[user_id].append(datetime.now())
            if len(self.user_activity[user_id]) > 100:
                self.user_activity[user_id] = self.user_activity[user_id][-100:]

    def log_error(self, error: str = None):
        self.total_errors += 1
        if error:
            logger.error(f"Error logged: {error}")

    def log_cache_hit(self):
        self.cache_hits += 1

    def log_cache_miss(self):
        self.cache_misses += 1

    def log_http(self):
        self.http_requests += 1

    def get_uptime(self) -> str:
        delta = datetime.now() - self.start_time
        days, seconds = delta.days, delta.seconds
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{days}д {hours}ч {minutes}м"

    def get_cache_hit_rate(self) -> float:
        total = self.cache_hits + self.cache_misses
        return (self.cache_hits / total * 100) if total > 0 else 0.0

    def get_report(self) -> str:
        return (f" **СИСТЕМНЫЕ МЕТРИКИ**\n"
                f"🔹 Аптайм: {self.get_uptime()}\n"
                f"🔹 Запросов всего: {self.total_requests}\n"
                f" Ошибок: {self.total_errors}\n"
                f"🔹 HTTP запросов: {self.http_requests}\n"
                f" Кэш: {self.cache_hits}/{self.cache_hits + self.cache_misses} "
                f"({self.get_cache_hit_rate():.1f}%)\n"
                f"🔹 Активных сессий: {self.active_sessions}\n"
                f"**Топ плагинов:**\n" +
                "\n".join(f"• {k}: {v}" for k, v in
                          sorted(self.plugin_stats.items(), key=lambda x: -x[1])[:5]))

metrics = SystemMetrics()

# ==========================================================
# 🔐 УТИЛИТЫ БЕЗОПАСНОСТИ
# ==========================================================
class SecurityUtils:
    @staticmethod
    def hash_password(password: str, salt: str = None) -> Tuple[str, str]:
        if not salt:
            salt = secrets.token_hex(16)
        hashed = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000)
        return hashed.hex(), salt

    @staticmethod
    def verify_password(password: str, hashed: str, salt: str) -> bool:
        test_hash, _ = SecurityUtils.hash_password(password, salt)
        return test_hash == hashed

    @staticmethod
    def validate_email(email: str) -> bool:
        pattern = r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'
        return re.match(pattern, email) is not None

    @staticmethod
    def validate_phone(phone: str) -> bool:
        try:
            p = phonenumbers.parse(phone, None)
            return phonenumbers.is_valid_number(p)
        except:
            return False

    @staticmethod
    def validate_ip(ip: str) -> bool:
        pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
        if not re.match(pattern, ip):
            return False
        parts = ip.split('.')
        return all(0 <= int(part) <= 255 for part in parts)

    @staticmethod
    def validate_domain(domain: str) -> bool:
        pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z]{2,})+$'
        return re.match(pattern, domain) is not None

    @staticmethod
    def sanitize_input(text: str, max_length: int = 1000, allowed_chars: str = None) -> str:
        text = text.strip()[:max_length]
        text = re.sub(r'[<>{}[\]\\`]', '', text)
        if allowed_chars:
            text = ''.join(c for c in text if c in allowed_chars)
        return text

    @staticmethod
    def generate_token(length: int = 32) -> str:
        return secrets.token_urlsafe(length)

# ==========================================================
# 🗄️ РАСШИРЕННАЯ БАЗА ДАННЫХ
# ==========================================================
class BotDatabase:
    def __init__(self, path: str):
        self.path = path
        self._connection = None

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                requests_left INTEGER DEFAULT 10,
                total_requests INTEGER DEFAULT 0,
                referral_code TEXT UNIQUE,
                referred_by INTEGER,
                is_premium BOOLEAN DEFAULT FALSE,
                is_banned BOOLEAN DEFAULT FALSE,
                is_admin BOOLEAN DEFAULT FALSE,
                premium_expires TEXT,
                language TEXT DEFAULT 'ru',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_active TEXT,
                last_request TEXT
            );
            CREATE TABLE IF NOT EXISTS mirrors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                mirror_url TEXT UNIQUE,
                mirror_code TEXT UNIQUE,
                uses_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER UNIQUE,
                bonus_given BOOLEAN DEFAULT FALSE,
                bonus_amount INTEGER DEFAULT 5,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (referrer_id) REFERENCES users(id),
                FOREIGN KEY (referred_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT CHECK(type IN ('stars_payment', 'manual', 'bonus', 'referral', 'mirror', 'admin_grant')),
                amount INTEGER,
                requests_added INTEGER,
                payment_id TEXT,
                status TEXT DEFAULT 'completed',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plugin TEXT,
                query TEXT,
                status TEXT,
                response_time REAL,
                error_message TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                ip_address TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT,
                expires_at REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS breaches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT COLLATE NOCASE,
                password TEXT,
                source TEXT,
                breach_date TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_users_referral ON users(referral_code);
            CREATE INDEX IF NOT EXISTS idx_users_active ON users(last_active);
            CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id);
            CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp);
            CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);
            CREATE INDEX IF NOT EXISTS idx_mirrors_user ON mirrors(user_id);
            CREATE INDEX IF NOT EXISTS idx_breaches_email ON breaches(email);
            CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at);
            """)
            await db.execute(
                "INSERT OR IGNORE INTO users (id, is_admin, requests_left, is_premium, username) VALUES (?, TRUE, 999999, TRUE, ?)",
                (ADMIN_IDS[0], "admin")
            )
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance_mode', 'false')")
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('welcome_message', 'Добро пожаловать в OSINT Framework!')")
            await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('rate_limit_per_minute', '100')")
            await db.commit()
        logger.info("️ Database schema initialized successfully")

    async def get_user(self, user_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def create_user(self, user_id: int, username: str, first: str, last: str) -> str:
        referral_code = f"ref_{user_id}_{secrets.token_hex(6)}"
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO users (id, username, first_name, last_name, referral_code, last_active) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, first, last, referral_code, datetime.now().isoformat())
            )
            await db.commit()
        return referral_code

    async def update_user(self, user_id: int, **kwargs) -> bool:
        if not kwargs:
            return False
        async with aiosqlite.connect(self.path) as db:
            fields = ", ".join(f"{k} = ?" for k in kwargs.keys())
            values = list(kwargs.values()) + [user_id]
            await db.execute(f"UPDATE users SET {fields} WHERE id = ?", values)
            await db.commit()
        return True

    async def use_request(self, user_id: int) -> Tuple[bool, str]:
        user = await self.get_user(user_id)
        if not user:
            return False, "Пользователь не найден"
        if user.get('is_banned'):
            return False, "Ваш аккаунт заблокирован"
        if user.get('is_admin') or user.get('is_premium'):
            return True, "Unlimited"
        if user['requests_left'] > 0:
            await self.update_user(
                user_id,
                requests_left=user['requests_left'] - 1,
                last_request=datetime.now().isoformat()
            )
            return True, "OK"
        return False, "Недостаточно запросов"

    async def add_requests(self, user_id: int, amount: int, reason: str = "bonus") -> bool:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET requests_left = requests_left + ?, total_requests = total_requests + ? WHERE id = ?",
                (amount, amount, user_id)
            )
            await db.commit()
            await self.log_transaction(user_id, reason, 0, amount, f"bonus_{reason}")
        return True

    async def get_all_users(self, limit: int = 50, offset: int = 0) -> List[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_user_count(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM users")
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def get_stats(self) -> dict:
        async with aiosqlite.connect(self.path) as db:
            total_users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
            active_users = (await (await db.execute(
                "SELECT COUNT(*) FROM users WHERE last_active > ?",
                ((datetime.now() - timedelta(hours=24)).isoformat(),)
            )).fetchone())[0]
            total_requests = (await (await db.execute("SELECT COALESCE(SUM(total_requests), 0) FROM users")).fetchone())[0]
            today_requests = (await (await db.execute(
                "SELECT COUNT(*) FROM logs WHERE timestamp > ?",
                (datetime.now().date().isoformat(),)
            )).fetchone())[0]
            banned_users = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_banned = TRUE")).fetchone())[0]
            premium_users = (await (await db.execute("SELECT COUNT(*) FROM users WHERE is_premium = TRUE")).fetchone())[0]
            breaches = (await (await db.execute("SELECT COUNT(*) FROM breaches")).fetchone())[0]
            total_income = (await (await db.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE type = 'stars_payment'"
            )).fetchone())[0]
            return {
                "total_users": total_users, "active_24h": active_users,
                "total_requests": total_requests, "today_requests": today_requests,
                "banned_users": banned_users, "premium_users": premium_users,
                "breaches_count": breaches, "total_income": total_income,
                "uptime": metrics.get_uptime(),
                "cache_hit_rate": f"{metrics.cache_hits}/{metrics.cache_hits + metrics.cache_misses}"
            }

    async def ban_user(self, user_id: int, reason: str = "Нарушение правил") -> bool:
        success = await self.update_user(user_id, is_banned=True)
        if success:
            logger.warning(f"🚫 User {user_id} banned: {reason}")
        return success

    async def unban_user(self, user_id: int) -> bool:
        success = await self.update_user(user_id, is_banned=False)
        if success:
            logger.info(f"✅ User {user_id} unbanned")
        return success

    async def is_banned(self, user_id: int) -> bool:
        user = await self.get_user(user_id)
        return user.get('is_banned', False) if user else False

    async def log(self, user_id: int, plugin: str, query: str, status: str = "ok",
                  response_time: float = 0, error_message: str = None) -> bool:
        try:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "INSERT INTO logs (user_id, plugin, query, status, response_time, error_message) VALUES (?, ?, ?, ?, ?, ?)",
                    (user_id, plugin, query, status, response_time, error_message)
                )
                await db.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to log: {e}")
            return False

    async def get_user_logs(self, user_id: int, limit: int = 50) -> List[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM logs WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
                (user_id, limit)
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def log_transaction(self, user_id: int, type_: str, amount: int,
                              requests: int, payment_id: str) -> bool:
        try:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "INSERT INTO transactions (user_id, type, amount, requests_added, payment_id) VALUES (?, ?, ?, ?, ?)",
                    (user_id, type_, amount, requests, payment_id)
                )
                await db.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to log transaction: {e}")
            return False

    async def get_cache(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT value FROM cache WHERE key = ? AND expires_at > ?",
                (key, time.time())
            )
            row = await cursor.fetchone()
            if row:
                metrics.log_cache_hit()
                return row[0]
            metrics.log_cache_miss()
            return None

    async def set_cache(self, key: str, value: str, ttl: int = CACHE_TTL) -> bool:
        try:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                    (key, value, time.time() + ttl)
                )
                await db.commit()
            return True
        except Exception as e:
            logger.error(f"Cache set error: {e}")
            return False

    async def clear_expired_cache(self) -> int:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("DELETE FROM cache WHERE expires_at <= ?", (time.time(),))
            await db.commit()
            return cursor.rowcount

    async def search_breaches(self, email: str) -> List[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT email, password, source, breach_date FROM breaches WHERE email = ? LIMIT 50",
                (email.lower(),)
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def add_breach(self, email: str, password: str, source: str, date: str = None) -> bool:
        if not date:
            date = datetime.now().isoformat()
        try:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "INSERT INTO breaches (email, password, source, breach_date) VALUES (?, ?, ?, ?)",
                    (email.lower(), password, source, date)
                )
                await db.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to add breach: {e}")
            return False

    async def create_mirror(self, user_id: int) -> str:
        mirror_code = secrets.token_urlsafe(10)
        mirror_url = f"https://t.me/osint_bot?start=mirror_{mirror_code}"
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO mirrors (user_id, mirror_url, mirror_code) VALUES (?, ?, ?)",
                (user_id, mirror_url, mirror_code)
            )
            await db.execute(
                "UPDATE users SET requests_left = requests_left + ? WHERE id = ?",
                (MIRROR_BONUS, user_id)
            )
            await db.commit()
        return mirror_url

    async def activate_mirror(self, user_id: int, mirror_code: str) -> Optional[int]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT id, user_id FROM mirrors WHERE mirror_code = ? AND is_active = TRUE",
                (mirror_code,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            mirror_id, creator_id = row
            if creator_id == user_id:
                return None
            cursor = await db.execute(
                "SELECT id FROM referrals WHERE referred_id = ? AND referrer_id = ?",
                (user_id, creator_id)
            )
            if await cursor.fetchone():
                return None
            await db.execute(
                "UPDATE users SET requests_left = requests_left + ? WHERE id = ?",
                (MIRROR_BONUS, creator_id)
            )
            await db.execute(
                "UPDATE mirrors SET uses_count = uses_count + 1 WHERE id = ?",
                (mirror_id,)
            )
            await db.execute(
                "INSERT INTO referrals (referrer_id, referred_id, bonus_amount) VALUES (?, ?, ?)",
                (creator_id, user_id, MIRROR_BONUS)
            )
            await db.commit()
        return creator_id

    async def process_referral(self, new_user_id: int, referrer_id: int) -> bool:
        if new_user_id == referrer_id:
            return False
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT id FROM referrals WHERE referred_id = ?",
                (new_user_id,)
            )
            if await cursor.fetchone():
                return False
            await db.execute(
                "INSERT INTO referrals (referrer_id, referred_id, bonus_amount) VALUES (?, ?, ?)",
                (referrer_id, new_user_id, REFERRAL_BONUS)
            )
            await db.execute(
                "UPDATE users SET requests_left = requests_left + ? WHERE id = ?",
                (REFERRAL_BONUS, referrer_id)
            )
            await db.execute(
                "UPDATE users SET referred_by = ? WHERE id = ?",
                (referrer_id, new_user_id)
            )
            await db.commit()
        return True

    async def get_setting(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> bool:
        try:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, value, datetime.now().isoformat())
                )
                await db.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to set setting: {e}")
            return False

# ==========================================================
# 🗃️ БАЗА УТЕЧЕК (ЛОКАЛЬНАЯ)
# ==========================================================
class BreachDatabase:
    def __init__(self, db_folder: Path):
        self.db_folder = db_folder
        self.db_folder.mkdir(exist_ok=True)
        self.breach_db = self.db_folder / "breaches.db"
        self.combo_json = self.db_folder / "combo_lists.json"
        self.phone_db = self.db_folder / "phone_leaks.db"

    async def init(self):
        async with aiosqlite.connect(self.breach_db) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS breaches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL COLLATE NOCASE,
                password TEXT,
                source TEXT,
                breach_date TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_email ON breaches(email)")
            await db.commit()
        async with aiosqlite.connect(self.phone_db) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS phones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                name TEXT,
                address TEXT,
                source TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_phone ON phones(phone)")
            await db.commit()
        logger.info(f"🕸️ Breach Database initialized: {self.db_folder.absolute()}")

    async def search_email(self, email: str) -> List[dict]:
        results = []
        email = email.lower().strip()
        try:
            async with aiosqlite.connect(self.breach_db) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT email, password, source, breach_date FROM breaches WHERE email = ? LIMIT 100",
                    (email,)
                )
                for row in await cursor.fetchall():
                    results.append({"email": row["email"], "password": row["password"], "source": row["source"], "date": row["breach_date"]})
        except Exception as e:
            logger.error(f"Breach DB error: {e}")
        return results

    async def search_phone(self, phone: str) -> List[dict]:
        results = []
        phone_clean = re.sub(r'[^\d+]', '', phone)
        try:
            async with aiosqlite.connect(self.phone_db) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT phone, name, address, source FROM phones WHERE phone LIKE ? OR phone LIKE ? LIMIT 50",
                    (f"%{phone_clean}%", f"%{phone}%")
                )
                for row in await cursor.fetchall():
                    results.append({"phone": row["phone"], "name": row["name"], "address": row["address"], "source": row["source"]})
        except Exception as e:
            logger.error(f"Phone DB error: {e}")
        return results

    async def get_stats(self) -> dict:
        stats = {"breaches": 0, "phones": 0, "emails": 0}
        try:
            async with aiosqlite.connect(self.breach_db) as db:
                stats["breaches"] = (await (await db.execute("SELECT COUNT(*) FROM breaches")).fetchone())[0]
                stats["emails"] = (await (await db.execute("SELECT COUNT(DISTINCT email) FROM breaches")).fetchone())[0]
        except: pass
        try:
            async with aiosqlite.connect(self.phone_db) as db:
                stats["phones"] = (await (await db.execute("SELECT COUNT(*) FROM phones")).fetchone())[0]
        except: pass
        return stats

# ==========================================================
# 🌐 HTTP КЛИЕНТ С ПОВТОРНЫМИ ПОПЫТКАМИ
# ==========================================================
class AsyncHTTP:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self.request_count = 0

    async def get_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=12),
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                    connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300, force_close=True)
                )
            return self._session

    async def get_json(self, url: str, headers: dict = None, retries: int = 3) -> dict:
        session = await self.get_session()
        for attempt in range(retries):
            try:
                metrics.log_http()
                async with session.get(url, headers=headers) as r:
                    r.raise_for_status()
                    return await r.json()
            except Exception:
                if attempt == retries - 1: return {}
                await asyncio.sleep(1.5 ** attempt)
        return {}

    async def get_text(self, url: str, headers: dict = None, retries: int = 3) -> str:
        session = await self.get_session()
        for attempt in range(retries):
            try:
                metrics.log_http()
                async with session.get(url, headers=headers) as r:
                    r.raise_for_status()
                    return await r.text()
            except Exception:
                if attempt == retries - 1: return ""
                await asyncio.sleep(1.5 ** attempt)
        return ""

    async def head(self, url: str, timeout: int = 4) -> int:
        session = await self.get_session()
        try:
            metrics.log_http()
            async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return r.status
        except:
            return 0

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

# ==========================================================
# ⏱️ RATE LIMITER
# ==========================================================
class RateLimiter:
    def __init__(self, max_req: int, window: int = 60):
        self.max = max_req
        self.window = window
        self.reqs = defaultdict(list)
        self.hourly = defaultdict(int)
        self.daily = defaultdict(int)

    def allow(self, uid: int, action: str = "default") -> Tuple[bool, str]:
        now = time.time()
        self.reqs[uid] = [t for t in self.reqs[uid] if now - t < self.window]
        if len(self.reqs[uid]) >= self.max:
            return False, f"Лимит: {self.max}/{self.window}сек"
        hour_key = int(now // 3600)
        if self.hourly[(uid, hour_key)] >= self.max * 10:
            return False, "Часовой лимит превышен"
        day_key = int(now // 86400)
        if self.daily[(uid, day_key)] >= self.max * 100:
            return False, "Дневной лимит превышен"
        self.reqs[uid].append(now)
        self.hourly[(uid, hour_key)] += 1
        self.daily[(uid, day_key)] += 1
        return True, "OK"

    def get_stats(self, uid: int) -> dict:
        now = time.time()
        minute_valid = len([t for t in self.reqs.get(uid, []) if now - t < self.window])
        hour_key = int(now // 3600)
        day_key = int(now // 86400)
        return {
            "minute": f"{minute_valid}/{self.max}",
            "hourly": f"{self.hourly[(uid, hour_key)]}/{self.max * 10}",
            "daily": f"{self.daily[(uid, day_key)]}/{self.max * 100}"
        }

# ==========================================================
#  DISCORD UTILS
# ==========================================================
class DiscordUtils:
    EPOCH = 1420070400000
    @staticmethod
    def decode_snowflake(sid: int) -> Optional[dict]:
        try:
            ts = ((sid >> 22) + DiscordUtils.EPOCH) / 1000
            created = datetime.fromtimestamp(ts)
            return {"created_at": created.strftime("%Y-%m-%d %H:%M:%S UTC"), "age_days": (datetime.now() - created).days, "snowflake": str(sid)}
        except: return None

    @staticmethod
    def avatar_url(uid: int, hsh: str = None) -> str:
        if hsh: return f"https://cdn.discordapp.com/avatars/{uid}/{hsh}.png"
        return f"https://cdn.discordapp.com/embed/avatars/{uid % 5}.png"

# ==========================================================
# ️ OSINT FRAMEWORK (ЯДРО)
# ==========================================================
class OSINTFramework:
    def __init__(self):
        self.db = BotDatabase(DB_PATH)
        self.breach_db = BreachDatabase(DATABASE_FOLDER)
        self.http = AsyncHTTP()
        self.limiter = RateLimiter(RATE_LIMIT)
        self.discord = DiscordUtils()
        self.security = SecurityUtils()
        self.plugins: Dict[str, Callable] = {}
        self.start_time = datetime.now()

    async def init(self):
        await self.db.init()
        await self.breach_db.init()
        self._register_plugins()
        logger.info("🕸️ OSINT Framework v15.2 ENTERPRISE initialized")

    def _register_plugins(self):
        self.plugins = {
            "ip": self.plugin_ip, "dns": self.plugin_dns, "whois": self.plugin_whois,
            "subs": self.plugin_subs, "nick": self.plugin_nick, "phone": self.plugin_phone,
            "email": self.plugin_email, "breach": self.plugin_breach, "ton": self.plugin_ton,
            "tg_id": self.plugin_tg_id, "geo": self.plugin_geo, "discord": self.plugin_discord,
            "exif": self.plugin_exif
        }

    async def plugin_ip(self, query: str) -> str:
        start_time = time.time()
        try:
            if not self.security.validate_ip(query):
                return "🕸️ Неверный формат IPv4. Пример: 8.8.8.8"
            cached = await self.db.get_cache(f"ip:{query}")
            if cached: return cached
            metrics.log_request("ip")
            data = await self.http.get_json(f"http://ip-api.com/json/{query}?fields=4194303")
            if data.get("status") == "fail": return f"🕸️ Ошибка API: {data.get('message', 'Unknown')}"
            lat, lon = data.get('lat', 0), data.get('lon', 0)
            res = (f"🕸️ **IP:** `{data['query']}`\n"
                   f"🕸️ **Страна:** {data.get('country')} ({data.get('countryCode')})\n"
                   f"🕸️ **Регион:** {data.get('regionName')}\n"
                   f"🕸️ **Город:** {data.get('city')}\n"
                   f"🕸️ **Координаты:** `{lat}, {lon}`\n"
                   f"🕸️ **ISP:** `{data.get('isp')}`\n"
                   f"🕸️ **Proxy/VPN:** {'✅' if data.get('proxy') else '❌'}")
            await self.db.set_cache(f"ip:{query}", res, 3600)
            await self.db.log(0, "ip", query, "ok", time.time() - start_time)
            return res
        except Exception as e:
            return f"🕸️ Ошибка: {str(e)}"

    async def plugin_dns(self, query: str) -> str:
        try:
            cached = await self.db.get_cache(f"dns:{query}")
            if cached: return cached
            metrics.log_request("dns")
            data = await self.http.get_json(f"https://dns.google/resolve?name={query}&type=A")
            answers = data.get("Answer", [])
            if not answers: return f"🕸️ DNS записи для `{query}` не найдены"
            res = f"️ **DNS для `{query}`:**\n" + "\n".join(f"🕸️ `{a['data']}` (TTL: {a['TTL']})" for a in answers[:20])
            await self.db.set_cache(f"dns:{query}", res, 1800)
            return res
        except Exception as e:
            return f"🕸️ Ошибка DNS: {str(e)}"

    async def plugin_whois(self, query: str) -> str:
        try:
            cached = await self.db.get_cache(f"whois:{query}")
            if cached: return cached
            metrics.log_request("whois")
            text = await self.http.get_text(f"https://api.hackertarget.com/whois/?q={query}")
            if "error" in text.lower() or len(text) < 100: return "🕸️ WHOIS скрыт или домен не найден"
            res = f"️ **WHOIS `{query}`:**\n```{text[:3500]}```"
            await self.db.set_cache(f"whois:{query}", res, 86400)
            return res
        except Exception as e:
            return f"🕸️ Ошибка WHOIS: {str(e)}"

    async def plugin_subs(self, query: str) -> str:
        try:
            cached = await self.db.get_cache(f"subs:{query}")
            if cached: return cached
            metrics.log_request("subs")
            data = await self.http.get_json(f"https://crt.sh/?q={query}&output=json")
            subs = {s.strip() for e in data for s in e.get("name_value", "").split("\n") if s.strip() and "*" not in s}
            if not subs: return f"🕸️ Субдомены для `{query}` не найдены"
            res = f"🕸️ **Субдомены `{query}` (Top 50):**\n" + "\n".join(f"🕸️ `{s}`" for s in sorted(subs)[:50])
            await self.db.set_cache(f"subs:{query}", res, 43200)
            return res
        except Exception as e:
            return f"🕸️ Ошибка: {str(e)}"

    async def plugin_nick(self, query: str) -> str:
        """Исправленный поиск по нику с защитой от блокировок"""
        start_time = time.time()
        metrics.log_request("nick")
        query = query.strip().lower()
        if not query or len(query) < 2:
            return "🕸️ Введите корректный никнейм (минимум 2 символа)"

        platforms = {
            "VK": f"https://vk.com/{query}", "Telegram": f"https://t.me/{query}",
            "Instagram": f"https://instagram.com/{query}", "Twitter/X": f"https://twitter.com/{query}",
            "TikTok": f"https://tiktok.com/@{query}", "Reddit": f"https://reddit.com/user/{query}",
            "YouTube": f"https://youtube.com/@{query}", "Twitch": f"https://twitch.tv/{query}",
            "GitHub": f"https://github.com/{query}", "Steam": f"https://steamcommunity.com/id/{query}",
            "SoundCloud": f"https://soundcloud.com/{query}", "Pinterest": f"https://pinterest.com/{query}",
            "Discord": f"https://discord.com/users/{query}", "LastFM": f"https://last.fm/user/{query}",
            "Linktree": f"https://linktr.ee/{query}", "Patreon": f"https://patreon.com/{query}",
            "OnlyFans": f"https://onlyfans.com/{query}", "Fansly": f"https://fansly.com/{query}",
            "Boosty": f"https://boosty.to/{query}", "StreamElements": f"https://streamelements.com/{query}",
            "LBRY": f"https://lbry.tv/@{query}", "Mastodon": f"https://mastodon.social/@{query}",
            "Keybase": f"https://keybase.io/{query}", "Medium": f"https://medium.com/@{query}",
            "CodePen": f"https://codepen.io/{query}", "Replit": f"https://replit.com/@{query}",
            "HackTheBox": f"https://hackthebox.com/profile/{query}", "TryHackMe": f"https://tryhackme.com/p/{query}",
            "LeetCode": f"https://leetcode.com/{query}", "Codeforces": f"https://codeforces.com/profile/{query}",
            "Bitbucket": f"https://bitbucket.org/{query}", "SourceForge": f"https://sourceforge.net/u/{query}",
            "WordPress": f"https://{query}.wordpress.com", "Carrd": f"https://{query}.carrd.co",
            "Linkin.bio": f"https://linkin.bio/{query}", "Bio.link": f"https://bio.link/{query}",
            "Kofi": f"https://ko-fi.com/{query}", "BuyMeACoffee": f"https://buymeacoffee.com/{query}",
            "Gumroad": f"https://gumroad.com/{query}", "Etsy": f"https://etsy.com/shop/{query}",
            "Redbubble": f"https://redbubble.com/people/{query}", "Teespring": f"https://teespring.com/stores/{query}",
            "Threadless": f"https://threadless.com/@{query}", "Pixiv": f"https://pixiv.net/users/{query}",
            "Danbooru": f"https://danbooru.donmai.us/users/{query}", "FurAffinity": f"https://furaffinity.net/user/{query}",
            "Odysee": f"https://odysee.com/@{query}", "Rumble": f"https://rumble.com/user/{query}",
            "Minds": f"https://minds.com/{query}", "Gab": f"https://gab.com/{query}",
            "TruthSocial": f"https://truthsocial.com/@{query}", "MeWe": f"https://mewe.com/profile/{query}",
            "Diaspora": f"https://diasporafoundation.org/people/{query}", "Misskey": f"https://misskey.io/@{query}",
            "PeerTube": f"https://peertube.social/a/{query}", "DTube": f"https://d.tube/#!/c/{query}",
            "Hive": f"https://hive.blog/@{query}", "Steemit": f"https://steemit.com/@{query}",
            "Lemmy": f"https://lemmy.world/u/{query}", "Kbin": f"https://kbin.social/u/{query}",
            "Discourse": f"https://meta.discourse.org/u/{query}", "XenForo": f"https://xenforo.com/community/members/{query}",
            "Fansly": f"https://fansly.com/{query}", "ManyVids": f"https://www.manyvids.com/Profile/{query}",
            "JustForFans": f"https://justforfans.com/{query}", "FanCentro": f"https://fancentro.com/{query}",
            "LoyalFans": f"https://loyalfans.com/{query}", "Fanbox": f"https://fanbox.cc/{query}",
            "Donationalerts": f"https://www.donationalerts.com/r/{query}", "Donorbox": f"https://donorbox.org/{query}",
            "Streamlabs": f"https://streamlabs.com/{query}", "Tipeee": f"https://fr.tipeee.com/{query}",
            "Liberapay": f"https://liberapay.com/{query}", "OpenCollective": f"https://opencollective.com/{query}",
            "Fundrazr": f"https://fundrazr.com/{query}", "GoFundMe": f"https://www.gofundme.com/f/{query}",
            "Kickstarter": f"https://www.kickstarter.com/profile/{query}", "Indiegogo": f"https://www.indiegogo.com/individual/{query}"
        }

        found, not_found, errors = [], 0, 0
        platform_list = list(platforms.items())
        semaphore = asyncio.Semaphore(10)  # Ограничение параллельных запросов

        async def check_one(name, url):
            async with semaphore:
                try:
                    status = await self.http.head(url, timeout=4)
                    if status in (200, 301, 302, 307):
                        return ("found", f"🕸️ {name}: `{url}`")
                    return ("not_found", None)
                except Exception:
                    return ("error", None)

        # Пакетная обработка с задержками
        for i in range(0, len(platform_list), 15):
            batch = platform_list[i:i+15]
            tasks = [check_one(n, u) for n, u in batch]
            results = await asyncio.gather(*tasks)
            for res in results:
                if res[0] == "found": found.append(res[1])
                elif res[0] == "not_found": not_found += 1
                else: errors += 1
            await asyncio.sleep(0.3)  # Задержка между пакетами

        res = f"🕸️ **Никнейм:** `{query}`\n"
        res += f"🕸️ **Найдено:** {len(found)}/{len(platforms)}\n"
        res += f"🕸️ **Не найдено:** {not_found}\n"
        if errors: res += f"️ **Ошибок/таймаутов:** {errors}\n"
        if found:
            res += "\n**🕸️ Активные профили:**\n" + "\n".join(found[:80])
            if len(found) > 80: res += f"\n... и ещё {len(found)-80}"

        await self.db.log(0, "nick", query, "ok", time.time() - start_time)
        await self.db.set_cache(f"nick:{query}", res, 3600)
        return res

    async def plugin_phone(self, query: str) -> str:
        try:
            p = phonenumbers.parse(query, None)
            if not phonenumbers.is_valid_number(p): return "️ Номер невалиден. Используйте формат: +79991234567"
            region_code = phonenumbers.region_code_for_number(p)
            region_name = geocoder.description_for_number(p, "ru")
            carr = carrier.name_for_number(p, "ru") or "Не определен"
            tz_list = timezone.time_zones_for_number(p)
            fmt = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            num_type = phonenumbers.number_type(p)
            type_map = {phonenumbers.PhoneNumberType.MOBILE: "Мобильный", phonenumbers.PhoneNumberType.FIXED_LINE: "Городской"}
            res = (f"🕸️ **Номер:** `{fmt}`\n"
                   f"🕸️ **Страна/Регион:** {region_name}\n"
                   f"🕸️ **Оператор:** {carr}\n"
                   f"🕸️ **Часовой пояс:** {tz_list[0] if tz_list else 'N/A'}\n"
                   f"🕸️ **Тип:** {type_map.get(num_type, 'Другой')}")
            return res
        except Exception as e:
            return f"🕸️ Ошибка: {str(e)}. Формат: +79991234567"

    def plugin_email(self, query: str) -> str:
        metrics.log_request("email")
        if not self.security.validate_email(query): return "🕸️ Неверный формат email"
        domain = query.split("@")[1]
        temp_domains = ["tempmail.com", "guerrillamail.com", "mailinator.com", "yopmail.com"]
        warn = "🕸️ **⚠️ ВРЕМЕННЫЙ ДОМЕН!**" if domain.lower() in temp_domains else "🕸️ Домен надёжный"
        return f"️ **Email:** `{query}`\n️ Домен: `{domain}`\n{warn}"

    async def plugin_breach(self, query: str) -> str:
        metrics.log_request("breach")
        if not self.security.validate_email(query): return "🕸️ Введите корректный email"
        results = await self.breach_db.search_email(query)
        if not results: return f"🕸️ **Email:** `{query}`\n🕸️ Не найден в базах утечек"
        sources = {}
        for r in results:
            src = r.get("source", "Unknown")
            sources.setdefault(src, []).append(r)
        res = f"️ **Email:** `{query}`\n️ **Найдено утечек:** {len(results)}\n🕸️ **Источников:** {len(sources)}\n"
        for source, breaches in list(sources.items())[:5]:
            res += f"🕸️ **{source}** ({len(breaches)} записей):\n"
            for b in breaches[:3]:
                pwd = b.get("password", "N/A")
                masked = pwd[:2] + "*" * (len(pwd) - 2) if len(pwd) > 2 else "***"
                res += f"   • `{masked}` ({b.get('date', 'N/A')})\n"
        return res

    async def plugin_ton(self, query: str) -> str:
        cached = await self.db.get_cache(f"ton:{query}")
        if cached: return cached
        try:
            data = await self.http.get_json(f"https://toncenter.com/api/v2/getAddressInformation?address={query}")
            if data.get("ok"):
                bal = int(data["result"].get("balance", 0)) / 1e9
                is_wallet = "b5ee9c72" in data["result"].get("code", "")
                res = f"️ **TON:** `{query}`\n️ **Баланс:** `{bal:.4f} TON`\n🕸️ **Тип:** {'Кошелёк' if is_wallet else 'Обычный адрес'}"
                await self.db.set_cache(f"ton:{query}", res, 60)
                return res
            return "🕸️ Адрес не найден"
        except Exception as e:
            return f"🕸️ Ошибка TON: {str(e)}"

    async def plugin_tg_id(self, query: str, bot) -> str:
        clean = query.replace("@", "").strip()
        if not clean: return "🕸️ Введите username"
        try:
            chat = await bot.get_chat(f"@{clean}")
            return (f"🕸️ **Telegram:** `{clean}`\n"
                    f"🕸️ **ID:** `{chat.id}`\n"
                    f"🕸️ **Имя:** {chat.first_name or ''} {chat.last_name or ''}\n"
                    f"🕸️ **Username:** @{chat.username or 'N/A'}\n"
                    f"️ **Бот:** {'✅ Да' if chat.is_bot else '❌ Нет'}")
        except Exception:
            return f"🕸️ **Telegram:** `{clean}`\n🕸️ Пользователь не найден ботом или аккаунт приватный."

    async def plugin_geo(self, query: str) -> str:
        parts = query.split()
        if len(parts) != 2: return "🕸️ Формат: `lat lon` (55.7558 37.6173)"
        try:
            data = await self.http.get_json(f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={parts[0]}&lon={parts[1]}")
            addr = data.get("address", {})
            return (f"🕸️ **Координаты:** `{parts[0]}, {parts[1]}`\n"
                    f"🕸️ **Адрес:** `{data.get('display_name', 'Не найден')}`\n"
                    f"🕸️ Город: {addr.get('city', addr.get('town', 'N/A'))}\n"
                    f"🕸️ Страна: {addr.get('country', 'N/A')}")
        except Exception as e:
            return f"🕸️ Ошибка гео: {str(e)}"

    async def plugin_discord(self, query: str) -> str:
        if not re.match(r"^\d{17,19}$", query): return "🕸️ Введите Discord ID (число 17-19 цифр)"
        user_id = int(query)
        decoded = self.discord.decode_snowflake(user_id)
        if not decoded: return "🕸️ Неверный Discord ID"
        avatar_url = self.discord.avatar_url(user_id)
        return (f"️ **Discord User**\n"
                f"🕸️ **User ID:** `{user_id}`\n"
                f"️ **Дата создания:** `{decoded['created_at']}`\n"
                f"🕸️ **Возраст аккаунта:** {decoded['age_days']} дней\n"
                f"🕸️ **Аватар:** [PNG]({avatar_url})")

    async def plugin_exif(self, file_id: str, bot) -> str:
        """Исправленный EXIF: поддержка фото и документов"""
        metrics.log_request("exif")
        try:
            file = await bot.get_file(file_id)
            file_bytes = await file.download_as_bytearray()
            
            # Проверка, что это действительно изображение
            try:
                img = Image.open(io.BytesIO(file_bytes))
                img.verify() # Проверка целостности
                img = Image.open(io.BytesIO(file_bytes)) # Перезагрузка после verify
            except Exception:
                return "🕸️ Файл не является корректным изображением. Отправьте JPG/PNG/TIFF."

            exif_data = img._getexif()
            if not exif_data:
                return "🕸️ **EXIF:** Метаданные отсутствуют.\n️ Отправьте фото как 'Файл' (без сжатия Telegram)."

            from PIL.ExifTags import TAGS
            decoded = {}
            for tag_id, value in exif_data.items():
                tag = TAGS.get(tag_id, tag_id)
                if isinstance(value, bytes):
                    try: value = value.decode('utf-8', errors='ignore')
                    except: value = str(value)
                decoded[tag] = value

            res = "🕸️ **EXIF Метаданные:**\n"
            important_tags = ["DateTimeOriginal", "DateTime", "Make", "Model", "Software",
                              "GPSInfo", "DateTimeDigitized", "LensModel", "ExifVersion",
                              "ISOSpeedRatings", "FNumber", "ExposureTime", "Flash"]
            for tag in important_tags:
                if tag in decoded:
                    val = decoded[tag]
                    if tag == "GPSInfo" and isinstance(val, dict):
                        res += f"🕸️ **{tag}:** GPS данные найдены 📍\n"
                    else:
                        res += f"️ **{tag}:** `{val}`\n"
            extra_count = len(decoded) - len(important_tags)
            if extra_count > 0: res += f"\n... и ещё {extra_count} метаданных"
            return res
        except Exception as e:
            return f"🕸️ Ошибка чтения EXIF: {str(e)}"

# ==========================================================
#  TELEGRAM BOT (ПОЛНАЯ ЛОГИКА)
# ==========================================================
fw = OSINTFramework()
admin_sessions: Dict[int, dict] = {}
user_states: Dict[int, dict] = {}

KB_MAIN = InlineKeyboardMarkup([
    [InlineKeyboardButton("🕸️ IP Info", callback_data="tool_ip"), InlineKeyboardButton("️ Phone", callback_data="tool_phone")],
    [InlineKeyboardButton("️ Nick 200+", callback_data="tool_nick"), InlineKeyboardButton("🕸️ Email", callback_data="tool_email")],
    [InlineKeyboardButton("🕸️ Breach DB", callback_data="tool_breach"), InlineKeyboardButton("🕸️ Discord", callback_data="tool_discord")],
    [InlineKeyboardButton("🕸️ DNS/WHOIS", callback_data="tool_dns"), InlineKeyboardButton("🕸️ TON", callback_data="tool_ton")],
    [InlineKeyboardButton("📊 Баланс", callback_data="balance"), InlineKeyboardButton("📷 EXIF", callback_data="tool_exif")],
    [InlineKeyboardButton("🔐 Админка", callback_data="admin_start")]
])

KB_ADMIN_LOGIN = InlineKeyboardMarkup([[InlineKeyboardButton("🔐 Войти", callback_data="admin_enter_pass")]])
KB_ADMIN = InlineKeyboardMarkup([
    [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"), InlineKeyboardButton("👥 Юзеры", callback_data="admin_users")],
    [InlineKeyboardButton("📜 Логи", callback_data="admin_logs"), InlineKeyboardButton("🚫 Бан/Разбан", callback_data="admin_ban")],
    [InlineKeyboardButton("🔙 Выйти", callback_data="admin_logout")]
])
KB_BACK = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Меню", callback_data="menu")]])

async def check_banned(uid: int, update_or_q, is_callback=False):
    """Глобальная проверка бана"""
    user = await fw.db.get_user(uid)
    if user and user.get('is_banned'):
        if is_callback:
            await update_or_q.answer(" Ваш аккаунт заблокирован", show_alert=True)
        else:
            await update_or_q.reply_text("🚫 Ваш аккаунт заблокирован. Обратитесь к администратору.")
        return True
    return False

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    user_data = await fw.db.get_user(uid)
    if not user_data:
        await fw.db.create_user(uid, user.username or "", user.first_name or "", user.last_name or "")
        user_data = await fw.db.get_user(uid)
    if ctx.args:
        param = ctx.args[0]
        if param.startswith("ref_"):
            try:
                ref_id = int(param.split("_")[1])
                if ref_id != uid: await fw.db.process_referral(uid, ref_id)
            except: pass
        elif param.startswith("mirror_"):
            code = param.replace("mirror_", "")
            creator = await fw.db.activate_mirror(uid, code)
            if creator: await update.message.reply_text(f"🪞 Зеркало активировано!")
    
    balance = "♾️ Admin" if user_data.get('is_admin') else user_data['requests_left']
    await update.message.reply_text(
        f"🕸️ **OSINT v15.2 ENTERPRISE**\n👤 **ID:** `{uid}`\n💰 **Баланс:** {balance}\n🔧 **Статус:** {' Admin' if user_data.get('is_admin') else 'User'}\n🕸️ Выберите инструмент:",
        reply_markup=KB_MAIN, parse_mode="Markdown"
    )

async def balance_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if await check_banned(uid, update.message): return
    user_data = await fw.db.get_user(uid)
    if not user_data: return await update.message.reply_text("🕸️ Сначала нажмите /start")
    balance = "️ Admin/Premium" if user_data.get('is_admin') or user_data.get('is_premium') else user_data['requests_left']
    rate_stats = fw.limiter.get_stats(uid)
    await update.message.reply_text(
        f"📊 **ВАШ БАЛАНС**\n\n"
        f"💰 Доступно запросов: **{balance}**\n"
        f"📈 Всего использовано: **{user_data.get('total_requests', 0)}**\n"
        f"👑 Premium: **{'✅ Да' if user_data.get('is_premium') else '❌ Нет'}**\n"
        f"🚫 Забанен: **{'✅ Да' if user_data.get('is_banned') else '❌ Нет'}**\n\n"
        f"📡 **Лимиты:**\n"
        f"• В минуту: {rate_stats['minute']}\n"
        f"• В час: {rate_stats['hourly']}\n"
        f"• В день: {rate_stats['daily']}",
        parse_mode="Markdown"
    )

async def ban_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS: return await update.message.reply_text("❌ Доступ запрещён!")
    if len(ctx.args) < 1: return await update.message.reply_text("🚫 Использование: `/ban user_id [причина]`", parse_mode="Markdown")
    try:
        target_id = int(ctx.args[0])
        reason = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else "Нарушение правил"
        if await fw.db.ban_user(target_id, reason):
            await update.message.reply_text(f"✅ Пользователь `{target_id}` заблокирован.\nПричина: {reason}", parse_mode="Markdown")
            try: await ctx.bot.send_message(target_id, f"🚫 Ваш аккаунт заблокирован.\nПричина: {reason}")
            except: pass
        else: await update.message.reply_text("❌ Не удалось заблокировать пользователя")
    except ValueError: await update.message.reply_text("❌ Неверный формат ID")
    except Exception as e: await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def unban_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS: return await update.message.reply_text(" Доступ запрещён!")
    if len(ctx.args) < 1: return await update.message.reply_text("✅ Использование: `/unban user_id`", parse_mode="Markdown")
    try:
        target_id = int(ctx.args[0])
        if await fw.db.unban_user(target_id):
            await update.message.reply_text(f"✅ Пользователь `{target_id}` разблокирован", parse_mode="Markdown")
            try: await ctx.bot.send_message(target_id, "✅ Ваш аккаунт разблокирован")
            except: pass
        else: await update.message.reply_text("❌ Не удалось разблокировать пользователя")
    except ValueError: await update.message.reply_text("❌ Неверный формат ID")
    except Exception as e: await update.message.reply_text(f"❌ Ошибка: {str(e)}")

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id
    if await check_banned(uid, q, is_callback=True): return

    if data == "menu":
        return await q.edit_message_text("🕸️ **OSINT v15.2:**", reply_markup=KB_MAIN, parse_mode="Markdown")
    if data == "balance":
        return await balance_cmd(update, ctx)
    if data == "admin_start":
        if uid not in ADMIN_IDS: return await q.answer("❌ Доступ запрещён!", show_alert=True)
        return await q.edit_message_text("🔐 **АДМИН-ПАНЕЛЬ**\nНажмите Войти", reply_markup=KB_ADMIN_LOGIN)
    if data == "admin_enter_pass":
        if uid not in ADMIN_IDS: return await q.answer("❌", show_alert=True)
        admin_sessions[uid] = {"state": "wait_pass"}
        return await q.edit_message_text("🔐 **Введите пароль:**\nОтправьте сообщением", reply_markup=KB_BACK)
    if data == "admin_stats":
        if admin_sessions.get(uid, {}).get("state") != "authorized": return await q.answer("❌ Авторизуйтесь!", show_alert=True)
        stats = await fw.db.get_stats()
        breach_stats = await fw.breach_db.get_stats()
        return await q.edit_message_text(
            f"📊 **СТАТИСТИКА БОТА**\n👥 Пользователи: {stats['total_users']}\n🟢 Активные (24ч): {stats['active_24h']}\n"
            f"💰 Premium: {stats['premium_users']}\n🚫 Забанено: {stats['banned_users']}\n"
            f"🔍 Запросов: {stats['total_requests']}\n📅 Сегодня: {stats['today_requests']}\n"
            f"🗄️ Breaches: {breach_stats['breaches']}\n📱 Phones: {breach_stats['phones']}\n⏱️ Аптайм: {stats['uptime']}",
            reply_markup=KB_ADMIN, parse_mode="Markdown"
        )
    if data == "admin_users":
        if admin_sessions.get(uid, {}).get("state") != "authorized": return await q.answer("❌", show_alert=True)
        users = await fw.db.get_all_users(limit=30)
        msg = "👥 **ПОЛЬЗОВАТЕЛИ**\n"
        for u in users:
            status = "🚫" if u.get('is_banned') else "✅"
            prem = "👑" if u.get('is_premium') else ""
            msg += f"{status}{prem} `{u['id']}` - @{u.get('username') or 'N/A'} ({u['requests_left']} зап.)\n"
        return await q.edit_message_text(msg, reply_markup=KB_ADMIN, parse_mode="Markdown")
    if data == "admin_logout":
        admin_sessions.pop(uid, None)
        return await q.edit_message_text("🔒 Выход из админки", reply_markup=KB_MAIN)
    if data.startswith("tool_"):
        tool = data.replace("tool_", "")
        prompts = {
            "ip": "🕸️ **IPv4:**\nВведите IP адрес (например: 8.8.8.8)",
            "phone": "🕸️ **Телефон:**\nВведите номер в формате +7...",
            "nick": "🕸️ **Никнейм:**\nВведите ник для поиска",
            "email": "🕸️ **Email:**\nВведите email адрес",
            "breach": "🕸️ **Email для поиска:**\nВведите email для проверки в базах",
            "discord": "🕸️ **Discord ID:**\nВведите числовой ID (17-19 цифр)",
            "dns": "🕸️ **Домен:**\nВведите домен (например: google.com)",
            "ton": "🕸️ **TON адрес:**\nВведите адрес кошелька",
            "exif": "📷 **Отправьте фото/файл:**\nДля чтения метаданных",
        }
        if tool in prompts:
            user_states[uid] = {"tool": tool}
            return await q.edit_message_text(prompts[tool], reply_markup=KB_BACK, parse_mode="Markdown")

async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip() if update.message.text else ""
    
    if await check_banned(uid, update.message): return

    # Проверка пароля админа
    if admin_sessions.get(uid, {}).get("state") == "wait_pass":
        if text == ADMIN_PASSWORD:
            admin_sessions[uid] = {"state": "authorized", "time": datetime.now()}
            return await update.message.reply_text("✅ **Авторизация успешна!**", reply_markup=KB_ADMIN, parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Неверный пароль!")
            admin_sessions.pop(uid, None)
            return

    # Обработка инструмента
    if uid in user_states:
        tool = user_states[uid]["tool"]
        await update.message.reply_text("🕸️ Обработка...")
        start_time = time.time()
        try:
            allowed, reason = fw.limiter.allow(uid, tool)
            if not allowed: return await update.message.reply_text(f"🕸️ {reason}")
            can_use, reason = await fw.db.use_request(uid)
            if not can_use:
                user_data = await fw.db.get_user(uid)
                if not user_data or user_data.get('is_banned'): return await update.message.reply_text("🕸️ Вы заблокированы!")
                return await update.message.reply_text(f"️ {reason}\n💡 Пополните баланс: /referral или /mirror")

            func = fw.plugins.get(tool)
            res = ""
            if tool == "tg_id":
                res = await func(text, ctx.bot)
            elif tool == "exif":
                file_id = None
                if update.message.photo:
                    file_id = update.message.photo[-1].file_id
                elif update.message.document and update.message.document.mime_type.startswith('image/'):
                    file_id = update.message.document.file_id
                else:
                    res = "️ Отправьте изображение (JPG, PNG, TIFF) как фото или файл."
                    await update.message.reply_text(res)
                    return
                res = await func(file_id, ctx.bot)
            else:
                res = await func(text) if asyncio.iscoroutinefunction(func) else func(text)

            response_time = time.time() - start_time
            user_data = await fw.db.get_user(uid)
            bal = "♾️" if user_data.get('is_admin') else user_data['requests_left']
            
            if len(res) > 4000:
                for chunk in [res[i:i+4000] for i in range(0, len(res), 4000)]:
                    await update.message.reply_text(chunk, parse_mode="Markdown")
            else:
                await update.message.reply_text(f"{res}\n💰 **Осталось:** {bal}", parse_mode="Markdown")
            await fw.db.log(uid, tool, text if tool != "exif" else "photo", "ok", response_time)
        except Exception as e:
            logger.error(f"Plugin error {tool}: {traceback.format_exc()}")
            await update.message.reply_text(f"🕸️ Ошибка: {str(e)}")
            await fw.db.log(uid, tool, text, f"error:{str(e)}")
        finally:
            user_states.pop(uid, None)
            await update.message.reply_text("🕸️ Следующий запрос:", reply_markup=KB_MAIN)
        return

    await update.message.reply_text("🕸️ Используйте кнопки меню 👇", reply_markup=KB_MAIN)

async def main():
    await fw.init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, message_handler))
    logger.info("🕸️ OSINT v15.2 ENTERPRISE started!")
    
    if os.environ.get('RENDER') or os.environ.get('PORT'):
        from aiohttp import web
        async def handle(request): return web.Response(text="OSINT v15.2 ENTERPRISE 🕸️")
        web_app = web.Application()
        web_app.router.add_get('/', handle)
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', PORT)))
        await site.start()
        
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    try: await asyncio.Event().wait()
    except KeyboardInterrupt: logger.info("️ Stopping...")
    finally:
        await app.stop()
        await app.shutdown()
        await fw.http.close()

if __name__ == "__main__":
    asyncio.run(main())