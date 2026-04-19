#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OSINT FRAMEWORK v8.0
Monetization + Referrals + Mirrors + Telegram Stars
"""
import asyncio
import logging
import time
import re
import io
import os
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import aiohttp
import phonenumbers
from phonenumbers import geocoder, carrier, timezone
import aiosqlite

# ==========================================================
# 🔧 КОНФИГУРАЦИЯ
# ==========================================================
BOT_TOKEN = "8586981878:AAH8w1r9CqLB9QndBjdpSXWc_b9uk_qC3c4"
ADMIN_IDS = [8449965783]
DB_PATH = "osint_framework.db"
DATABASE_FOLDER = Path("database")
RATE_LIMIT = 10
CACHE_TTL = 300

# 💰 ЦЕНЫ И БОНУСЫ
FREE_REQUESTS = 10  # Бесплатно при старте
MIRROR_BONUS = 5    # За создание зеркала
REFERRAL_BONUS = 5  # За приглашённого друга
PRICE_PER_REQUEST = 2  # Рублей за запрос (в звёздах ~1 звезда = 1.5₽)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("OSINT")

# ==========================================================
# 🗄️ БАЗА ДАННЫХ С МОНЕТИЗАЦИЕЙ
# ==========================================================
class BotDatabase:
    def __init__(self, path: str):
        self.path = path

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
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_active TEXT,
                is_premium BOOLEAN DEFAULT FALSE
            );
            CREATE TABLE IF NOT EXISTS mirrors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                mirror_url TEXT UNIQUE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER,
                referred_id INTEGER UNIQUE,
                bonus_given BOOLEAN DEFAULT FALSE,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (referrer_id) REFERENCES users(id),
                FOREIGN KEY (referred_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                type TEXT,
                amount INTEGER,
                requests_added INTEGER,
                telegram_payment_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plugin TEXT,
                query TEXT,
                status TEXT,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT,
                expires_at REAL
            );
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """)
            await db.commit()

    async def get_user(self, user_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def create_user(self, user_id: int, username: str, first: str, last: str):
        async with aiosqlite.connect(self.path) as db:
            referral_code = f"ref_{user_id}_{secrets.token_hex(4)}"
            await db.execute(
                "INSERT INTO users (id, username, first_name, last_name, referral_code, last_active) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, first, last, referral_code, datetime.now().isoformat()))
            await db.commit()

    async def update_user(self, user_id: int, **kwargs):
        async with aiosqlite.connect(self.path) as db:
            fields = ", ".join(f"{k} = ?" for k in kwargs.keys())
            values = list(kwargs.values()) + [user_id]
            await db.execute(f"UPDATE users SET {fields} WHERE id = ?", values)
            await db.commit()

    async def add_requests(self, user_id: int, amount: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET requests_left = requests_left + ?, total_requests = total_requests + ? WHERE id = ?",
                (amount, amount, user_id))
            await db.commit()

    async def use_request(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT requests_left FROM users WHERE id = ?", (user_id,))
            row = await cursor.fetchone()
            if row and row[0] > 0:
                await db.execute("UPDATE users SET requests_left = requests_left - 1 WHERE id = ?", (user_id,))
                await db.commit()
                return True
            return False

    async def get_referral_code(self, user_id: int) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT referral_code FROM users WHERE id = ?", (user_id,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def process_referral(self, new_user_id: int, referrer_id: int) -> bool:
        """Обработка реферала. Возвращает True если бонус начислен."""
        if new_user_id == referrer_id:
            return False
        
        async with aiosqlite.connect(self.path) as db:
            # Проверяем не был ли уже реферал
            cursor = await db.execute("SELECT id FROM referrals WHERE referred_id = ?", (new_user_id,))
            if await cursor.fetchone():
                return False
            
            # Добавляем реферала
            await db.execute(
                "INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
                (referrer_id, new_user_id))
            
            # Начисляем бонус рефереру
            await db.execute(
                "UPDATE users SET requests_left = requests_left + ? WHERE id = ?",
                (REFERRAL_BONUS, referrer_id))
            
            # Привязываем нового пользователя к рефереру
            await db.execute(
                "UPDATE users SET referred_by = ? WHERE id = ?",
                (referrer_id, new_user_id))
            
            await db.commit()
            return True

    async def create_mirror(self, user_id: int) -> str:
        """Создаёт зеркало (реферальную ссылку). Возвращает URL."""
        mirror_code = secrets.token_urlsafe(8)
        mirror_url = f"https://t.me/{(await self.get_bot_username())}?start=mirror_{mirror_code}"
        
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO mirrors (user_id, mirror_url) VALUES (?, ?)",
                (user_id, mirror_url))
            await db.execute(
                "UPDATE users SET requests_left = requests_left + ? WHERE id = ?",
                (MIRROR_BONUS, user_id))
            await db.commit()
        
        return mirror_url

    async def process_mirror_activation(self, user_id: int, mirror_code: str):
        """Активация зеркала. Начисляет бонус создателю зеркала."""
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "SELECT user_id FROM mirrors WHERE mirror_url LIKE ? AND is_active = TRUE",
                (f"%{mirror_code}%",))
            row = await cursor.fetchone()
            if row:
                creator_id = row[0]
                if creator_id != user_id:  # Не самому себе
                    await db.execute(
                        "UPDATE users SET requests_left = requests_left + ? WHERE id = ?",
                        (MIRROR_BONUS, creator_id))
                    await db.commit()

    async def log_transaction(self, user_id: int, type_: str, amount: int, requests: int, payment_id: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO transactions (user_id, type, amount, requests_added, telegram_payment_id) VALUES (?, ?, ?, ?, ?)",
                (user_id, type_, amount, requests, payment_id))
            await db.commit()

    async def log(self, user_id: int, plugin: str, query: str, status: str = "ok"):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO logs (user_id, plugin, query, status) VALUES (?, ?, ?, ?)",
                             (user_id, plugin, query, status))
            await db.commit()

    async def is_blacklisted(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,))
            return await cursor.fetchone() is not None

    async def get_cache(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT value FROM cache WHERE key = ? AND expires_at > ?",
                                      (key, time.time()))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_cache(self, key: str, value: str, ttl: int = CACHE_TTL):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                             (key, value, time.time() + ttl))
            await db.commit()

    async def get_bot_username(self) -> str:
        """Получает username бота"""
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("SELECT username FROM users WHERE id = ? LIMIT 1", (ADMIN_IDS[0],))
            row = await cursor.fetchone()
            return row[0] if row else "osint_bot"

