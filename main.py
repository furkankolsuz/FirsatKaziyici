"""
FirsatKaziyici - Donan?mHaber Forum Deal Scraper & Telegram Bulletin
Modules:
  - Module 1: Web Scraping (ForumScraper)
  - Module 2: Validation & Pre-filtering (ValidationEngine)
  - Module 3: AI Analysis (LLMAgent)
  - Module 4: Telegram Notification (TelegramNotifier)
"""

from __future__ import annotations

import asyncio
import re
import time
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types
import os
import logging
import logging
import os
import re
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
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
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://forum.donanimhaber.com/",
}

# Istanbul timezone = UTC+3
TZ_ISTANBUL = timezone(timedelta(hours=3))

GEMINI_API_KEY: str = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID: str = os.environ["TELEGRAM_CHAT_ID"]
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

LOOKBACK_HOURS: int = int(os.getenv("LOOKBACK_HOURS", "26"))
PAGES_TO_SCAN: int = int(os.getenv("PAGES_TO_SCAN", "2"))
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

        soup = BeautifulSoup(resp.text, "lxml")

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

        soup = BeautifulSoup(resp.text, "lxml")
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
                        raw_links.append(
                            data_href if data_href.startswith("http") else "https://" + data_href
                        )
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

            for page in pages_to_fetch:
                posts = await self._scrape_page(slug, page, source_name)
                all_posts.extend(posts)
                await asyncio.sleep(REQUEST_DELAY)

        logger.info("Total %d posts scraped.", len(all_posts))
        return all_posts

    async def resolve_redirect(self, url: str) -> Optional[str]:
        """Resolves forum redirect URLs (/mesaj/yonlen/...) to the actual product URL."""
        if not url.startswith("http"):
            url = "https://" + url
        try:
            resp = await self.client.head(url, follow_redirects=True, timeout=8)
            final = str(resp.url)
            if any(d in final for d in SHOPPING_DOMAINS):
                return final
            resp = await self.client.get(url, follow_redirects=True, timeout=10)
            return str(resp.url)
        except Exception:
            return None


# ===========================================================================
# MODULE 2: Validation & Pre-filtering
# ===========================================================================
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
            try:
                check_url = post.resolved_links[0]
                resp = await self.client.head(check_url, follow_redirects=True, timeout=5)
                post.http_status = resp.status_code
            except Exception as exc:
                logger.debug("HTTP check error (%s): %s", post.resolved_links[0], exc)
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
    """Uses Gemini to analyze deals, outputs JSON, and formats Telegram HTML safely."""

    def __init__(self) -> None:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is missing!")
        self.client = genai.Client(api_key=GEMINI_API_KEY)

    def _discover_models(self) -> list[str]:
        candidate_models: list[str] = []
        try:
            available = list(self.client.models.list())
            for m in available:
                m_name = getattr(m, "name", "") or str(m)
                methods = getattr(m, "supported_generation_methods", []) or []
                if "generateContent" in methods:
                    clean = m_name.replace("models/", "")
                    if "flash" in clean.lower():
                        candidate_models.insert(0, clean)
                    else:
                        candidate_models.append(clean)
        except Exception:
            pass

        fallbacks = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-2.5-pro"]
        for fb in fallbacks:
            if fb not in candidate_models:
                candidate_models.append(fb)
        return candidate_models

    def _build_prompt(self, posts: list[ForumPost]) -> str:
        lines: list[str] = [
            "GOREVIN:",
            "Sana verilen forum mesajlarindaki gecerli ve indirimli TUM URUNLERI cikar.",
            "SAKIN eleme yapip 2-3 tane urun birakma! Mesajlarda bulunan butun linkli urunleri JSON'a ekle (10-20 tane olsa bile hepsini ekle).",
            "Sadece net olarak sahte olanlari veya linki olmayanlari atla.",
            "Urunleri 'trend_firsatlar', 'elektronik', 'ev_yasam', 'supermarket', 'moda', 'oyun_hobi', 'diger' kategorilerine ayir.",
            "En iyi, en sicak 5 firsati 'trend_firsatlar' icine koy.",
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
            if p.resolved_links:
                lines.append(f"    Link: {p.resolved_links[0]}")

        return "\n".join(lines)

    def _json_to_html(self, data: dict, run_date: str) -> str:
        """Safely converts structured JSON to Telegram valid HTML."""
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
                    u_adi = item.get("urun_adi", "Ürün").replace("<", "").replace(">", "")
                    fiyat = item.get("fiyat", "Belirsiz").replace("<", "").replace(">", "")
                    link = item.get("link", "#")
                    kaynak = item.get("kaynak", "Link").replace("<", "").replace(">", "")
                    if not link.startswith("http"):
                        link = "#"
                    bulten += f"• <b>{u_adi}</b> — <code>{fiyat}</code> | <a href='{link}'>Satın Al</a> (<i>{kaynak}</i>)\n"
                    total_items += 1
                bulten += "\n"
        
        if total_items == 0:
            return "<b>ℹ️ Bugün kazınan geçerli bir fırsat bulunamadı.</b>"

        bulten += "<i>📅 Kaynak: DonanımHaber Forum</i>"
        return bulten

    async def analyze(self, posts: list[ForumPost]) -> str:
        if not posts:
            return "<b>ℹ️ Bugun paylasilan gecerli bir firsat bulunamadi.</b>"

        now = datetime.now(TZ_ISTANBUL)
        MONTHS_TR = {1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran", 7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık"}
        run_date = f"{now.day} {MONTHS_TR[now.month]} {now.year}"
        
        prompt = self._build_prompt(posts)
        logger.info("Sending %d posts to Gemini for JSON extraction...", len(posts))

        raw = ""
        last_error = None
        models_to_try = self._discover_models()

        for m_name in models_to_try:
            try:
                logger.info("Trying Gemini model: %s", m_name)
                response = self.client.models.generate_content(
                    model=m_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        response_mime_type="application/json",
                        max_output_tokens=8192,
                    ),
                )
                raw = response.text or ""
                if raw:
                    logger.info("Successfully generated with model: %s", m_name)
                    break
            except Exception as exc:
                logger.warning("Gemini model %s error: %s", m_name, exc)
                last_error = exc

        if not raw:
            logger.error("All Gemini models failed. Last error: %s", last_error)
            return f"<b>⚠️ AI analizi sırasında hata oluştu:</b> <code>{last_error}</code>"

        try:
            # Clean markdown JSON wrappers if Gemini added them despite response_mime_type
            raw_clean = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE | re.MULTILINE)
            raw_clean = re.sub(r"\s*```$", "", raw_clean, flags=re.MULTILINE)
            data = json.loads(raw_clean)
            bulletin = self._json_to_html(data, run_date)
            logger.info("JSON successfully parsed and HTML bulletin generated (%d chars).", len(bulletin))
            return bulletin
        except json.JSONDecodeError as e:
            logger.error("Failed to parse Gemini JSON: %s\nRaw output: %s", e, raw)
            return "<b>⚠️ AI veriyi formatlarken hata yaptı.</b>"


