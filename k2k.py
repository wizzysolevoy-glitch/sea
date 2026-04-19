#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OSINT FRAMEWORK v7.0
Russian Interface + Render.com Compatible
"""
import asyncio
import logging
import time
import re
import io
import os
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import aiohttp
import phonenumbers
from phonenumbers import geocoder, carrier, timezone
from PIL import Image
import aiosqlite

# ==========================================================
# 🔧 КОНФИГУРАЦИЯ
# ==========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8586981878:AAH8w1r9CqLB9QndBjdpSXWc_b9uk_qC3c4")
ADMIN_IDS = [8449965783]
DB_PATH = os.getenv("DB_PATH", "osint_framework.db")
DATABASE_FOLDER = Path(os.getenv("DB_FOLDER", "database"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT", 10))
CACHE_TTL = int(os.getenv("CACHE_TTL", 300))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("OSINT")

# ==========================================================
# 🗄️ БАЗА ДАННЫХ БОТА
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
                lang TEXT DEFAULT 'ru',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_active TEXT
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
            # Добавляем колонку lang если её нет
            try:
                await db.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'ru'")
            except:
                pass
            await db.commit()

    async def log(self, user_id: int, plugin: str, query: str, status: str = "ok"):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO logs (user_id, plugin, query, status) VALUES (?, ?, ?, ?)",
                             (user_id, plugin, query, status))
            await db.commit()

    async def upsert_user(self, user_id: int, username: str, first: str, last: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO users (id, username, first_name, last_name, last_active) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET username=?, first_name=?, last_name=?, last_active=?",
                (user_id, username, first, last, datetime.now().isoformat(),
                 username, first, last, datetime.now().isoformat()))
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

# ==========================================================
# 🗃️ СИСТЕМА БАЗ УТЕЧЕК
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
# 🌐 HTTP КЛИЕНТ
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
# ⏱️ RATE LIMITER
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
# 🎮 DISCORD UTILS
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
            "exif": self.plugin_exif,
            "discord": self.plugin_discord
        })
        logger.info(f"🕸️ Registered {len(self.plugins)} plugins")

    # ─────────── ПЛАГИНЫ ───────────
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
            "MyAnimeList": f"https://myanimelist.net/profile/{query}", "Flickr": f"https://flickr.com/people/{query}",
            "Vimeo": f"https://vimeo.com/{query}", "Dribbble": f"https://dribbble.com/{query}",
            "DeviantArt": f"https://deviantart.com/{query}", "ArtStation": f"https://artstation.com/{query}",
            "Keybase": f"https://keybase.io/{query}", "Medium": f"https://medium.com/@{query}",
            "Quora": f"https://quora.com/profile/{query}", "StackOverflow": f"https://stackoverflow.com/users/{query}",
            "CodePen": f"https://codepen.io/{query}", "Replit": f"https://replit.com/@{query}",
            "HackTheBox": f"https://hackthebox.com/profile/{query}", "TryHackMe": f"https://tryhackme.com/p/{query}",
            "LeetCode": f"https://leetcode.com/{query}", "Codeforces": f"https://codeforces.com/profile/{query}",
            "AtCoder": f"https://atcoder.jp/users/{query}", "TopCoder": f"https://topcoder.com/members/{query}",
            "Bitbucket": f"https://bitbucket.org/{query}", "SourceForge": f"https://sourceforge.net/u/{query}",
            "Gitee": f"https://gitee.com/{query}", "Codewars": f"https://codewars.com/users/{query}",
            "Exercism": f"https://exercism.org/profiles/{query}", "HackerEarth": f"https://hackerearth.com/@{query}",
            "Coderbyte": f"https://coderbyte.com/profile/{query}", "Edabit": f"https://edabit.com/user/{query}",
            "Scratch": f"https://scratch.mit.edu/users/{query}", "Newgrounds": f"https://newgrounds.com/art/view/{query}",
            "Itch.io": f"https://itch.io/profile/{query}", "GameJolt": f"https://gamejolt.com/@{query}",
            "Speedrun": f"https://speedrun.com/user/{query}", "Trello": f"https://trello.com/{query}",
            "Notion": f"https://notion.so/{query}", "Carrd": f"https://{query}.carrd.co",
            "Linkin.bio": f"https://linkin.bio/{query}", "Bio.link": f"https://bio.link/{query}",
            "AllMyLinks": f"https://allmylinks.com/{query}", "Crewfire": f"https://crewfire.me/{query}",
            "Kofi": f"https://ko-fi.com/{query}", "Patreon": f"https://patreon.com/{query}",
            "BuyMeACoffee": f"https://buymeacoffee.com/{query}", "Gumroad": f"https://gumroad.com/{query}",
            "Etsy": f"https://etsy.com/shop/{query}", "Redbubble": f"https://redbubble.com/people/{query}",
            "Society6": f"https://society6.com/{query}", "Teespring": f"https://teespring.com/stores/{query}",
            "MerchByAmazon": f"https://amazon.com/shops/{query}", "Zazzle": f"https://zazzle.com/store/{query}",
            "CafePress": f"https://cafepress.com/{query}", "Spreadshirt": f"https://spreadshirt.com/shop/user/{query}",
            "Teepublic": f"https://teepublic.com/user/{query}", "Threadless": f"https://threadless.com/@{query}",
            "DesignByHumans": f"https://designbyhumans.com/shop/{query}", "Inprnt": f"https://inprnt.com/gallery/{query}",
            "Pixiv": f"https://pixiv.net/users/{query}", "Danbooru": f"https://danbooru.donmai.us/users/{query}",
            "Gelbooru": f"https://gelbooru.com/index.php?page=account&s=profile&uname={query}",
            "Rule34": f"https://rule34.xxx/index.php?page=account&s=profile&uname={query}",
            "E621": f"https://e621.net/user/show/{query}", "FurAffinity": f"https://furaffinity.net/user/{query}",
            "Inkbunny": f"https://inkbunny.net/{query}", "Weasyl": f"https://weasyl.com/~{query}",
            "BaraAgar": f"https://baraag.net/@{query}", "Mstdn.social": f"https://mstdn.social/@{query}",
            "Social.tchncs.de": f"https://social.tchncs.de/@{query}", "Mamot.fr": f"https://mamot.fr/@{query}",
            "PixelFed": f"https://pixelfed.social/{query}", "Lemmy": f"https://lemmy.world/u/{query}",
            "Kbin": f"https://kbin.social/u/{query}", "Peertube": f"https://peertube.social/a/{query}",
            "Odysee": f"https://odysee.com/@{query}", "Rumble": f"https://rumble.com/user/{query}",
            "Bitchute": f"https://bitchute.com/channel/{query}", "LBRY": f"https://lbry.tv/@{query}",
            "DTube": f"https://d.tube/#!/c/{query}", "3Speak": f"https://3speak.tv/user/{query}",
            "Hive": f"https://hive.blog/@{query}", "Steemit": f"https://steemit.com/@{query}",
            "Minds": f"https://minds.com/{query}", "Gab": f"https://gab.com/{query}",
            "Parler": f"https://parler.com/profile/{query}", "Gettr": f"https://gettr.com/user/{query}",
            "TruthSocial": f"https://truthsocial.com/@{query}", "MeWe": f"https://mewe.com/profile/{query}",
            "Vero": f"https://vero.co/{query}", "Ello": f"https://ello.co/{query}",
            "Diaspora": f"https://diasporafoundation.org/people/{query}", "Friendica": f"https://friendica.network/profile/{query}",
            "Hubzilla": f"https://hubzilla.org/channel/{query}", "Misskey": f"https://misskey.io/@{query}",
            "Pleroma": f"https://pleroma.social/{query}", "Akkoma": f"https://akkoma.social/{query}",
            "Firefish": f"https://firefish.social/@{query}", "Calckey": f"https://calckey.social/@{query}",
            "Sharkey": f"https://sharkey.social/@{query}", "Glitch": f"https://glitch.social/@{query}",
            "Squawk": f"https://squawk.social/@{query}", "Birdsite": f"https://birdsite.live/{query}",
            "Nitter": f"https://nitter.net/{query}", "Twitodon": f"https://twitodon.com/{query}",
            "Thread Reader": f"https://threadreaderapp.com/user/{query}", "TweetDeck": f"https://tweetdeck.twitter.com/{query}",
            "Twitter Lists": f"https://twitter.com/{query}/lists", "Circle": f"https://circle.so/c/{query}",
            "Discourse": f"https://meta.discourse.org/u/{query}", "Flarum": f"https://discuss.flarum.org/u/{query}",
            "NodeBB": f"https://community.nodebb.org/user/{query}", "XenForo": f"https://xenforo.com/community/members/{query}",
            "phpBB": f"https://area51.phpbb.com/phpBB/memberlist.php?mode=viewprofile&u={query}",
            "MyBB": f"https://community.mybb.com/member.php?action=profile&uid={query}",
            "Vanilla": f"https://vanillaforums.org/profile/{query}", "Simple Machines": f"https://www.simplemachines.org/community/index.php?action=profile;u={query}",
            "Invision Power": f"https://www.invisioncommunity.com/profile/{query}",
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
            res += "**🕸️ Активные профили:**\n" + "\n".join(found[:50])
            if len(found) > 50:
                res += f"\n... и ещё {len(found)-50}"
        return res

    def plugin_phone(self, query: str) -> str:
        try:
            p = phonenumbers.parse(query, None)
            if not phonenumbers.is_valid_number(p):
                return "🕸️ Номер невалиден"
            region_code = phonenumbers.region_code_for_number(p)
            region_name = geocoder.description_for_number(p, "ru")
            # Уточнение региона для РФ
            if region_code == "RU" and (not region_name or region_name in ("Россия", "Russia")):
                national = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.NATIONAL)
                code = re.sub(r'[^\d]', '', national)
                if code.startswith('8'):
                    code = '7' + code[1:]
                if len(code) > 3:
                    code = code[1:4]
                    ru_regions = {
                        "347": "Республика Башкортостан", "843": "Республика Татарстан",
                        "855": "Республика Татарстан", "495": "Москва", "499": "Москва",
                        "812": "Санкт-Петербург", "383": "Новосибирская область",
                        "861": "Краснодарский край", "863": "Ростовская область",
                        "343": "Свердловская область", "423": "Приморский край",
                        "846": "Самарская область", "831": "Нижегородская область",
                        "473": "Воронежская область", "862": "Краснодарский край (Сочи)",
                        "391": "Красноярский край", "421": "Хабаровский край",
                        "844": "Волгоградская область", "845": "Саратовская область",
                        "865": "Ставропольский край", "872": "Республика Дагестан",
                        "871": "Чеченская Республика", "873": "Республика Ингушетия",
                        "917": "Поволжье/Урал (МТС)", "927": "Поволжье/Урал (МегаФон)",
                        "937": "Поволжье/Урал (МегаФон)", "987": "Поволжье/Урал (МегаФон)",
                        "996": "Поволжье/Урал (МегаФон)", "903": "Москва (МТС)",
                        "905": "Москва (МТС)", "906": "Москва (МТС)", "909": "Москва (МТС)",
                        "910": "Центр (МТС)", "911": "СЗФО (МТС)", "912": "Урал (МТС)",
                        "913": "Сибирь (МТС)", "914": "ДВ (МТС)", "915": "Центр (МТС)",
                        "916": "Москва (МТС)", "918": "Юг (МТС)", "919": "Поволжье (МТС)",
                        "920": "Центр (МегаФон)", "921": "СЗФО (МегаФон)", "922": "Урал (МегаФон)",
                        "923": "Сибирь (МегаФон)", "924": "ДВ (МегаФон)", "925": "Москва (МегаФон)",
                        "926": "Москва (МегаФон)", "928": "Юг (МегаФон)", "929": "Поволжье (МегаФон)",
                        "930": "Центр (МегаФон)", "931": "СЗФО (МегаФон)", "932": "Урал (МегаФон)",
                        "933": "Сибирь (МегаФон)", "934": "ДВ (МегаФон)", "936": "Москва (МегаФон)",
                        "938": "Юг (МегаФон)", "939": "Поволжье (МегаФон)", "941": "СЗФО (МегаФон)",
                        "950": "Урал (Билайн)", "951": "Сибирь (Билайн)", "952": "ДВ (Билайн)",
                        "953": "Поволжье (Билайн)", "958": "Москва (Билайн)", "960": "Центр (Билайн)",
                        "961": "СЗФО (Билайн)", "962": "Урал (Билайн)", "963": "Сибирь (Билайн)",
                        "964": "ДВ (Билайн)", "965": "Москва (Билайн)", "966": "Москва (Билайн)",
                        "967": "Москва (Билайн)", "968": "Москва (Билайн)", "969": "Москва (Билайн)",
                        "977": "Москва (Теле2)", "978": "Крым (Теле2)", "979": "Москва (Теле2)",
                        "980": "Центр (Теле2)", "981": "СЗФО (Теле2)", "982": "Урал (Теле2)",
                        "983": "Сибирь (Теле2)", "984": "ДВ (Теле2)", "985": "Москва (Теле2)",
                        "986": "Центр (Теле2)", "988": "Юг (Теле2)", "989": "Поволжье (Теле2)",
                        "991": "Центр (Тинькофф)", "992": "Таджикистан", "993": "Туркменистан",
                        "994": "Азербайджан", "995": "Грузия", "997": "Москва (Тинькофф)",
                        "998": "Узбекистан", "999": "РФ (разные)"
                    }
                    region_name = ru_regions.get(code, f"Россия (код {code})")
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
                    f"🕸️ **Регион:** {region_name}\n"
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

    async def plugin_exif(self, file_id: str, bot) -> str:
        try:
            file = await bot.get_file(file_id)
            data = await file.download_as_bytearray()
            img = Image.open(io.BytesIO(data))
            exif = img.getexif()
            if not exif:
                return "🕸️ **EXIF:** Метаданные отсутствуют. Отправьте как 'Файл'."
            tags = ["GPSInfo", "Model", "Make", "DateTimeOriginal", "Software", "LensModel"]
            found = {exif.get_tag_name(tid): exif[tid] for tid in exif if exif.get_tag_name(tid) in tags}
            if not found:
                return "🕸️ **EXIF:** Данные есть, но ключевые метки скрыты."
            return "🕸️ **EXIF Анализ:**\n" + "\n".join(f"🕸️ {k}: `{v}`" for k, v in found.items())
        except Exception as e:
            return f"🕸️ Ошибка фото: {e}"

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
# 🤖 TELEGRAM BOT
# ==========================================================
fw = OSINTFramework()

# КЛАВИАТУРА С РУССКИМИ НАЗВАНИЯМИ
KB_MAIN = InlineKeyboardMarkup([
    [InlineKeyboardButton("🕸️ IP Инфо", callback_data="ip"),
     InlineKeyboardButton("🕸️ DNS/WHOIS", callback_data="dns")],
    [InlineKeyboardButton("🕸️ Никнейм", callback_data="nick"),
     InlineKeyboardButton("🕸️ Телефон", callback_data="phone")],
    [InlineKeyboardButton("🕸️ Email", callback_data="email"),
     InlineKeyboardButton("🕸️ Утечки", callback_data="breach")],
    [InlineKeyboardButton("🕸️ TON Кошелёк", callback_data="ton"),
     InlineKeyboardButton("🕸️ TG Пользователь", callback_data="tg_id")],
    [InlineKeyboardButton("🕸️ Субдомены", callback_data="subs"),
     InlineKeyboardButton("🕸️ Discord", callback_data="discord")],
    [InlineKeyboardButton("🕸️ Геолокация", callback_data="geo"),
     InlineKeyboardButton("🕸️ EXIF Фото", callback_data="exif")],
    [InlineKeyboardButton("🕸️ Статистика БД", callback_data="dbstats")]
])

KB_BACK = InlineKeyboardMarkup([[InlineKeyboardButton("🕸️ Меню", callback_data="menu")]])

async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await fw.db.upsert_user(update.effective_user.id, update.effective_user.username,
                            update.effective_user.first_name, update.effective_user.last_name)
    db_stats = await fw.breach_db.get_stats()
    await update.message.reply_text(
        f"🕸️ **OSINT FRAMEWORK v7.0**\n\n"
        f"🕸️ **Базы утечек:**\n"
        f"• Email breaches: `{db_stats['breaches']:,}`\n"
        f"• Phone leaks: `{db_stats['phones']:,}`\n"
        f"• Combo lists: `{db_stats['combos']:,}`\n\n"
        f"🕸️ **Доступные модули:**\n"
        f"• 100+ платформ для ника\n"
        f"• Discord snowflake decoder\n"
        f"• Локальный поиск по базам\n\n"
        f"🕸️ **White Hat Only!**\n"
        f"Используйте только в законных целях.",
        reply_markup=KB_MAIN, parse_mode="Markdown"
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
        "exif": "🕸️ Отправьте **фото**:",
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
    await update.message.reply_text("🕸️ Обработка...")
    query = update.message.text.strip() if update.message.text else ""
    try:
        if tool == "exif":
            if not update.message.photo:
                res = "🕸️ Отправьте фото"
            else:
                res = await fw.plugin_exif(update.message.photo[-1].file_id, ctx.bot)
        else:
            plugin_func = fw.plugins.get(tool)
            if not plugin_func:
                res = "🕸️ Модуль не найден"
            elif tool == "tg_id":
                res = await plugin_func(query, ctx.bot)
            else:
                res = await plugin_func(query) if asyncio.iscoroutinefunction(plugin_func) else plugin_func(query)
        
        if len(res) > 4000:
            chunks = [res[i:i+4000] for i in range(0, len(res), 4000)]
            for chunk in chunks:
                await update.message.reply_text(chunk, parse_mode="Markdown")
        else:
            await update.message.reply_text(res, parse_mode="Markdown")
        await fw.db.log(uid, tool, query if query else "photo", "ok")
    except Exception as e:
        await update.message.reply_text(f"🕸️ Ошибка: {e}")
        await fw.db.log(uid, tool, query, f"error:{e}")
    finally:
        ctx.user_data["awaiting"] = None
        await update.message.reply_text("🕸️ Следующий запрос:", reply_markup=KB_MAIN)

# ==========================================================
# 🚀 ЗАПУСК (Render.com Compatible)
# ==========================================================
async def main():
    await fw.init()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, message_handler))
    logger.info("🕸️ OSINT Framework v7.0 запущен")
    
    # 🌐 ХАК ДЛЯ RENDER.COM (FREE TIER) - запускаем веб-сервер
    if os.environ.get('RENDER') or os.environ.get('PORT'):
        from aiohttp import web
        async def handle(request):
            return web.Response(text="OSINT Bot is alive 🕸️")
        web_app = web.Application()
        web_app.router.add_get('/', handle)
        runner = web.AppRunner(web_app)
        await runner.setup()
        port = int(os.environ.get('PORT', 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        logger.info(f"🕸️ Web server started on port {port} to keep Render awake")
    
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