# ==========================================================
# 🗃️ СИСТЕМА БАЗ УТЕЧЕК (без изменений)
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
                email TEXT,
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
                phone TEXT,
                name TEXT,
                address TEXT,
                source TEXT,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_phone ON phones(phone)")
            await db.commit()
        logger.info(f"🕸️ Database folder: {self.db_folder.absolute()}")

    async def search_email(self, email: str) -> List[dict]:
        results = []
        try:
            async with aiosqlite.connect(self.breach_db) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT email, password, source, breach_date FROM breaches WHERE email = ? LIMIT 50",
                    (email.lower(),))
                for row in await cursor.fetchall():
                    results.append(dict(row))
        except Exception as e:
            logger.error(f"DB error: {e}")
        if self.combo_json.exists():
            try:
                with open(self.combo_json, 'r', encoding='utf-8') as f:
                    for entry in json.load(f):
                        if entry.get("email", "").lower() == email.lower():
                            results.append(entry)
            except Exception as e:
                logger.error(f"JSON error: {e}")
        return results

    async def search_phone(self, phone: str) -> List[dict]:
        results = []
        phone_clean = re.sub(r'[^\d+]', '', phone)
        try:
            async with aiosqlite.connect(self.phone_db) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT phone, name, address, source FROM phones WHERE phone LIKE ? OR phone LIKE ? LIMIT 20",
                    (f"%{phone_clean[-10:]}%", f"%{phone}%"))
                for row in await cursor.fetchall():
                    results.append(dict(row))
        except Exception as e:
            logger.error(f"Phone DB error: {e}")
        return results

    async def get_stats(self) -> dict:
        stats = {"breaches": 0, "phones": 0, "combos": 0}
        try:
            async with aiosqlite.connect(self.breach_db) as db:
                c = await db.execute("SELECT COUNT(*) FROM breaches")
                stats["breaches"] = (await c.fetchone())[0]
        except: pass
        try:
            async with aiosqlite.connect(self.phone_db) as db:
                c = await db.execute("SELECT COUNT(*) FROM phones")
                stats["phones"] = (await c.fetchone())[0]
        except: pass
        if self.combo_json.exists():
            try:
                with open(self.combo_json, 'r') as f:
                    stats["combos"] = len(json.load(f))
            except: pass
        return stats

# ==========================================================
# 🌐 HTTP КЛИЕНТ (без изменений)
# ==========================================================
class AsyncHTTP:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        return self._session

    async def get_json(self, url: str, headers: dict = None) -> dict:
        s = await self.get_session()
        async with s.get(url, headers=headers) as r:
            r.raise_for_status()
            return await r.json()

    async def get_text(self, url: str, headers: dict = None) -> str:
        s = await self.get_session()
        async with s.get(url, headers=headers) as r:
            r.raise_for_status()
            return await r.text()

    async def head(self, url: str) -> int:
        s = await self.get_session()
        try:
            async with s.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=4)) as r:
                return r.status
        except:
            return 0

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

# ==========================================================
# ⏱️ RATE LIMITER (без изменений)
# ==========================================================
class RateLimiter:
    def __init__(self, max_req: int, window: int = 60):
        self.max, self.window, self.reqs = max_req, window, {}

    def allow(self, uid: int) -> bool:
        now = time.time()
        self.reqs.setdefault(uid, [])
        self.reqs[uid] = [t for t in self.reqs[uid] if now - t < self.window]
        if len(self.reqs[uid]) >= self.max:
            return False
        self.reqs[uid].append(now)
        return True

    def remaining(self, uid: int) -> int:
        now = time.time()
        valid = [t for t in self.reqs.get(uid, []) if now - t < self.window]
        return max(0, self.max - len(valid))

# ==========================================================
# 🎮 DISCORD UTILS (без изменений)
# ==========================================================
class DiscordUtils:
    EPOCH = 1420070400000

    @staticmethod
    def decode_snowflake(sid: int) -> dict:
        try:
            ts = ((sid >> 22) + DiscordUtils.EPOCH) / 1000
            created = datetime.fromtimestamp(ts)
            return {
                "created_at": created.strftime("%Y-%m-%d %H:%M:%S UTC"),
                "age_days": (datetime.now() - created).days,
                "internal_id": sid & 0xFFFFFF,
                "process_id": (sid >> 17) & 0x1F,
                "worker_id": (sid >> 22) & 0x1F,
                "unix_ts": int(ts)
            }
        except:
            return None

    @staticmethod
    def avatar_url(uid: int, hsh: str = None, bot: bool = False) -> str:
        if hsh:
            ext = "gif" if hsh.startswith("a_") else "png"
            return f"https://cdn.discordapp.com/avatars/{uid}/{hsh}.{ext}"
        return f"https://cdn.discordapp.com/embed/avatars/{0 if bot else uid % 5}.png"

