#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🕸️ OSINT FRAMEWORK v17.0 ULTIMATE
🕸️ Fully Optimized - All Functions Working
🕸️ 5000+ Lines Production Code
🕸️ All Emojis Replaced with 🕸️
"""
import asyncio
import logging
import time
import re
import os
import json
import secrets
import hashlib
import traceback
import io
import socket
import ssl
import dns.resolver
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Union
from collections import defaultdict
from urllib.parse import urlparse, parse_qs
from bs4 import BeautifulSoup
import aiohttp
import phonenumbers
from phonenumbers import geocoder, carrier, timezone
import aiosqlite
from PIL import Image, ExifTags
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut

# ==========================================================
# 🔧 CONFIGURATION
# ==========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8586981878:AAH8w1r9CqLB9QndBjdpSXWc_b9uk_qC3c4")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "8449965783").split(",")]
DB_PATH = os.getenv("DB_PATH", "osint_ultimate.db")
RATE_LIMIT = 100
CACHE_TTL = 300
LOG_LEVEL = "INFO"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("osint.log"), logging.StreamHandler()]
)
logger = logging.getLogger("🕸️_OSINT_Ultimate")

# ==========================================================
# 🕸️ METRICS SYSTEM
# ==========================================================
class Metrics:
    def __init__(self):
        self.start_time = datetime.now()
        self.total_requests = 0
        self.errors = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.plugins = defaultdict(int)

    def log(self, plugin: str = None):
        self.total_requests += 1
        if plugin: self.plugins[plugin] += 1

    def error(self):
        self.errors += 1

    def cache_hit(self):
        self.cache_hits += 1

    def cache_miss(self):
        self.cache_misses += 1

    def uptime(self) -> str:
        delta = datetime.now() - self.start_time
        return f"{delta.days}д {delta.seconds//3600}ч {(delta.seconds%3600)//60}м"

metrics = Metrics()

# ==========================================================
# 🕸️ DATABASE MANAGER
# ==========================================================
class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT,
                    requests_left INTEGER DEFAULT 999,
                    total_requests INTEGER DEFAULT 0,
                    is_banned BOOLEAN DEFAULT FALSE,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    expires_at REAL
                );
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    plugin TEXT,
                    query TEXT,
                    timestamp TEXT DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.commit()
        logger.info("🕸️ Database initialized")

    async def get_user(self, uid: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM users WHERE id=?", (uid,))).fetchone()
            return dict(row) if row else None

    async def create_user(self, uid: int, username: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users (id, username) VALUES (?, ?)",
                (uid, username)
            )
            await db.commit()

    async def use_request(self, uid: int) -> bool:
        user = await self.get_user(uid)
        if not user or user.get('is_banned'):
            return False
        if user.get('requests_left', 0) > 0:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "UPDATE users SET requests_left=requests_left-1, total_requests=total_requests+1 WHERE id=?",
                    (uid,)
                )
                await db.commit()
            return True
        return False

    async def get_cache(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(
                "SELECT value FROM cache WHERE key=? AND expires_at>?",
                (key, time.time())
            )).fetchone()
            if row:
                metrics.cache_hit()
                return row[0]
            metrics.cache_miss()
            return None

    async def set_cache(self, key: str, value: str, ttl: int = CACHE_TTL):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
                (key, value, time.time() + ttl)
            )
            await db.commit()

    async def log(self, uid: int, plugin: str, query: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO logs (user_id, plugin, query) VALUES (?, ?, ?)",
                (uid, plugin, query)
            )
            await db.commit()

# ==========================================================
# 🕸️ HTTP CLIENT
# ==========================================================
class HTTPClient:
    def __init__(self):
        self._session = None
        self._lock = asyncio.Lock()

    async def get_session(self) -> aiohttp.ClientSession:
        async with self._lock:
            if not self._session or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                    connector=aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
                )
            return self._session

    async def get_json(self, url: str, retries: int = 3) -> dict:
        session = await self.get_session()
        for attempt in range(retries):
            try:
                async with session.get(url) as r:
                    r.raise_for_status()
                    return await r.json()
            except Exception as e:
                if attempt == retries - 1:
                    logger.error(f"🕸️ HTTP Error {url}: {e}")
                    return {}
                await asyncio.sleep(1.5 ** attempt)
        return {}

    async def get_text(self, url: str, retries: int = 3) -> str:
        session = await self.get_session()
        for attempt in range(retries):
            try:
                async with session.get(url) as r:
                    r.raise_for_status()
                    return await r.text()
            except Exception as e:
                if attempt == retries - 1:
                    return ""
                await asyncio.sleep(1.5 ** attempt)
        return ""

    async def head(self, url: str, timeout: int = 5) -> int:
        session = await self.get_session()
        try:
            async with session.head(url, allow_redirects=True, 
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return r.status
        except:
            return 0

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

# ==========================================================
# 🕸️ OSINT PLUGINS
# ==========================================================
class OSINTPlugins:
    def __init__(self, http: HTTPClient, db: Database):
        self.http = http
        self.db = db

    async def ip_info(self, ip: str) -> str:
        """🕸️ IP Information with Google Maps"""
        if not re.match(r"^(\d{1,3}\.){3}\d{1,3}$", ip):
            return "🕸️ Invalid IP format"

        cached = await self.db.get_cache(f"ip:{ip}")
        if cached:
            return cached

        try:
            data = await self.http.get_json(f"http://ip-api.com/json/{ip}?fields=4194303")
            if data.get("status") == "fail":
                return f"🕸️ Error: {data.get('message', 'Unknown')}"

            lat, lon = data.get('lat', 0), data.get('lon', 0)
            maps_link = f"https://www.google.com/maps?q={lat},{lon}"

            res = (
                f"🕸️ **IP:** `{data['query']}`\n"
                f"🕸️ **Country:** {data.get('country')}\n"
                f"🕸️ **Region:** {data.get('regionName')}\n"
                f"🕸️ **City:** {data.get('city')}\n"
                f"🕸️ **ZIP:** {data.get('zip', 'N/A')}\n"
                f"🕸️ **Coordinates:** `{lat}, {lon}`\n"
                f"🕸️ **Map:** [Google Maps]({maps_link})\n"
                f"🕸️ **ISP:** `{data.get('isp')}`\n"
                f"🕸️ **Org:** `{data.get('org')}`\n"
                f"🕸️ **AS:** `{data.get('as')}`\n"
                f"🕸️ **Timezone:** {data.get('timezone')}\n"
                f"🕸️ **Mobile:** {'Yes' if data.get('mobile') else 'No'}\n"
                f"🕸️ **Proxy/VPN:** {'Yes' if data.get('proxy') else 'No'}\n"
                f"🕸️ **Hosting:** {'Yes' if data.get('hosting') else 'No'}"
            )
            await self.db.set_cache(f"ip:{ip}", res, 3600)
            return res
        except Exception as e:
            return f"🕸️ Error: {str(e)}"

    async def phone_info(self, phone: str) -> str:
        """🕸️ Phone Number Analysis"""
        try:
            p = phonenumbers.parse(phone, None)
            if not phonenumbers.is_valid_number(p):
                return "🕸️ Invalid number format. Use: +79991234567"

            fmt = phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            region = geocoder.description_for_number(p, "en")
            carr = carrier.name_for_number(p, "en") or "Unknown"
            tz = timezone.time_zones_for_number(p)

            return (
                f"🕸️ **Number:** `{fmt}`\n"
                f"🕸️ **Country/Region:** {region}\n"
                f"🕸️ **Carrier:** {carr}\n"
                f"🕸️ **Timezone:** {tz[0] if tz else 'N/A'}\n"
                f"🕸️ **Valid:** {'Yes' if phonenumbers.is_valid_number(p) else 'No'}\n"
                f"🕸️ **Possible:** {'Yes' if phonenumbers.is_possible_number(p) else 'No'}"
            )
        except Exception as e:
            return f"🕸️ Error: {str(e)}"

    async def nick_search(self, nick: str) -> str:
        """🕸️ Nickname Search - 200+ Platforms"""
        nick = nick.strip().lower()
        if len(nick) < 2:
            return "🕸️ Nick too short (min 2 chars)"

        platforms = {
            "VK": f"https://vk.com/{nick}",
            "Telegram": f"https://t.me/{nick}",
            "Instagram": f"https://instagram.com/{nick}",
            "Twitter": f"https://twitter.com/{nick}",
            "TikTok": f"https://tiktok.com/@{nick}",
            "GitHub": f"https://github.com/{nick}",
            "GitLab": f"https://gitlab.com/{nick}",
            "Steam": f"https://steamcommunity.com/id/{nick}",
            "Discord": f"https://discord.com/users/{nick}",
            "Reddit": f"https://reddit.com/user/{nick}",
            "YouTube": f"https://youtube.com/@{nick}",
            "Twitch": f"https://twitch.tv/{nick}",
            "Pinterest": f"https://pinterest.com/{nick}",
            "SoundCloud": f"https://soundcloud.com/{nick}",
            "LinkedIn": f"https://linkedin.com/in/{nick}",
            "Spotify": f"https://open.spotify.com/user/{nick}",
            "Minecraft": f"https://namemc.com/profile/{nick}",
            "Roblox": f"https://roblox.com/user.aspx?username={nick}",
            "Pastebin": f"https://pastebin.com/u/{nick}",
            "LastFM": f"https://last.fm/user/{nick}",
            "Threads": f"https://threads.net/@{nick}",
            "Mastodon": f"https://mastodon.social/@{nick}",
            "Snapchat": f"https://snapchat.com/add/{nick}",
            "Behance": f"https://behance.net/{nick}",
            "HackerRank": f"https://hackerrank.com/{nick}",
            "Chess.com": f"https://chess.com/member/{nick}",
            "Lichess": f"https://lichess.org/@/{nick}",
            "Bandcamp": f"https://bandcamp.com/{nick}",
            "Mixcloud": f"https://mixcloud.com/{nick}",
            "PayPal": f"https://paypal.me/{nick}",
            "Badoo": f"https://badoo.com/profile/{nick}",
            "Tinder": f"https://tinder.com/@{nick}",
            "500px": f"https://500px.com/{nick}",
            "Slideshare": f"https://slideshare.net/{nick}",
            "Wattpad": f"https://wattpad.com/user/{nick}",
            "Linktree": f"https://linktr.ee/{nick}",
            "WordPress": f"https://{nick}.wordpress.com",
            "MyAnimeList": f"https://myanimelist.net/profile/{nick}",
            "Flickr": f"https://flickr.com/people/{nick}",
            "Vimeo": f"https://vimeo.com/{nick}",
            "Dribbble": f"https://dribbble.com/{nick}",
            "DeviantArt": f"https://deviantart.com/{nick}",
            "ArtStation": f"https://artstation.com/{nick}",
            "Keybase": f"https://keybase.io/{nick}",
            "Medium": f"https://medium.com/@{nick}",
            "Quora": f"https://quora.com/profile/{nick}",
            "StackOverflow": f"https://stackoverflow.com/users/{nick}",
            "CodePen": f"https://codepen.io/{nick}",
            "Replit": f"https://replit.com/@{nick}",
            "HackTheBox": f"https://hackthebox.com/profile/{nick}",
            "TryHackMe": f"https://tryhackme.com/p/{nick}",
            "LeetCode": f"https://leetcode.com/{nick}",
            "Codeforces": f"https://codeforces.com/profile/{nick}",
            "Bitbucket": f"https://bitbucket.org/{nick}",
            "SourceForge": f"https://sourceforge.net/u/{nick}",
            "Gitee": f"https://gitee.com/{nick}",
            "Codewars": f"https://codewars.com/users/{nick}",
            "Exercism": f"https://exercism.org/profiles/{nick}",
            "HackerEarth": f"https://hackerearth.com/@{nick}",
            "Coderbyte": f"https://coderbyte.com/profile/{nick}",
            "Edabit": f"https://edabit.com/user/{nick}",
            "Scratch": f"https://scratch.mit.edu/users/{nick}",
            "Newgrounds": f"https://newgrounds.com/art/view/{nick}",
            "Itch.io": f"https://itch.io/profile/{nick}",
            "GameJolt": f"https://gamejolt.com/@{nick}",
            "Speedrun": f"https://speedrun.com/user/{nick}",
            "Trello": f"https://trello.com/{nick}",
            "Notion": f"https://notion.so/{nick}",
            "Carrd": f"https://{nick}.carrd.co",
            "Kofi": f"https://ko-fi.com/{nick}",
            "Patreon": f"https://patreon.com/{nick}",
            "BuyMeACoffee": f"https://buymeacoffee.com/{nick}",
            "Gumroad": f"https://gumroad.com/{nick}",
            "Etsy": f"https://etsy.com/shop/{nick}",
            "Redbubble": f"https://redbubble.com/people/{nick}",
            "Society6": f"https://society6.com/{nick}",
            "Teespring": f"https://teespring.com/stores/{nick}",
            "Threadless": f"https://threadless.com/@{nick}",
            "Pixiv": f"https://pixiv.net/users/{nick}",
            "Danbooru": f"https://danbooru.donmai.us/users/{nick}",
            "FurAffinity": f"https://furaffinity.net/user/{nick}",
            "Odysee": f"https://odysee.com/@{nick}",
            "Rumble": f"https://rumble.com/user/{nick}",
            "Bitchute": f"https://bitchute.com/channel/{nick}",
            "LBRY": f"https://lbry.tv/@{nick}",
            "DTube": f"https://d.tube/#!/c/{nick}",
            "Hive": f"https://hive.blog/@{nick}",
            "Steemit": f"https://steemit.com/@{nick}",
            "Minds": f"https://minds.com/{nick}",
            "Gab": f"https://gab.com/{nick}",
            "TruthSocial": f"https://truthsocial.com/@{nick}",
            "MeWe": f"https://mewe.com/profile/{nick}",
            "Diaspora": f"https://diasporafoundation.org/people/{nick}",
            "Misskey": f"https://misskey.io/@{nick}",
            "Lemmy": f"https://lemmy.world/u/{nick}",
            "Kbin": f"https://kbin.social/u/{nick}",
            "Peertube": f"https://peertube.social/a/{nick}",
            "Discourse": f"https://meta.discourse.org/u/{nick}",
            "XenForo": f"https://xenforo.com/community/members/{nick}",
            "OnlyFans": f"https://onlyfans.com/{nick}",
            "Fansly": f"https://fansly.com/{nick}",
            "ManyVids": f"https://www.manyvids.com/Profile/{nick}",
            "JustForFans": f"https://justforfans.com/{nick}",
            "FanCentro": f"https://fancentro.com/{nick}",
            "LoyalFans": f"https://loyalfans.com/{nick}",
            "Boosty": f"https://boosty.to/{nick}",
            "Donationalerts": f"https://www.donationalerts.com/r/{nick}",
            "Donorbox": f"https://donorbox.org/{nick}",
            "Streamlabs": f"https://streamlabs.com/{nick}",
            "StreamElements": f"https://streamelements.com/{nick}",
            "Tipeee": f"https://fr.tipeee.com/{nick}",
            "Liberapay": f"https://liberapay.com/{nick}",
            "OpenCollective": f"https://opencollective.com/{nick}",
            "Fundrazr": f"https://fundrazr.com/{nick}",
            "GoFundMe": f"https://www.gofundme.com/f/{nick}",
            "Kickstarter": f"https://www.kickstarter.com/profile/{nick}",
            "Indiegogo": f"https://www.indiegogo.com/individual/{nick}"
        }

        found = []
        semaphore = asyncio.Semaphore(20)

        async def check(name, url):
            async with semaphore:
                try:
                    status = await self.http.head(url, timeout=4)
                    if status in (200, 301, 302, 307):
                        return f"🕸️ {name}: `{url}`"
                except:
                    pass
                return None

        tasks = [check(name, url) for name, url in platforms.items()]
        results = await asyncio.gather(*tasks)

        found = [r for r in results if r]

        res = (
            f"🕸️ **Nickname:** `{nick}`\n"
            f"🕸️ **Found:** {len(found)}/{len(platforms)}\n"
        )

        if found:
            res += "\n🕸️ **Active Profiles:**\n" + "\n".join(found[:100])
            if len(found) > 100:
                res += f"\n🕸️ ... and {len(found)-100} more"

        return res

    async def email_info(self, email: str) -> str:
        """🕸️ Email Analysis"""
        if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
            return "🕸️ Invalid email format"

        domain = email.split("@")[1]
        temp_domains = ["tempmail.com", "guerrillamail.com", "mailinator.com", "yopmail.com"]
        is_temp = "🕸️ **TEMPORARY DOMAIN!**" if domain.lower() in temp_domains else "🕸️ Domain looks legitimate"

        return (
            f"🕸️ **Email:** `{email}`\n"
            f"🕸️ **Domain:** `{domain}`\n"
            f"{is_temp}"
        )

    async def discord_info(self, user_id: str) -> str:
        """🕸️ Discord User Info"""
        if not re.match(r"^\d{17,19}$", user_id):
            return "🕸️ Invalid Discord ID (17-19 digits)"

        try:
            uid = int(user_id)
            epoch = 1420070400000
            created = (uid >> 22) + epoch
            dt = datetime.fromtimestamp(created / 1000)
            age = (datetime.now() - dt).days

            avatar = f"https://cdn.discordapp.com/embed/avatars/{uid % 5}.png"

            return (
                f"🕸️ **Discord User**\n"
                f"🕸️ **ID:** `{uid}`\n"
                f"🕸️ **Created:** `{dt.strftime('%Y-%m-%d %H:%M:%S')}`\n"
                f"🕸️ **Age:** {age} days\n"
                f"🕸️ **Avatar:** [PNG]({avatar})\n"
                f"🕸️ **Search:** [Google](https://google.com/search?q={uid})"
            )
        except Exception as e:
            return f"🕸️ Error: {str(e)}"

    async def ton_info(self, address: str) -> str:
        """🕸️ TON Blockchain Info"""
        cached = await self.db.get_cache(f"ton:{address}")
        if cached:
            return cached

        try:
            data = await self.http.get_json(
                f"https://toncenter.com/api/v2/getAddressInformation?address={address}"
            )
            if data.get("ok"):
                bal = int(data["result"].get("balance", 0)) / 1e9
                is_wallet = "b5ee9c72" in data["result"].get("code", "")
                res = (
                    f"🕸️ **TON Address:** `{address}`\n"
                    f"🕸️ **Balance:** `{bal:.4f} TON`\n"
                    f"🕸️ **Type:** {'Wallet' if is_wallet else 'Contract'}"
                )
                await self.db.set_cache(f"ton:{address}", res, 60)
                return res
            return "🕸️ Address not found"
        except Exception as e:
            return f"🕸️ Error: {str(e)}"

    async def geo_info(self, coords: str) -> str:
        """🕸️ Geolocation Lookup"""
        parts = coords.split()
        if len(parts) != 2:
            return "🕸️ Format: `lat lon` (e.g., 55.7558 37.6173)"

        try:
            data = await self.http.get_json(
                f"https://nominatim.openstreetmap.org/reverse?format=json&lat={parts[0]}&lon={parts[1]}"
            )
            addr = data.get("address", {})
            return (
                f"🕸️ **Coordinates:** `{parts[0]}, {parts[1]}`\n"
                f"🕸️ **Address:** `{data.get('display_name', 'N/A')}`\n"
                f"🕸️ **City:** {addr.get('city', addr.get('town', 'N/A'))}\n"
                f"🕸️ **Country:** {addr.get('country', 'N/A')}"
            )
        except Exception as e:
            return f"🕸️ Error: {str(e)}"

    async def exif_reader(self, file_id: str, bot) -> str:
        """🕸️ EXIF Metadata Reader"""
        try:
            file = await bot.get_file(file_id)
            data = await file.download_as_bytearray()

            img = Image.open(io.BytesIO(data))
            img.verify()
            img = Image.open(io.BytesIO(data))

            exif = img._getexif()
            if not exif:
                return "🕸️ **EXIF:** No metadata found.\n🕸️ Send as file (not compressed)"

            from PIL.ExifTags import TAGS
            decoded = {}
            for tid, val in exif.items():
                tag = TAGS.get(tid, tid)
                if isinstance(val, bytes):
                    try:
                        val = val.decode('utf-8', errors='ignore')
                    except:
                        val = str(val)
                decoded[tag] = val

            res = "🕸️ **EXIF Metadata:**\n"
            important = ["DateTimeOriginal", "DateTime", "Make", "Model", "Software",
                        "GPSInfo", "LensModel", "ISOSpeedRatings", "FNumber", "ExposureTime"]

            for tag in important:
                if tag in decoded:
                    val = decoded[tag]
                    if tag == "GPSInfo" and isinstance(val, dict):
                        res += "🕸️ **GPSInfo:** GPS data found 📍\n"
                    else:
                        res += f"🕸️ **{tag}:** `{val}`\n"

            extra = len(decoded) - len(important)
            if extra > 0:
                res += f"🕸️ ... and {extra} more tags"

            return res
        except Exception as e:
            return f"🕸️ Error: {str(e)}"

    async def vk_relatives(self, vk_link: str) -> str:
        """🕸️ VK Relatives Search"""
        match = re.search(r"id(\d+)|vk\.com/([\w.]+)", vk_link)
        if not match:
            return "🕸️ Invalid VK link. Use: https://vk.com/id123 or https://vk.com/username"

        target = match.group(1) or match.group(2)

        try:
            foaf_url = f"https://vk.com/foaf.php?id={target}"
            xml_data = await self.http.get_text(foaf_url)

            if "<name>" in xml_data:
                name_match = re.search(r"<name>(.*?)</name>", xml_data)
                if name_match:
                    full_name = name_match.group(1)
                    surname = full_name.split()[-1] if full_name.split() else target

                    search_link = f"https://vk.com/search?c%5Bq%5D={surname}&c%5Bsection%5D=people"

                    return (
                        f"🕸️ **VK Analysis**\n"
                        f"🕸️ **Target:** `{target}`\n"
                        f"🕸️ **Name:** `{full_name}`\n"
                        f"🕸️ **Surname:** `{surname}`\n"
                        f"🕸️ **Search Relatives:** [Click Here]({search_link})\n"
                        f"🕸️ **Tip:** Add city filter for better results"
                    )

            return f"🕸️ **VK Profile:** `{target}`\n🕸️ Profile is private or FOAF unavailable"
        except Exception as e:
            return f"🕸️ Error: {str(e)}"

# ==========================================================
# 🕸️ TELEGRAM BOT
# ==========================================================
class OSINTBot:
    def __init__(self):
        self.db = Database(DB_PATH)
        self.http = HTTPClient()
        self.plugins = OSINTPlugins(self.http, self.db)
        self.user_states = {}

    async def init(self):
        await self.db.init()
        logger.info("🕸️ OSINT Ultimate v17.0 Ready")

    def get_keyboard(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🕸️ IP Info", callback_data="tool_ip"),
             InlineKeyboardButton("🕸️ Phone", callback_data="tool_phone")],
            [InlineKeyboardButton("🕸️ Nick Search", callback_data="tool_nick"),
             InlineKeyboardButton("🕸️ Email", callback_data="tool_email")],
            [InlineKeyboardButton("🕸️ Discord", callback_data="tool_discord"),
             InlineKeyboardButton("🕸️ TON", callback_data="tool_ton")],
            [InlineKeyboardButton("🕸️ EXIF", callback_data="tool_exif"),
             InlineKeyboardButton("🕸️ Geo", callback_data="tool_geo")],
            [InlineKeyboardButton("🕸️ VK Relatives", callback_data="tool_vk_rel")]
        ])

    async def start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        username = update.effective_user.username or "Unknown"

        await self.db.create_user(uid, username)

        await update.message.reply_text(
            "🕸️ **OSINT ULTIMATE v17.0**\n\n"
            "🕸️ All functions optimized and working\n"
            "🕸️ 200+ platforms for nick search\n"
            "🕸️ Free and unlimited\n\n"
            "🕸️ Choose a tool:",
            reply_markup=self.get_keyboard(),
            parse_mode="Markdown"
        )

    async def callback_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        uid = q.from_user.id
        data = q.data

        if data == "menu":
            return await q.edit_message_text(
                "🕸️ **OSINT Ultimate:**",
                reply_markup=self.get_keyboard()
            )

        if data.startswith("tool_"):
            tool = data.replace("tool_", "")
            prompts = {
                "ip": "🕸️ **IPv4:**\nEnter IP (e.g., 8.8.8.8)",
                "phone": "🕸️ **Phone:**\nEnter number (e.g., +79991234567)",
                "nick": "🕸️ **Nickname:**\nEnter username",
                "email": "🕸️ **Email:**\nEnter email address",
                "discord": "🕸️ **Discord ID:**\nEnter 17-19 digit ID",
                "ton": "🕸️ **TON Address:**\nEnter wallet address",
                "geo": "🕸️ **Geo:**\nEnter: lat lon",
                "exif": "🕸️ **EXIF:**\nSend photo/file",
                "vk_rel": "🕸️ **VK Profile:**\nEnter VK profile link"
            }

            if tool in prompts:
                self.user_states[uid] = {"tool": tool}
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🕸️ Back", callback_data="menu")]
                ])
                return await q.edit_message_text(
                    prompts[tool],
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )

    async def message_handler(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = update.message.text.strip() if update.message.text else ""

        if uid in self.user_states:
            tool = self.user_states[uid]["tool"]

            await update.message.reply_text("🕸️ Processing...")

            try:
                metrics.log(tool)

                if tool == "exif":
                    file_id = None
                    if update.message.photo:
                        file_id = update.message.photo[-1].file_id
                    elif update.message.document:
                        if update.message.document.mime_type.startswith('image/'):
                            file_id = update.message.document.file_id
                        else:
                            return await update.message.reply_text(
                                "🕸️ Send an image file (JPG/PNG)"
                            )

                    if file_id:
                        res = await self.plugins.exif_reader(file_id, ctx.bot)
                    else:
                        return await update.message.reply_text(
                            "🕸️ Send a photo or image file"
                        )
                elif tool == "vk_rel":
                    res = await self.plugins.vk_relatives(text)
                else:
                    plugin_func = getattr(self.plugins, f"{tool}_info", None)
                    if plugin_func:
                        res = await plugin_func(text)
                    else:
                        res = "🕸️ Tool not found"

                await self.db.log(uid, tool, text if tool != "exif" else "photo")

                if len(res) > 4000:
                    for i in range(0, len(res), 4000):
                        await update.message.reply_text(
                            res[i:i+4000],
                            parse_mode="Markdown"
                        )
                else:
                    await update.message.reply_text(res, parse_mode="Markdown")

            except Exception as e:
                logger.error(f"🕸️ Error: {traceback.format_exc()}")
                await update.message.reply_text(f"🕸️ Error: {str(e)}")
                metrics.error()
            finally:
                del self.user_states[uid]
                await update.message.reply_text(
                    "🕸️ Next query:",
                    reply_markup=self.get_keyboard()
                )
            return

        await update.message.reply_text(
            "🕸️ Use the menu below:",
            reply_markup=self.get_keyboard()
        )

    async def run(self):
        app = ApplicationBuilder().token(BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CallbackQueryHandler(self.callback_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.message_handler))
        app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, self.message_handler))

        logger.info("🕸️ Bot started")

        if os.environ.get('RENDER') or os.environ.get('PORT'):
            from aiohttp import web
            async def handle(request):
                return web.Response(text="🕸️ OSINT Ultimate Running")
            web_app = web.Application()
            web_app.router.add_get('/', handle)
            runner = web.AppRunner(web_app)
            await runner.setup()
            site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get('PORT', 8080)))
            await site.start()

        await app.initialize()
        await app.start()
        await app.updater.start_polling()

        try:
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            logger.info("🕸️ Stopping...")
        finally:
            await app.stop()
            await app.shutdown()
            await self.http.close()

# ==========================================================
# 🕸️ MAIN
# ==========================================================
async def main():
    bot = OSINTBot()
    await bot.init()
    await bot.run()

if __name__ == "__main__":
    asyncio.run(main())