"""
FirsatKaziyici - Donan?mHaber Forum Deal Scraper & Telegram Bulletin
Modules:
  - Module 1: Web Scraping (ForumScraper)
  - Module 2: Validation & Pre-filtering (ValidationEngine)
  - Module 3: AI Analysis (LLMAgent)
  - Module 4: Telegram Notification (TelegramNotifier)
"""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=UserWarning)

import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
from urllib.parse import urljoin, urlparse, unquote

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("firsatkaziyici")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
FORUM_BASE = "https://forum.donanimhaber.com"
REDIRECT_BASE = f"{FORUM_BASE}/mesaj/yonlen"

FORUM_SOURCES: dict[str, dict] = {
    "Amazon TR": {
        "url": f"{FORUM_BASE}/amazon-turkiye-ve-firsatlari-ana-konu--135048063",
        "slug": "amazon-turkiye-ve-firsatlari-ana-konu--135048063",
    },
    "Pazarama": {
        "url": f"{FORUM_BASE}/pazarama-firsatlari-ve-indirimleri-5-tl-ye-alisveris--157031926",
        "slug": "pazarama-firsatlari-ve-indirimleri-5-tl-ye-alisveris--157031926",
    },
    "N11": {
        "url": f"{FORUM_BASE}/n11-indirimleri-firsatlari-ve-kampanyalari-ana-konu--156735400",
        "slug": "n11-indirimleri-firsatlari-ve-kampanyalari-ana-konu--156735400",
    },
}

SHOPPING_DOMAINS = (
    "amazon.com.tr",
    "pazarama.com",
    "n11.com",
    "trendyol.com",
    "hepsiburada.com",
    "gittigidiyor.com",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://forum.donanimhaber.com/",
}

# Istanbul timezone = UTC+3
TZ_ISTANBUL = timezone(timedelta(hours=3))

GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

LOOKBACK_HOURS: int = int(os.getenv("LOOKBACK_HOURS", "26"))
PAGES_TO_SCAN: int = int(os.getenv("PAGES_TO_SCAN", "10"))
REQUEST_DELAY: float = float(os.getenv("REQUEST_DELAY", "2.0"))

# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------
@dataclass
class ForumPost:
    """Represents a single forum post/message."""
    source: str                    # Amazon TR / Pazarama / N11
    message_id: str
    author: str
    post_date: datetime
    text: str
    likes: int
    raw_links: list[str] = field(default_factory=list)
    resolved_links: list[str] = field(default_factory=list)
    is_valid: bool = False
    http_status: Optional[int] = None