# ==========================================================
# 🕵️ OSINT FRAMEWORK
# ==========================================================
class OSINTFramework:
    def __init__(self):
        self.db = BotDatabase(DB_PATH)
        self.breach_db = BreachDatabase(DATABASE_FOLDER)
        self.http = AsyncHTTP()
        self.limiter = RateLimiter(RATE_LIMIT)
        self.discord = DiscordUtils()
        self.plugins = {}

    async def init(self):
        await self.db.init()
        await self.breach_db.init()
        self.register_plugins()
        logger.info("🕸️ Framework initialized")

    def register_plugins(self):
        self.plugins.update({
            "ip": self.plugin_ip,
            "dns": self.plugin_dns,
            "whois": self.plugin_whois,
            "subs": self.plugin_subs,
            "nick": self.plugin_nick,
            "phone": self.plugin_phone,
            "email": self.plugin_email,
            "breach": self.plugin_breach,
            "ton": self.plugin_ton,
            "tg_id": self.plugin_tg_id,
            "geo": self.plugin_geo,
            "discord": self.plugin_discord
        })
        logger.info(f"🕸️ Registered {len(self.plugins)} plugins")

    # ─────────── ПЛАГИНЫ (те же что были) ───────────
    async def plugin_ip(self, query: str) -> str:
        if not re.match(r"^(\d{1,3}\.){3}\d{1,3}$", query):
            return "🕸️ Неверный формат IPv4"
        cached = await self.db.get_cache(f"ip:{query}")
        if cached:
            return cached
        data = await self.http.get_json(f"http://ip-api.com/json/{query}?fields=4194303")
        if data.get("status") == "fail":
            return f"🕸️ {data.get('message', 'Ошибка')}"
        lat = data.get('lat', 0)
        lon = data.get('lon', 0)
        google_maps = f"https://www.google.com/maps?q={lat},{lon}"
        res = (f"🕸️ **IP:** `{data['query']}`\n"
               f"🕸️ **Страна:** {data.get('country')} ({data.get('countryCode')})\n"
               f"🕸️ **Регион:** {data.get('regionName')}\n"
               f"🕸️ **Город:** {data.get('city')}\n"
               f"🕸️ **Индекс:** {data.get('zip', 'N/A')}\n"
               f"🕸️ **Координаты:** `{lat}, {lon}`\n"
               f"🕸️ **Карта:** [Google Maps]({google_maps})\n"
               f"🕸️ **TZ:** {data.get('timezone')}\n"
               f"🕸️ **ISP:** `{data.get('isp')}`\n"
               f"🕸️ **Org:** `{data.get('org')}`\n"
               f"🕸️ **AS:** `{data.get('as')}`\n"
               f"🕸️ **Детекция:** Мобильный: {'🕸️' if data.get('mobile') else '❌'} | "
               f"Proxy/VPN: {'🕸️' if data.get('proxy') else '❌'} | "
               f"Хостинг: {'🕸️' if data.get('hosting') else '❌'}")
        await self.db.set_cache(f"ip:{query}", res)
        return res

    async def plugin_dns(self, query: str) -> str:
        cached = await self.db.get_cache(f"dns:{query}")
        if cached:
            return cached
        data = await self.http.get_json(f"https://dns.google/resolve?name={query}&type=A")
        answers = data.get("Answer", [])
        if not answers:
            return f"🕸️ DNS записи для `{query}` не найдены"
        res = f"🕸️ **DNS для `{query}`:**\n"
        for a in answers[:15]:
            res += f"🕸️ `{a['data']}` (TTL: {a['TTL']})\n"
        await self.db.set_cache(f"dns:{query}", res)
        return res

    async def plugin_whois(self, query: str) -> str:
        cached = await self.db.get_cache(f"whois:{query}")
        if cached:
            return cached
        text = await self.http.get_text(f"https://api.hackertarget.com/whois/?q={query}")
        if "error" in text.lower() or len(text) < 100:
            return "🕸️ WHOIS скрыт или домен не найден"
        res = f"🕸️ **WHOIS `{query}`:**\n```{text[:3500]}```" if len(text) > 3500 else f"🕸️ **WHOIS `{query}`:**\n```{text}```"
        await self.db.set_cache(f"whois:{query}", res, 86400)
        return res

    async def plugin_subs(self, query: str) -> str:
        cached = await self.db.get_cache(f"subs:{query}")
        if cached:
            return cached
        try:
            data = await self.http.get_json(f"https://crt.sh/?q={query}&output=json")
            subs = {s.strip() for e in data for s in e.get("name_value", "").split("\n") if s.strip() and "*" not in s}
            if not subs:
                return f"🕸️ Субдомены для `{query}` не найдены"
            res = f"🕸️ **Субдомены `{query}` (Top 30):**\n" + "\n".join(f"🕸️ `{s}`" for s in sorted(subs)[:30])
            if len(subs) > 30:
                res += f"\n... и ещё {len(subs)-30}"
            await self.db.set_cache(f"subs:{query}", res, 43200)
            return res
        except Exception as e:
            return f"🕸️ Ошибка: {e}"

    async def plugin_nick(self, query: str) -> str:
        platforms = {
            "VK": f"https://vk.com/{query}", "Telegram": f"https://t.me/{query}",
            "Instagram": f"https://instagram.com/{query}", "Twitter/X": f"https://twitter.com/{query}",
            "TikTok": f"https://tiktok.com/@{query}", "Reddit": f"https://reddit.com/user/{query}",
            "YouTube": f"https://youtube.com/@{query}", "Twitch": f"https://twitch.tv/{query}",
            "GitHub": f"https://github.com/{query}", "GitLab": f"https://gitlab.com/{query}",
            "Steam": f"https://steamcommunity.com/id/{query}", "SoundCloud": f"https://soundcloud.com/{query}",
            "LinkedIn": f"https://linkedin.com/in/{query}", "Pinterest": f"https://pinterest.com/{query}",
            "Spotify": f"https://open.spotify.com/user/{query}", "Discord": f"https://discord.com/users/{query}",
            "Minecraft": f"https://namemc.com/profile/{query}", "Roblox": f"https://roblox.com/user.aspx?username={query}",
            "Pastebin": f"https://pastebin.com/u/{query}", "LastFM": f"https://last.fm/user/{query}",
            "Threads": f"https://threads.net/@{query}", "Mastodon": f"https://mastodon.social/@{query}",
            "Snapchat": f"https://snapchat.com/add/{query}", "Behance": f"https://behance.net/{query}",
            "HackerRank": f"https://hackerrank.com/{query}", "Chess.com": f"https://chess.com/member/{query}",
            "Lichess": f"https://lichess.org/@{query}", "Bandcamp": f"https://bandcamp.com/{query}",
            "Mixcloud": f"https://mixcloud.com/{query}", "IndieHackers": f"https://indiehackers.com/@{query}",
            "CoinMarketCap": f"https://coinmarketcap.com/community/profile/{query}",
            "PayPal": f"https://paypal.me/{query}", "Badoo": f"https://badoo.com/profile/{query}",
            "Tinder": f"https://tinder.com/@{query}", "500px": f"https://500px.com/{query}",
            "Slideshare": f"https://slideshare.net/{query}", "Wattpad": f"https://wattpad.com/user/{query}",
            "Linktree": f"https://linktr.ee/{query}", "WordPress": f"https://{query}.wordpress.com",
            "MyAnimeList": f"https://myanimelist.net/profile/{query}",
        }
        tasks = [self.http.head(url) for url in platforms.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        found, not_found = [], []
        for (name, url), res in zip(platforms.items(), results):
            if isinstance(res, int) and res in (200, 301, 302, 307):
                found.append(f"🕸️ {name}: `{url}`")
            else:
                not_found.append(f"❌ {name}")
        res = f"🕸️ **Никнейм:** `{query}`\n"
        res += f"🕸️ **Найдено:** {len(found)} | ❌ **Не найдено:** {len(not_found)}\n"
        if found:
            res += "**🕸️ Активные профили:**\n" + "\n".join(found[:30])
            if len(found) > 30:
                res += f"\n... и ещё {len(found)-30}"
        return res

    def plugin_phone(self, query: str) -> str:
        try:
            p = phonenumbers.parse(query, None)
            if not phonenumbers.is_valid_number(p):
                return "🕸️ Номер невалиден"
            region_code = phonenumbers.region_code_for_number(p)
            region_name = geocoder.description_for_number(p, "ru")
            carr = carrier.name_for_number(p, "ru") or "Не определен"
            tz = timezone.time_zones_for_number(p)
            fmt = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            num_type = phonenumbers.number_type(p)
            type_map = {
                phonenumbers.PhoneNumberType.MOBILE: "Мобильный",
                phonenumbers.PhoneNumberType.FIXED_LINE: "Городской",
                phonenumbers.PhoneNumberType.VOIP: "VoIP",
                phonenumbers.PhoneNumberType.TOLL_FREE: "Бесплатный",
                phonenumbers.PhoneNumberType.PREMIUM_RATE: "Премиум"
            }
            num_type_str = type_map.get(num_type, "Другой")
            return (f"🕸️ **Номер:** `{fmt}`\n"
                    f"🕸️ **Страна:** {region_name}\n"
                    f"🕸️ **Регион:** {region_code}\n"
                    f"🕸️ **Оператор:** {carr}\n"
                    f"🕸️ **TZ:** {tz[0] if tz else 'N/A'}\n"
                    f"🕸️ **Тип:** {num_type_str}")
        except Exception as e:
            return f"🕸️ Ошибка: {e}. Формат: +7..."

    def plugin_email(self, query: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", query):
            return "🕸️ Неверный формат"
        domain = query.split("@")[1]
        disp = ["tempmail.com", "guerrillamail.com", "mailinator.com", "yopmail.com", "sharklasers.com", "10minutemail.com"]
        warn = "🕸️ Временный домен" if domain.lower() in disp else "🕸️ Домен надёжный"
        return f"🕸️ **Email:** `{query}`\n🕸️ Домен: `{domain}`\n{warn}"

    async def plugin_breach(self, query: str) -> str:
        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", query):
            return "🕸️ Введите корректный email"
        results = await self.breach_db.search_email(query)
        if not results:
            return f"🕸️ **Email:** `{query}`\n🕸️ Не найден в базах утечек"
        sources = {}
        for r in results:
            src = r.get("source", "Unknown")
            if src not in sources:
                sources[src] = []
            sources[src].append(r)
        res = f"🕸️ **Email:** `{query}`\n"
        res += f"🕸️ **Найдено утечек:** {len(results)}\n"
        res += f"🕸️ **Источников:** {len(sources)}\n"
        for source, breaches in list(sources.items())[:5]:
            res += f"🕸️ **{source}** ({len(breaches)} записей):\n"
            for b in breaches[:3]:
                pwd = b.get("password", "N/A")
                masked_pwd = pwd[:2] + "*" * (len(pwd) - 2) if len(pwd) > 2 else "***"
                res += f"   • `{masked_pwd}` ({b.get('date', 'N/A')})\n"
            if len(breaches) > 3:
                res += f"   ... и ещё {len(breaches)-3}\n"
            res += "\n"
        if len(sources) > 5:
            res += f"... и ещё {len(sources)-5} источников\n"
        res += "\n🕸️ **Рекомендации:**\n"
        res += "1. Смените пароли на всех сервисах\n"
        res += "2. Включите 2FA\n"
        res += "3. Используйте уникальный пароль для каждого сервиса"
        return res

    async def plugin_ton(self, query: str) -> str:
        cached = await self.db.get_cache(f"ton:{query}")
        if cached:
            return cached
        try:
            data = await self.http.get_json(f"https://toncenter.com/api/v2/getAddressInformation?address={query}")
            if data.get("ok"):
                bal = int(data["result"].get("balance", 0)) / 1e9
                is_wallet = "b5ee9c72" in data["result"].get("code", "")
                res = f"🕸️ **TON:** `{query}`\n"
                res += f"🕸️ **Баланс:** `{bal:.4f} TON`\n"
                res += f"🕸️ **Тип:** {'Кошелёк' if is_wallet else 'Обычный адрес'}"
                await self.db.set_cache(f"ton:{query}", res, 60)
                return res
            return "🕸️ Адрес не найден"
        except Exception as e:
            return f"🕸️ Ошибка TON: {e}"

    async def plugin_tg_id(self, query: str, bot) -> str:
        try:
            clean = query.replace("@", "").strip()
            if not clean:
                return "🕸️ Введите username"
            try:
                chat = await bot.get_chat(f"@{clean}")
                return (f"🕸️ **Telegram:** `{clean}`\n"
                        f"🕸️ **ID:** `{chat.id}`\n"
                        f"🕸️ **Имя:** {chat.first_name or ''} {chat.last_name or ''}\n"
                        f"🕸️ **Username:** @{chat.username or 'N/A'}\n"
                        f"🕸️ **Бот:** {'Да' if chat.is_bot else 'Нет'}\n"
                        f"🕸️ **Bio:** {chat.bio or 'Нет'}\n"
                        f"🕸️ **Фото:** {'Есть' if chat.photo else 'Нет'}")
            except Exception as e1:
                return (f"🕸️ **Telegram:** `{clean}`\n"
                        f"🕸️ Пользователь не найден ботом.\n"
                        f"🕸️ **Причины:**\n"
                        f"• Никогда не писал боту\n"
                        f"• Приватный аккаунт\n"
                        f"• Username изменён\n"
                        f"🕸️ Попробуйте @userinfobot")
        except Exception as e:
            return f"🕸️ Ошибка: {e}"

    async def plugin_geo(self, query: str) -> str:
        parts = query.split()
        if len(parts) != 2:
            return "🕸️ Формат: `lat lon` (55.7558 37.6173)"
        cached = await self.db.get_cache(f"geo:{query}")
        if cached:
            return cached
        try:
            headers = {"User-Agent": "OSINT_FW/1.0"}
            data = await self.http.get_json(
                f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={parts[0]}&lon={parts[1]}",
                headers=headers
            )
            addr = data.get("address", {})
            res = (f"🕸️ **Координаты:** `{parts[0]}, {parts[1]}`\n"
                   f"🕸️ **Адрес:** `{data.get('display_name', 'Не найден')}`\n"
                   f"🕸️ Город: {addr.get('city', addr.get('town', 'N/A'))}\n"
                   f"🕸️ Улица: {addr.get('road', 'N/A')}\n"
                   f"🕸️ Дом: {addr.get('house_number', 'N/A')}\n"
                   f"🕸️ Индекс: {addr.get('postcode', 'N/A')}\n"
                   f"🕸️ Страна: {addr.get('country', 'N/A')}")
            await self.db.set_cache(f"geo:{query}", res, 86400)
            return res
        except Exception as e:
            return f"🕸️ Ошибка гео: {e}"

    async def plugin_discord(self, query: str) -> str:
        try:
            if re.match(r"^\d{17,19}$", query):
                user_id = int(query)
                decoded = self.discord.decode_snowflake(user_id)
                if not decoded:
                    return "🕸️ Неверный Discord ID"
                avatar_url = self.discord.avatar_url(user_id)
                res = (f"🕸️ **Discord User**\n"
                       f"🕸️ **User ID:** `{user_id}`\n"
                       f"🕸️ **Internal ID:** `{decoded['internal_id']}`\n"
                       f"🕸️ **Worker ID:** `{decoded['worker_id']}`\n"
                       f"🕸️ **Process ID:** `{decoded['process_id']}`\n"
                       f"🕸️ **Дата создания:** `{decoded['created_at']}`\n"
                       f"🕸️ **Возраст аккаунта:**\n"
                       f"   • Дней: `{decoded['age_days']}`\n"
                       f"   • Часов: `{decoded['age_days'] * 24}`\n"
                       f"🕸️ **Аватар:**\n"
                       f"[PNG]({avatar_url.replace('.gif', '.png')}) | "
                       f"[GIF]({avatar_url})\n"
                       f"🕸️ **Поиск:**\n"
                       f"[Google](https://google.com/search?q={user_id}) | "
                       f"[Xeno](https://discord.id/) | "
                       f"[Bin](https://discordbin.com/user/{user_id})\n"
                       f"🕸️ **Snowflake декодер:**\n"
                       f"`Timestamp: {decoded['unix_ts']}`\n"
                       f"`Epoch: {self.discord.EPOCH}`")
                return res
            else:
                return (f"🕸️ **Discord Username:** `{query}`\n"
                        f"🕸️ Для детальной информации нужен **User ID**.\n"
                        f"🕸️ **Как получить ID:**\n"
                        f"1. Включите **Режим разработчика** в настройках Discord\n"
                        f"2. ПКМ по пользователю → **Копировать ID**\n"
                        f"3. Вставьте ID в бота\n"
                        f"🕸️ Инструкция: https://support.discord.com/hc/articles/206346498")
        except Exception as e:
            return f"🕸️ Ошибка Discord: {e}"

# ==========================================================
# 🤖 TELEGRAM BOT С МОНЕТИЗАЦИЕЙ
# ==========================================================
fw = OSINTFramework()

# Клавиатуры
KB_MAIN = InlineKeyboardMarkup([
    [InlineKeyboardButton("🕸️ IP Info", callback_data="ip"), InlineKeyboardButton("🕸️ DNS/WHOIS", callback_data="dns")],
    [InlineKeyboardButton("🕸️ Nick Scan", callback_data="nick"), InlineKeyboardButton("🕸️ Phone", callback_data="phone")],
    [InlineKeyboardButton("🕸️ Email", callback_data="email"), InlineKeyboardButton("🕸️ Breach DB", callback_data="breach")],
    [InlineKeyboardButton("🕸️ TON Wallet", callback_data="ton"), InlineKeyboardButton("🕸️ TG User", callback_data="tg_id")],
    [InlineKeyboardButton("🕸️ Subdomains", callback_data="subs"), InlineKeyboardButton("🕸️ Discord", callback_data="discord")],
    [InlineKeyboardButton("🕸️ GeoINT", callback_data="geo"), InlineKeyboardButton("🕸️ DB Stats", callback_data="dbstats")],
    [InlineKeyboardButton("💰 Купить запросы", callback_data="buy"), InlineKeyboardButton("👥 Рефералы", callback_data="referral")],
    [InlineKeyboardButton("🪞 Создать зеркало", callback_data="mirror"), InlineKeyboardButton("📊 Баланс", callback_data="balance")]
])

KB_BACK = InlineKeyboardMarkup([[InlineKeyboardButton("🕸️ Меню", callback_data="menu")]])

KB_BUY = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔹 50 запросов - 100₽", callback_data="buy_50")],
    [InlineKeyboardButton("🔹 200 запросов - 300₽", callback_data="buy_200")],
    [InlineKeyboardButton("🔹 500 запросов - 600₽", callback_data="buy_500")],
    [InlineKeyboardButton("♾️ Безлимит на месяц - 999₽", callback_data="buy_unlimited")],
    [InlineKeyboardButton("🔙 Назад", callback_data="menu")]
])

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    
    # Проверяем есть ли реферальный код в start
    ref_code = None
    mirror_code = None
    
    if ctx.args:
        param = ctx.args[0]
        if param.startswith('ref_'):
            ref_code = param
        elif param.startswith('mirror_'):
            mirror_code = param.replace('mirror_', '')
    
    # Создаём или обновляем пользователя
    user_data = await fw.db.get_user(user_id)
    if not user_data:
        await fw.db.create_user(user_id, user.username or "", user.first_name or "", user.last_name or "")
        # Если есть реферальный код - обрабатываем
        if ref_code:
            # Извлекаем ID реферера из кода
            referrer_id = int(ref_code.split('_')[1])
            await fw.db.process_referral(user_id, referrer_id)
            await update.message.reply_text(f"🎉 Вы получили +{REFERRAL_BONUS} запросов по реферальной ссылке!")
        # Если активировали зеркало
        elif mirror_code:
            await fw.db.process_mirror_activation(user_id, mirror_code)
            await update.message.reply_text(f"🪞 Зеркало активировано! Создатель получил +{MIRROR_BONUS} запросов.")
    else:
        await fw.db.update_user(user_id, username=user.username or "", last_active=datetime.now().isoformat())
    
    db_stats = await fw.breach_db.get_stats()
    await update.message.reply_text(
        f"🕸️ **OSINT FRAMEWORK v8.0**\n\n"
        f"💰 **Ваш баланс:** {user_data['requests_left'] if user_data else FREE_REQUESTS} запросов\n\n"
        f"🕸️ **Базы утечек:**\n"
        f"• Email breaches: `{db_stats['breaches']:,}`\n"
        f"• Phone leaks: `{db_stats['phones']:,}`\n"
        f"• Combo lists: `{db_stats['combos']:,}`\n\n"
        f"🕸️ **Доступные модули:**\n"
        f"• 150+ платформ для ника\n"
        f"• Discord snowflake decoder\n"
        f"• Локальный поиск по базам\n\n"
        f"🎁 **Бонусы:**\n"
        f"• Приглашай друзей: +{REFERRAL_BONUS} запросов\n"
        f"• Создавай зеркала: +{MIRROR_BONUS} запросов\n\n"
        f"🕸️ **White Hat Only!**\n"
        f"Используйте только в законных целях.",
        reply_markup=KB_MAIN, parse_mode="Markdown"
    )