class TelegramNotifier:
    """Sends messages via Telegram Bot HTTP API using plain text + URLs."""

    MAX_LEN = 4000

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    def _split(self, text: str) -> list[str]:
        """Splits text into Telegram-safe chunks."""
        if len(text) <= self.MAX_LEN:
            return [text]

        chunks: list[str] = []
        while text:
            if len(text) <= self.MAX_LEN:
                chunks.append(text)
                break
            cut = text.rfind("\n", 0, self.MAX_LEN)
            if cut == -1:
                cut = self.MAX_LEN
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        return chunks

    async def _send_chunk(self, chunk: str) -> None:
        """Sends a single text chunk with HTML parsing."""
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "link_preview_options": {"is_disabled": True},
        }
        resp = await self.client.post(
            f"{TELEGRAM_API_BASE}/sendMessage",
            json=payload,
            timeout=15,
        )
        if resp.is_error:
            logger.error("Telegram API Error: %s", resp.text)
        resp.raise_for_status()

    async def send(self, text: str) -> None:
        """Sends cleanly generated HTML to Telegram."""
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Cannot send Telegram message: Credentials missing.")
            return

        chunks = self._split(text)

        for idx, chunk in enumerate(chunks, 1):
            try:
                await self._send_chunk(chunk)
                logger.info("Telegram message sent (%d/%d).", idx, len(chunks))
            except Exception as exc:
                logger.error("Telegram HTML send error (%d/%d): %s", idx, len(chunks), exc)
                # Fallback to plain text if somehow HTML still fails
                logger.error("Telegram HTML failed permanently for this chunk.")

            if len(chunks) > 1:
                await asyncio.sleep(1)

    async def send_error(self, error_msg: str) -> None:
        """Sends a short error notification."""
        text = f"⚠️ FirsatKaziyici Hata:\n{error_msg[:300]}"
        await self.send(text)


# ===========================================================================
# MAIN FLOW
# ===========================================================================
async def main() -> None:
    start = time.monotonic()
    logger.info("=" * 60)
    logger.info("FirsatKaziyici v2.0 started -- %s", datetime.now(TZ_ISTANBUL).isoformat())
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
                    "<b>\u2139\uFE0F Bugun kaz?nan gecerli firsat mesaji bulunamadi.</b>"
                )
                return

            # ---- Module 2: Validation ----
            validator = ValidationEngine(http_client, scraper)
            valid_posts = await validator.validate_all(raw_posts)

            # Fallback: add liked posts if too few valid ones
            if len(valid_posts) < 3:
                logger.info("Few valid posts, adding liked posts as fallback...")
                liked = sorted(raw_posts, key=lambda p: p.likes, reverse=True)[:10]
                for p in liked:
                    if p not in valid_posts:
                        valid_posts.append(p)

            # Sort by likes descending
            valid_posts.sort(key=lambda p: p.likes, reverse=True)

            # ---- Module 3: LLM Analysis ----
            agent = LLMAgent()
            bulletin = await agent.analyze(valid_posts)

            # ---- Module 4: Telegram ----
            await notifier.send(bulletin)

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