# ===========================================================================
# MODULE 1: Web Scraping
# ===========================================================================
class ForumScraper:
    """Scrapes the last pages of Donan?mHaber forum threads."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def _get_total_pages(self, slug: str) -> int:
        """Returns total page count for a forum thread."""
        url = f"{FORUM_BASE}/{slug}"
        try:
            resp = await self.client.get(url, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Could not get page count (%s): %s", slug, exc)
            return 1

        soup = BeautifulSoup(resp.text, "html.parser")

        # From JS variable: totalPageCount: 22126
        match = re.search(r"totalPageCount\s*:\s*(\d+)", resp.text)
        if match:
            return int(match.group(1))

        # From last pagination link
        end_arrow = soup.select_one("a img.end-arrow")
        if end_arrow:
            parent = end_arrow.parent
            if parent and parent.get("href"):
                m = re.search(r"-(\d+)$", parent["href"])
                if m:
                    return int(m.group(1))

        return 1

    async def _scrape_page(self, slug: str, page: int, source_name: str) -> list[ForumPost]:
        """Parses all posts from a single forum page."""
        url = f"{FORUM_BASE}/{slug}-{page}" if page > 1 else f"{FORUM_BASE}/{slug}"
        logger.info("Scraping: %s (page %d)", source_name, page)

        try:
            resp = await self.client.get(url, follow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            logger.error("Failed to fetch page (%s p%d): %s", source_name, page, exc)
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        posts: list[ForumPost] = []

        cutoff = datetime.now(TZ_ISTANBUL) - timedelta(hours=LOOKBACK_HOURS)

        # Each <li id="mess_..."> is one post
        for li in soup.select("li[id^='mess_']"):
            msg_id = li.get("data-id", "")

            # Date from <time datetime="...">
            time_tag = li.select_one("time[datetime]")
            if not time_tag:
                continue
            try:
                dt_str = time_tag["datetime"]
                post_dt = datetime.fromisoformat(dt_str)
                if post_dt.tzinfo is None:
                    post_dt = post_dt.replace(tzinfo=TZ_ISTANBUL)
            except (ValueError, KeyError):
                continue

            # Time filter
            if post_dt < cutoff:
                continue

            # Author
            author_tag = (
                li.select_one(".ki-kullaniciadi a b")
                or li.select_one(".ki-kullaniciadi a")
            )
            author = author_tag.get_text(strip=True) if author_tag else "Unknown"

            # Message text
            content_div = (
                li.select_one(".icerik .msg")
                or li.select_one(f"section#messTable_{msg_id}")
                or li.select_one("span.msg")
            )
            text = content_div.get_text(separator=" ", strip=True) if content_div else ""

            # Like count
            like_el = li.select_one("a[class*='begeni']")
            likes = 0
            if like_el:
                m = re.search(r"(\d+)", like_el.get_text(strip=True))
                if m:
                    likes = int(m.group(1))

            # Extract links (forum redirects + direct shopping links)
            raw_links: list[str] = []
            for a in li.select("a[href]"):
                href = a["href"]
                if "ExternalLinkRedirect" in href or "/mesaj/yonlen" in href:
                    data_href = a.get("data-href", "")
                    if data_href and "donanimhaber" not in data_href:
                        if data_href.startswith("http"):
                            raw_links.append(data_href)
                        elif "." in data_href and "/" in data_href:
                            raw_links.append("https://" + data_href)
                    else:
                        raw_links.append(urljoin(FORUM_BASE, href))
                elif any(d in href for d in SHOPPING_DOMAINS):
                    raw_links.append(href)

            posts.append(ForumPost(
                source=source_name,
                message_id=msg_id,
                author=author,
                post_date=post_dt,
                text=text[:800],
                likes=likes,
                raw_links=list(dict.fromkeys(raw_links)),
            ))

        logger.info("  -> %d new posts found (last %d hours)", len(posts), LOOKBACK_HOURS)
        return posts

    async def scrape_all(self) -> list[ForumPost]:
        """Scrapes all configured forum sources."""
        all_posts: list[ForumPost] = []

        for source_name, source_info in FORUM_SOURCES.items():
            slug = source_info["slug"]
            total = await self._get_total_pages(slug)
            logger.info("%s: total %d pages", source_name, total)

            pages_to_fetch = list(range(max(1, total - PAGES_TO_SCAN + 1), total + 1))

            cutoff = datetime.now(TZ_ISTANBUL) - timedelta(hours=LOOKBACK_HOURS)
            # Fetch from newest page (total) downwards
            pages_to_fetch.reverse()
            for page in pages_to_fetch:
                posts = await self._scrape_page(slug, page, source_name)
                all_posts.extend(posts)
                if posts and posts[-1].post_date < cutoff:
                    logger.info("Reached posts older than cutoff (%d hours), stopping scan.", LOOKBACK_HOURS)
                    break
                await asyncio.sleep(REQUEST_DELAY)

        logger.info("Total %d posts scraped.", len(all_posts))
        return all_posts

    async def resolve_redirect(self, url: str) -> Optional[str]:
        """Resolves forum redirect URLs statically or via HEAD if necessary."""
        from urllib.parse import parse_qs, urlparse
        
        if not url.startswith("http"):
            url = "https://" + url
            
        if "ExternalLinkRedirect" in url:
            qs = parse_qs(urlparse(url).query)
            target = qs.get("url", [None])[0]
            if target:
                return target

        # If it's already a direct shopping link, return it without HTTP GET
        if any(d in url for d in SHOPPING_DOMAINS):
            return url

        # Only do HTTP request for /mesaj/yonlen/... or shortlinks
        try:
            async with self.client.stream("GET", url, follow_redirects=True, timeout=5) as resp:
                final = str(resp.url)
                if resp.status_code == 404:
                    return None
            if any(d in final for d in SHOPPING_DOMAINS):
                return final
            return None
        except Exception:
            return None


# ===========================================================================
# MODULE 2: Validation & Pre-filtering
# ===========================================================================

SEEN_FILE = "seen_ids.json"

def load_seen() -> set[str]:
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r", encoding="utf-8") as file:
                return set(json.load(file))
    except Exception:
        pass
    return set()

def save_seen(seen: set[str]) -> None:
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as file:
            json.dump(list(seen), file)
    except Exception:
        pass

class ValidationEngine:
    """
    - Resolves forum redirect links to real product URLs.
    - Verifies HTTP 200 status.
    - Filters out posts with no product links.
    """

    def __init__(self, client: httpx.AsyncClient, scraper: ForumScraper) -> None:
        self.client = client
        self.scraper = scraper

    async def validate_post(self, post: ForumPost) -> ForumPost:
        """Validates a post: resolves links and checks HTTP status."""
        if not post.raw_links:
            post.is_valid = False
            return post

        resolved: list[str] = []
        for raw in post.raw_links[:3]:
            if "/mesaj/yonlen/" in raw or "ExternalLinkRedirect" in raw:
                r = await self.scraper.resolve_redirect(raw)
                if r:
                    resolved.append(r)
            elif any(d in raw for d in SHOPPING_DOMAINS):
                resolved.append(raw)
            await asyncio.sleep(0.5)

        post.resolved_links = list(dict.fromkeys(resolved))

        if post.resolved_links:
            post.is_valid = True
            # Skip the actual GET for direct shopping links to save time and avoid 403s
            post.http_status = 200
            post.is_valid = True
        else:
            post.is_valid = False

        return post

    async def validate_all(self, posts: list[ForumPost]) -> list[ForumPost]:
        """Validates all posts concurrently with a semaphore limit."""
        sem = asyncio.Semaphore(5)

        async def _bounded(p: ForumPost) -> ForumPost:
            async with sem:
                return await self.validate_post(p)

        results = await asyncio.gather(*[_bounded(p) for p in posts], return_exceptions=True)
        valid_posts: list[ForumPost] = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Validation error: %s", r)
            elif isinstance(r, ForumPost) and r.is_valid:
                valid_posts.append(r)

        logger.info("%d / %d posts passed validation.", len(valid_posts), len(posts))
        return valid_posts


# ===========================================================================
# MODULE 3: AI Analysis
# ===========================================================================
class LLMAgent:
    def __init__(self) -> None:
        self.client = genai.Client(api_key=GEMINI_API_KEY)

    def _discover_models(self) -> list[str]:
        candidates = []
        try:
            for m in self.client.models.list():
                name = getattr(m, "name", "").replace("models/", "")
                if "flash" in name.lower() or "pro" in name.lower():
                    candidates.append(name)
        except Exception as e:
            logger.error("Telegram send_error failed: %s", e)
        # Prioritize 2.5 flash, then fallbacks
        fallbacks = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]
        valid = []
        for f in fallbacks:
            if any(c.startswith(f) for c in candidates):
                valid.append(f)
        return valid or fallbacks

    def _build_prompt(self, posts: list[ForumPost]) -> str:
        lines = [
            "GOREVIN:",
            "Sana verilen forum mesajlarindaki gecerli ve indirimli TUM URUNLERI cikar.",
            "SAKIN eleme yapip 2-3 tane urun birakma! Mesajlarda bulunan butun linkli urunleri JSON'a ekle (kac tane varsa hepsi).",
            "Urunleri 'trend_firsatlar', 'elektronik', 'ev_yasam', 'supermarket', 'moda', 'oyun_hobi', 'diger' kategorilerine ayir.",
            "En iyi 5 firsati 'trend_firsatlar' icine koy.",
            "",
            "CIKTI FORMATI KESINLIKLE JSON OLMALIDIR:",
            "{",
            "  \"trend_firsatlar\": [",
            "    {\"urun_adi\": \"Urun Adi\", \"fiyat\": \"1.999 TL\", \"link\": \"https://...\", \"kaynak\": \"Amazon TR\"}",
            "  ],",
            "  \"elektronik\": [],",
            "  \"ev_yasam\": [],",
            "  \"supermarket\": [],",
            "  \"moda\": [],",
            "  \"oyun_hobi\": [],",
            "  \"diger\": []",
            "}",
            "",
            "FORUM MESAJLARI:"
        ]
        for i, p in enumerate(posts, 1):
            lines.append(f"[{i}] Kaynak: {p.source}")
            lines.append(f"    Mesaj: {p.text[:400]}")
            if p.resolved_links: lines.append(f"    Linkler: {', '.join(p.resolved_links)}")
        return "\n".join(lines)

    def _json_to_html(self, data: dict, run_date: str) -> str:
        bulten = f"<b>🔥 Günlük Fırsat Bülteni — {run_date}</b>\n\n"
        categories = {
            "trend_firsatlar": "⭐ Trend Fırsatlar",
            "elektronik": "📱 Elektronik",
            "ev_yasam": "🏠 Ev & Yaşam",
            "supermarket": "🛒 Süpermarket",
            "moda": "👕 Moda",
            "oyun_hobi": "🎮 Oyun & Hobi",
            "diger": "📦 Diğer"
        }
        total_items = 0
        for cat_key, cat_name in categories.items():
            items = data.get(cat_key, [])
            if items:
                bulten += f"<b>{cat_name}</b>\n"
                for item in items:
                    u_adi = html.escape(str(item.get("urun_adi", "Ürün")))
                    fiyat = html.escape(str(item.get("fiyat", "Belirsiz")))
                    link = str(item.get("link", "#"))
                    kaynak = html.escape(str(item.get("kaynak", "Link")))
                    if not link.startswith("http"): link = "#"
                    bulten += f'• <b>{u_adi}</b> — <code>{fiyat}</code> | <a href="{html.escape(link, quote=True)}">Satın Al</a> (<i>{kaynak}</i>)\n'
                    total_items += 1
                bulten += "\n"
        if total_items == 0: return "<b>ℹ️ Bugün kazınan geçerli bir fırsat bulunamadı.</b>"
        bulten += "<i>📅 Kaynak: DonanımHaber Forum</i>"
        return bulten

    async def analyze(self, posts: list[ForumPost]) -> str:
        if not posts: return "<b>ℹ️ Bugün paylaşılan geçerli bir fırsat bulunamadı.</b>"
        now = datetime.now(TZ_ISTANBUL)
        MONTHS_TR = {1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran", 7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık"}
        run_date = f"{now.day} {MONTHS_TR[now.month]} {now.year}"
        prompt = self._build_prompt(posts)
        sys_instr = "Tüm geçerli ürünleri bulan bir JSON API'sisin. SADECE GEÇERLİ (VALID) JSON CIKTISI VER. JSON string'leri içinde çift tırnak kullanırken kaçış (escape) yap."
        
        raw = ""
        last_error = None
        for m_name in self._discover_models():
            try:
                response = await self.client.aio.models.generate_content(
                    model=m_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        system_instruction=sys_instr,
                        response_mime_type="application/json",
                        max_output_tokens=32000,
                    ),
                )
                raw = response.text or ""
                if raw: break
            except Exception as exc:
                last_error = exc
        if not raw: return f"<b>⚠️ AI Hatası:</b> <code>{last_error}</code>"

        try:
            raw_clean = raw.strip()
            if raw_clean.startswith("```"):
                raw_clean = re.sub(r"^```(?:json)?\s*", "", raw_clean, flags=re.IGNORECASE)
                raw_clean = re.sub(r"\s*```$", "", raw_clean)
            s = raw_clean.find("{")
            e = raw_clean.rfind("}")
            if s != -1 and e != -1: raw_clean = raw_clean[s:e+1]
            data = json.loads(raw_clean)
            return self._json_to_html(data, run_date)
        except Exception as e:
            logger.error("JSON Hatasi. Raw output: %s", raw)
            return f"<b>⚠️ JSON Hatası:</b> <code>{e}</code>\n\nRaw Data:\n<code>{raw[:500]}</code>"


class TelegramNotifier:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    def _split(self, text: str) -> list[str]:
        max_len = 4000
        if len(text) <= max_len: return [text]
        
        # Split safely by double newline (paragraphs/items) to avoid breaking HTML tags mid-line
        chunks = []
        paragraphs = text.split("\n\n")
        current_chunk = ""
        
        for p in paragraphs:
            if len(p) > max_len:
                # Fallback to line splitting if paragraph is huge
                if current_chunk:
                    chunks.append(current_chunk.strip())
                    current_chunk = ""
                for line in p.split("\n"):
                    if len(current_chunk) + len(line) + 1 <= max_len:
                        current_chunk += line + "\n"
                    else:
                        if current_chunk: chunks.append(current_chunk.strip())
                        current_chunk = line + "\n"
            elif len(current_chunk) + len(p) + 2 <= max_len:
                current_chunk += p + "\n\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = p + "\n\n"
                
        if current_chunk:
            chunks.append(current_chunk.strip())
            
        return chunks

    async def send(self, text: str) -> None:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
        chunks = self._split(text)
        for idx, chunk in enumerate(chunks, 1):
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            try:
                resp = await self.client.post(f"{TELEGRAM_API_BASE}/sendMessage", json=payload, timeout=15)
                resp.raise_for_status()
            except Exception as e:
                logger.error("Telegram HTML failed, attempting plain text: %s", e)
                try:
                    payload.pop("parse_mode", None)
                    payload["text"] = re.sub(r'<[^>]+>', '', chunk)
                    await self.client.post(f"{TELEGRAM_API_BASE}/sendMessage", json=payload, timeout=15)
                except Exception as e2:
                    logger.error("Telegram plain text fallback also failed: %s", e2)
            if len(chunks) > 1: await asyncio.sleep(1)


    async def send_error(self, error_msg: str) -> None:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"⚠️ FirsatKaziyici Hata:\n{error_msg[:1000]}"}
        try:
            await self.client.post(f"{TELEGRAM_API_BASE}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            logger.error("Telegram send_error failed: %s", e)

async def main() -> None:
    start = time.monotonic()
    logger.info("=" * 60)
    logger.info("FirsatKaziyici v3.1 started -- %s", datetime.now(TZ_ISTANBUL).isoformat())
    logger.info("=" * 60)

    missing = []
    if not GEMINI_API_KEY: missing.append("GEMINI_API_KEY")
    if not TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")
    if missing:
        logger.error("CRITICAL: Missing environment variables: %s", ", ".join(missing))

    async with httpx.AsyncClient(
        headers=HEADERS,
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    ) as http_client:
        notifier = TelegramNotifier(http_client)
        if missing:
            logger.error("Execution halted due to missing secrets.")
            return

        try:
            # ---- Module 1: Scraping ----
            scraper = ForumScraper(http_client)
            raw_posts = await scraper.scrape_all()

            if not raw_posts:
                logger.warning("No posts found, skipping bulletin.")
                await notifier.send(
                    "<b>\u2139\uFE0F Bugün kazınan geçerli fırsat mesajı bulunamadi.</b>"
                )
                return

            # ---- Module 2: Validation ----
            validator = ValidationEngine(http_client, scraper)
            valid_posts = await validator.validate_all(raw_posts)
            
            seen_ids = load_seen()
            new_posts = [p for p in valid_posts if p.message_id not in seen_ids]
            new_posts.sort(key=lambda p: (p.likes, p.post_date.timestamp()), reverse=True)

            if not new_posts:
                logger.warning("No new valid posts found after filtering seen_ids.")
                await notifier.send("<b>ℹ️ Taranan sayfalarda yeni fırsat bulunamadı.</b>")
                return

            # ---- Module 3: LLM Analysis ----
            agent = LLMAgent()
            bulletin = await agent.analyze(new_posts)

            # ---- Module 4: Telegram ----
            await notifier.send(bulletin)
            
            # Save state
            seen_ids.update(p.message_id for p in new_posts)
            save_seen(seen_ids)

        except Exception as exc:
            logger.exception("Critical error: %s", exc)
            try:
                await notifier.send_error(str(exc))
            except Exception:
                pass

    elapsed = time.monotonic() - start
    logger.info("Done in %.1f seconds.", elapsed)


if __name__ == "__main__":
    asyncio.run(main())