async def balance_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = await fw.db.get_user(user_id)
    if not user_data:
        await update.message.reply_text("🕸️ Сначала нажмите /start")
        return
    
    total_used = user_data['total_requests'] - user_data['requests_left']
    
    await update.message.reply_text(
        f"📊 **Ваш баланс:**\n\n"
        f"🔹 Доступно запросов: **{user_data['requests_left']}**\n"
        f"🔹 Всего использовано: **{total_used}**\n"
        f"🔹 Premium: **{'✅ Да' if user_data['is_premium'] else '❌ Нет'}**\n\n"
        f"💡 **Как получить ещё:**\n"
        f"• Пригласите друга: +{REFERRAL_BONUS} запросов\n"
        f"• Создайте зеркало: +{MIRROR_BONUS} запросов\n"
        f"• Купите пакет в разделе /buy",
        parse_mode="Markdown"
    )

async def buy_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 **Выберите пакет:**\n\n"
        "🔹 **50 запросов** - 100₽ (~67 звёзд)\n"
        "🔹 **200 запросов** - 300₽ (~200 звёзд)\n"
        "🔹 **500 запросов** - 600₽ (~400 звёзд)\n"
        "♾️ **Безлимит на месяц** - 999₽ (~666 звёзд)\n\n"
        "💡 Telegram Stars принимаются автоматически",
        reply_markup=KB_BUY, parse_mode="Markdown"
    )

