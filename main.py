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
import logging
import os
import re
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
    """Uses Gemini to analyze deals and generate the Telegram bulletin text."""

    MODEL = "gemini-1.5-flash"

    def __init__(self) -> None:
        self.client = genai.Client(api_key=GEMINI_API_KEY)

    def _build_prompt(self, posts: list[ForumPost], run_date: str) -> str:
        lines: list[str] = [
            f"Tarih: {run_date}",
            "Asagida Donan?mHaber forumundan kaz?nan son 24 saatin firsat mesajlari var.",
            "Her mesajda: kaynak, yazar, begeni sayisi, mesaj metni ve urun linkleri yer almaktadir.",
            "",
            "GOREV:",
            "1. Sahte/sisiririlmis indirimleri ve belirsiz kuponlari filtrele.",
            "2. Kalan firsatlar aras?ndan EN YUKSEK TASARRUFLU 3-5 urunu 'Trend Firsatlar' olarak sec.",
            "3. Firsatlari kategorilere ayir: Elektronik | Bilgisayar & Oyun | Ev & Yasam | Supermarket | Moda | Diger",
            "4. Cikt?y? asagidaki HTML formatinda, Turkce olarak uretin.",
            "   - Emoji kullan, okunabilir tut.",
            "   - Her firsat icin: urun ad?, fiyat, kaynak site, link, kategori.",
            "   - Telegram HTML parse moduna uygun: <b>, <i>, <a href>, <code> taglari kullan.",
            "   - 3800 karakteri ASMA (Telegram limiti).",
            "",
            "CIKTI FORMATI (tam bu sekilde, baska hicbir sey yazma):",
            "---BEGIN---",
            "<b>\U0001F525 Gunluk Firsat Bulteni \u2014 {tarih}</b>",
            "",
            "<b>\u2B50 Trend Firsatlar</b>",
            "\u2022 <b>[Urun Adi]</b> \u2014 <code>[Fiyat TL]</code> | <a href=\'[link]\'>Satin Al</a> (<i>[Kaynak]</i>) \U0001F44D[begeni]",
            "...",
            "",
            "<b>\U0001F4E6 Elektronik</b>",
            "\u2022 ...",
            "",
            "<b>\U0001F3E0 Ev & Yasam</b>",
            "\u2022 ...",
            "",
            "<b>\U0001F6D2 Supermarket</b>",
            "\u2022 ...",
            "",
            "<i>\U0001F4C5 Bulten saati: TSI 10:00 | Kaynak: forum.donanimhaber.com</i>",
            "---END---",
            "",
            "FORUM MESAJLARI:",
        ]

        for i, p in enumerate(posts, 1):
            lines.append(
                f"\n[{i}] Kaynak: {p.source} | Yazar: {p.author} | Begeni: {p.likes}"
            )
            lines.append(f"    Tarih: {p.post_date.strftime('%d.%m.%Y %H:%M')}")
            lines.append(f"    Metin: {p.text[:400]}")
            if p.resolved_links:
                lines.append(f"    Link(ler): {' | '.join(p.resolved_links[:2])}")

        return "\n".join(lines)

    async def analyze(self, posts: list[ForumPost]) -> str:
        """Sends posts to Gemini and returns the bulletin text."""
        if not posts:
            return "<b>\u2139\uFE0F Bugun paylasilan gecerli bir firsat bulunamadi.</b>"

        run_date = datetime.now(TZ_ISTANBUL).strftime("%d %B %Y")
        prompt = self._build_prompt(posts, run_date)
        logger.info("Sending %d posts to Gemini...", len(posts))

        try:
            response = self.client.models.generate_content(
                model=self.MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=2048,
                ),
            )
            raw = response.text or ""
        except Exception as exc:
            logger.error("Gemini API error: %s", exc)
            return f"<b>\u26A0\uFE0F AI analizi sirasinda hata olustu:</b> <code>{exc}</code>"

        # Extract content between ---BEGIN--- and ---END---
        match = re.search(r"---BEGIN---\s*(.*?)\s*---END---", raw, re.DOTALL)
        bulletin = match.group(1).strip() if match else raw.strip()

        logger.info("Bulletin generated (%d chars).", len(bulletin))
        return bulletin


# ===========================================================================
# MODULE 4: Telegram Notification
# ===========================================================================
class TelegramNotifier:
    """Sends messages via Telegram Bot HTTP API."""

    MAX_LEN = 4096

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

    async def send(self, text: str) -> None:
        """Sends message chunks to the configured chat ID."""
        chunks = self._split(text)
        for idx, chunk in enumerate(chunks, 1):
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            }
            try:
                resp = await self.client.post(
                    f"{TELEGRAM_API_BASE}/sendMessage",
                    json=payload,
                    timeout=15,
                )
                if resp.is_error:
                    logger.error("Telegram API Error Body: %s", resp.text)
                resp.raise_for_status()
                logger.info("Telegram message sent (%d/%d).", idx, len(chunks))
            except Exception as exc:
                logger.error("Telegram send error (%d/%d): %s", idx, len(chunks), exc)
                logger.info("Retrying without HTML parse mode...")
                plain_payload = {
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": re.sub(r'<[^>]+>', '', chunk),
                }
                try:
                    r2 = await self.client.post(
                        f"{TELEGRAM_API_BASE}/sendMessage",
                        json=plain_payload,
                        timeout=15,
                    )
                    r2.raise_for_status()
                    logger.info("Telegram plain text fallback sent successfully.")
                except Exception as e2:
                    logger.error("Telegram fallback also failed: %s", e2)
            if len(chunks) > 1:
                await asyncio.sleep(1)

    async def send_error(self, error_msg: str) -> None:
        """Sends a short error notification."""
        text = f"<b>\u26A0\uFE0F FirsatKaziyici Hata</b>\n<code>{error_msg[:300]}</code>"
        await self.send(text)


# ===========================================================================
# MAIN FLOW
# ===========================================================================
async def main() -> None:
    start = time.monotonic()
    logger.info("=" * 60)
    logger.info("FirsatKaziyici started -- %s", datetime.now(TZ_ISTANBUL).isoformat())
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
            bulletin = await agent.analyze(valid_posts[:30])

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