async def referral_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ref_code = await fw.db.get_referral_code(user_id)
    
    if not ref_code:
        await update.message.reply_text("🕸️ Ошибка. Нажмите /start")
        return
    
    bot_username = (await fw.db.get_bot_username()).replace('@', '')
    ref_link = f"https://t.me/{bot_username}?start={ref_code}"
    
    await update.message.reply_text(
        f"👥 **Реферальная программа:**\n\n"
        f"Пригласите друзей и получите **{REFERRAL_BONUS} запросов** за каждого!\n\n"
        f"🔗 **Ваша ссылка:**\n`{ref_link}`\n\n"
        f"📊 **Статистика:**\n"
        f"• Приглашено: 0\n"
        f"• Получено бонусов: 0\n\n"
        f"💡 Отправьте ссылку друзьям!",
        parse_mode="Markdown"
    )

async def mirror_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = await fw.db.get_user(user_id)
    
    if not user_data:
        await update.message.reply_text("🕸️ Сначала нажмите /start")
        return
    
    # Проверяем есть ли уже активное зеркало
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT mirror_url FROM mirrors WHERE user_id = ? AND is_active = TRUE LIMIT 1",
            (user_id,))
        row = await cursor.fetchone()
    
    if row:
        await update.message.reply_text(
            f"🪞 **Ваше зеркало уже создано:**\n\n"
            f"🔗 `{row[0]}`\n\n"
            f"💡 Каждое использование даёт вам +{MIRROR_BONUS} запросов!",
            parse_mode="Markdown"
        )
        return
    
    # Создаём новое зеркало
    mirror_url = await fw.db.create_mirror(user_id)
    
    await update.message.reply_text(
        f"🪞 **Зеркало создано!**\n\n"
        f"🔗 **Ссылка:**\n`{mirror_url}`\n\n"
        f"🎁 Вы получили **+{MIRROR_BONUS} запросов**!\n\n"
        f"💡 Отправьте ссылку друзьям - за каждое использование вы получите бонус!",
        parse_mode="Markdown"
    )

# Обработчик покупок
async def buy_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    data = query.data
    
    packages = {
        'buy_50': {'requests': 50, 'price': 100, 'stars': 67},
        'buy_200': {'requests': 200, 'price': 300, 'stars': 200},
        'buy_500': {'requests': 500, 'price': 600, 'stars': 400},
        'buy_unlimited': {'requests': 999999, 'price': 999, 'stars': 666}
    }
    
    if data not in packages:
        return
    
    pkg = packages[data]
    
    # Создаём инвойс Telegram Stars
    prices = [LabeledPrice(label="Telegram Stars", amount=pkg['stars'])]
    
    await context.bot.send_invoice(
        chat_id=user_id,
        title=f"OSINT Запросы ({pkg['requests']} шт)",
        description=f"Покупка {pkg['requests']} запросов для OSINT Framework",
        payload=f"osint_{pkg['requests']}",
        provider_token="",  # Для Stars не нужен
        currency="XTR",  # Telegram Stars
        prices=prices,
        start_parameter=f"buy_{pkg['requests']}",
    )

async def pre_checkout_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    
    # Извлекаем количество запросов из payload
    try:
        requests = int(payment.invoice_payload.replace('osint_', ''))
    except:
        requests = 50
    
    # Начисляем запросы
    await fw.db.add_requests(user_id, requests)
    
    # Логируем транзакцию
    total_stars = sum(price.amount for price in update.message.successful_payment.total_amount)
    await fw.db.log_transaction(
        user_id, 
        "stars_payment", 
        total_stars, 
        requests,
        payment.telegram_payment_charge_id
    )
    
    await update.message.reply_text(
        f"✅ **Оплата прошла успешно!**\n\n"
        f"🎁 Вам начислено **{requests} запросов**!\n"
        f"💰 Списано: {total_stars} звёзд\n\n"
        f"🕸️ Теперь у вас {await (await fw.db.get_user(user_id))['requests_left']} запросов.",
        parse_mode="Markdown"
    )

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    
    if data == "menu":
        await q.edit_message_text("🕸️ **OSINT Модули:**", reply_markup=KB_MAIN)
        return
    if data == "dbstats":
        if q.from_user.id not in ADMIN_IDS:
            await q.edit_message_text("🕸️ Доступ только админам.", reply_markup=KB_BACK)
            return
        stats = await fw.breach_db.get_stats()
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT COUNT(*) FROM users")
            users = (await cur.fetchone())[0]
            cur = await db.execute("SELECT COUNT(*) FROM logs")
            logs = (await cur.fetchone())[0]
        await q.edit_message_text(
            f"🕸️ **Статистика:**\n"
            f"🕸️ **Базы утечек:**\n"
            f"• Breaches: `{stats['breaches']:,}`\n"
            f"• Phones: `{stats['phones']:,}`\n"
            f"• Combos: `{stats['combos']:,}`\n\n"
            f"🕸️ **Пользователей:** `{users}`\n"
            f"🕸️ **Запросов:** `{logs}`",
            reply_markup=KB_BACK, parse_mode="Markdown"
        )
        return
    if data == "buy":
        await buy_callback(update, ctx)
        return
    if data == "referral":
        await referral_cmd(update, ctx)
        return
    if data == "mirror":
        await mirror_cmd(update, ctx)
        return
    if data == "balance":
        await balance_cmd(update, ctx)
        return
    if data.startswith('buy_'):
        await buy_callback(update, ctx)
        return

    prompts = {
        "ip": "🕸️ Введите **IPv4**:",
        "dns": "🕸️ Введите **домен**:",
        "subs": "🕸️ Введите **домен**:",
        "nick": "🕸️ Введите **никнейм**:",
        "phone": "🕸️ Введите **номер** (+7...):",
        "email": "🕸️ Введите **email**:",
        "breach": "🕸️ Введите **email** для поиска в базах:",
        "ton": "🕸️ Введите **TON адрес**:",
        "tg_id": "🕸️ Введите **username** (без @):",
        "geo": "🕸️ Введите **координаты** `lat lon`:",
        "discord": "🕸️ Введите **Discord ID**:",
        "whois": "🕸️ Введите **домен**:"
    }
    if data in prompts:
        ctx.user_data["awaiting"] = data
        await q.edit_message_text(prompts[data], reply_markup=KB_BACK, parse_mode="Markdown")

async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tool = ctx.user_data.get("awaiting")
    
    if not tool:
        await update.message.reply_text("🕸️ Используйте кнопки меню 👇", reply_markup=KB_MAIN)
        return
    if await fw.db.is_blacklisted(uid):
        await update.message.reply_text("🕸️ Вы заблокированы.")
        ctx.user_data["awaiting"] = None
        return
    if not fw.limiter.allow(uid):
        await update.message.reply_text(f"🕸️ Лимит: {RATE_LIMIT}/мин")
        ctx.user_data["awaiting"] = None
        return
    
    # Проверяем баланс
    user_data = await fw.db.get_user(uid)
    if not user_data or user_data['requests_left'] <= 0:
        await update.message.reply_text(
            "⚠️ **Недостаточно запросов!**\n\n"
            "💡 Пополните баланс:\n"
            "• /buy - купить запросы\n"
            "• /referral - пригласить друга (+5 запросов)\n"
            "• /mirror - создать зеркало (+5 запросов)",
            reply_markup=KB_BUY,
            parse_mode="Markdown"
        )
        return
    
    await update.message.reply_text("🕸️ Обработка...")
    query = update.message.text.strip() if update.message.text else ""
    
    try:
        plugin_func = fw.plugins.get(tool)
        if not plugin_func:
            res = "🕸️ Модуль не найден"
        elif tool == "tg_id":
            res = await plugin_func(query, ctx.bot)
        else:
            res = await plugin_func(query) if asyncio.iscoroutinefunction(plugin_func) else plugin_func(query)
        
        # Списываем запрос
        await fw.db.use_request(uid)
        
        # Показываем результат
        if len(res) > 4000:
            chunks = [res[i:i+4000] for i in range(0, len(res), 4000)]
            for chunk in chunks:
                await update.message.reply_text(chunk, parse_mode="Markdown")
        else:
            # Показываем остаток запросов
            new_balance = await fw.db.get_user(uid)
            await update.message.reply_text(
                f"{res}\n\n"
                f"💰 **Осталось запросов:** {new_balance['requests_left']}",
                parse_mode="Markdown"
            )
        
        await fw.db.log(uid, tool, query if query else "photo", "ok")
    except Exception as e:
        await update.message.reply_text(f"🕸️ Ошибка: {e}")
        await fw.db.log(uid, tool, query, f"error:{e}")
    finally:
        ctx.user_data["awaiting"] = None
        await update.message.reply_text("🕸️ Следующий запрос:", reply_markup=KB_MAIN)

# ==========================================================
# 🚀 ЗАПУСК
# ==========================================================
async def main():
    await fw.init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Хендлеры
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("balance", balance_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler("referral", referral_cmd))
    app.add_handler(CommandHandler("mirror", mirror_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Оплата Telegram Stars
    app.add_handler(CallbackQueryHandler(pre_checkout_callback), group=1)
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    
    logger.info("🕸️ OSINT Framework v8.0 запущен с монетизацией!")
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("🕸️ Остановка...")
    finally:
        await app.stop()
        await app.shutdown()
        await fw.http.close()

if __name__ == "__main__":
    asyncio.run(main())