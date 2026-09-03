"""
FirsatKaziyici - DonanimHaber Forum Deal Scraper & Telegram Bulletin
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# ===========================================================================
# CONFIGURATION
# ===========================================================================
FORUM_BASE_URL = "https://forum.donanimhaber.com"
TARGET_URLS = [
    "https://forum.donanimhaber.com/amazon-turkiye-ve-firsatlari-ana-konu--135048063",
    "https://forum.donanimhaber.com/pazarama-firsatlari-ve-indirimleri-5-tl-ye-alisveris--157031926",
    "https://forum.donanimhaber.com/n11-indirimleri-firsatlari-ve-kampanyalari-ana-konu--156735400",
]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

TZ_ISTANBUL = timezone(timedelta(hours=3))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/115.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("firsatkaziyici")

# ===========================================================================
# MODELS
# ===========================================================================
@dataclass
class ForumPost:
    post_id: str
    author: str
    text: str
    raw_links: list[str] = field(default_factory=list)
    resolved_links: list[str] = field(default_factory=list)
    post_date: datetime = field(default_factory=lambda: datetime.now(TZ_ISTANBUL))
    source: str = ""
    likes: int = 0

# ===========================================================================
# MODULE 1: SCRAPER
# ===========================================================================
class ForumScraper:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def get_page(self, url: str) -> str:
        resp = await self.client.get(url, timeout=15)
        resp.raise_for_status()
        return resp.text

    def extract_total_pages(self, html: str) -> int:
        soup = BeautifulSoup(html, "html.parser")
        pag = soup.find("ul", class_="pagination")
        if not pag:
            return 1
        links = pag.find_all("a")
        for link in reversed(links):
            if link.text.strip().isdigit():
                return int(link.text.strip())
        return 1

    def parse_posts(self, html: str, source_name: str) -> list[ForumPost]:
        soup = BeautifulSoup(html, "html.parser")
        posts: list[ForumPost] = []
        msg_divs = soup.find_all("div", class_="msg-border")

        for msg in msg_divs:
            try:
                author_tag = msg.find("div", class_="msg-info").find("a", class_="ki")
                author = author_tag.text.strip() if author_tag else "Bilinmiyor"
                post_id = msg.get("id", "") or "Bilinmiyor"

                text_div = msg.find("div", class_="msg-text")
                if not text_div:
                    continue
                content = text_div.get_text(separator="\n", strip=True)

                raw_links = []
                for a_tag in text_div.find_all("a", href=True):
                    href = a_tag["href"]
                    if href.startswith("/store/"):
                        href = urljoin(FORUM_BASE_URL, href)
                    raw_links.append(href)

                date_str = "Bilinmiyor"
                date_tag = msg.find("div", class_="upb")
                if date_tag:
                    dt = date_tag.find("time")
                    date_str = dt.text.strip() if dt else date_tag.text.strip()

                likes = 0
                like_tag = msg.find("span", class_="sayiArti")
                if like_tag and like_tag.text.strip().isdigit():
                    likes = int(like_tag.text.strip())

                posts.append(
                    ForumPost(
                        post_id=post_id,
                        author=author,
                        text=content,
                        raw_links=raw_links,
                        source=source_name,
                        likes=likes,
                    )
                )
            except Exception as e:
                logger.debug("Eski veya bozuk bir mesaj atlaniyor: %s", e)
        return posts

    async def scrape_all(self) -> list[ForumPost]:
        all_posts: list[ForumPost] = []
        for url in TARGET_URLS:
            try:
                html = await self.get_page(url)
                soup = BeautifulSoup(html, "html.parser")
                title_tag = soup.find("h1")
                topic = title_tag.text.strip() if title_tag else "DH Konusu"
                if "Amazon" in topic: source_name = "Amazon TR"
                elif "Pazarama" in topic: source_name = "Pazarama"
                elif "N11" in topic: source_name = "N11"
                else: source_name = topic[:15]

                total_pages = self.extract_total_pages(html)
                logger.info("%s: total %d pages", source_name, total_pages)

                pages_to_scrape = [total_pages - 1, total_pages] if total_pages > 1 else [1]
                for p_num in pages_to_scrape:
                    if p_num <= 0: continue
                    logger.info("Scraping: %s (page %d)", source_name, p_num)
                    p_url = f"{url}-{p_num}" if p_num > 1 else url
                    p_html = await self.get_page(p_url)
                    page_posts = self.parse_posts(p_html, source_name)
                    all_posts.extend(page_posts)
                    logger.info("-> %d posts found", len(page_posts))
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error("Error scraping %s: %s", url, e)
        return all_posts

# ===========================================================================
# MODULE 2: VALIDATOR
# ===========================================================================
class ValidationEngine:
    BANNED_WORDS = {"ref", "referans", "davet", "yardim", "paramguvende", "satilik", "alici"}
    BLOCKED_DOMAINS = {"youtube.com", "youtu.be", "twitter.com", "instagram.com"}

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    def _clean_url(self, link: str) -> str:
        return link.split("?")[0].split("#")[0]

    async def _resolve_link(self, link: str) -> Optional[str]:
        if "donanimhaber.com/store" in link:
            try:
                r = await self.client.head(link, timeout=5)
                return self._clean_url(str(r.url))
            except Exception:
                return None
        return link

    async def validate_all(self, posts: list[ForumPost]) -> list[ForumPost]:
        valid: list[ForumPost] = []
        for p in posts:
            if not p.text.strip(): continue
            low_text = p.text.lower()
            if any(b in low_text for b in self.BANNED_WORDS): continue

            resolved = []
            for rlink in p.raw_links:
                res = await self._resolve_link(rlink)
                if not res: continue
                domain = urlparse(res).netloc.lower()
                if any(bd in domain for b in self.BLOCKED_DOMAINS): continue
                resolved.append(res)
            
            p.resolved_links = list(set(resolved))
            if p.resolved_links:
                valid.append(p)
                
        logger.info("%d / %d posts passed validation.", len(valid), len(posts))
        return valid

# ===========================================================================
# MODULE 3: LLM AGENT
# ===========================================================================
class LLMAgent:
    def __init__(self) -> None:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is missing!")
        self.client = genai.Client(api_key=GEMINI_API_KEY)

    def _discover_models(self) -> list[str]:
        return ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-2.5-pro"]

    def _build_prompt(self, posts: list[ForumPost]) -> str:
        lines = [
            "GOREVIN:",
            "Sana verilen forum mesajlarindaki gecerli ve indirimli TUM URUNLERI cikar.",
            "SAKIN eleme yapip 2-3 tane urun birakma! Mesajlarda bulunan butun linkli urunleri JSON'a ekle.",
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
            if p.resolved_links:
                lines.append(f"    Link: {p.resolved_links[0]}")
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
                    u_adi = item.get("urun_adi", "Ürün").replace("<", "").replace(">", "")
                    fiyat = item.get("fiyat", "Belirsiz").replace("<", "").replace(">", "")
                    link = item.get("link", "#")
                    kaynak = item.get("kaynak", "Link").replace("<", "").replace(">", "")
                    if not link.startswith("http"): link = "#"
                    bulten += f"• <b>{u_adi}</b> — <code>{fiyat}</code> | <a href='{link}'>Satın Al</a> (<i>{kaynak}</i>)\n"
                    total_items += 1
                bulten += "\n"
        
        if total_items == 0:
            return "<b>ℹ️ Bugün kazınan geçerli bir fırsat bulunamadı.</b>"

        bulten += "<i>📅 Kaynak: DonanımHaber Forum</i>"
        return bulten

    async def analyze(self, posts: list[ForumPost]) -> str:
        if not posts:
            return "<b>ℹ️ Bugün paylaşılan geçerli bir fırsat bulunamadı.</b>"

        now = datetime.now(TZ_ISTANBUL)
        MONTHS_TR = {1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran", 7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık"}
        run_date = f"{now.day} {MONTHS_TR[now.month]} {now.year}"
        
        prompt = self._build_prompt(posts)
        logger.info("Sending %d posts to Gemini for JSON extraction...", len(posts))

        sys_instr = "Sen profesyonel bir veri ayıklayıcısın. Metinden tüm ürünleri yakala ve SADECE JSON formatında ver."
        raw = ""
        last_error = None
        
        for m_name in self._discover_models():
            try:
                logger.info("Trying Gemini model: %s", m_name)
                response = self.client.models.generate_content(
                    model=m_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        system_instruction=sys_instr,
                        response_mime_type="application/json",
                        max_output_tokens=8000,
                    ),
                )
                raw = response.text or ""
                if raw: break
            except Exception as exc:
                last_error = exc

        if not raw:
            return f"<b>⚠️ AI analizi sırasında hata oluştu:</b> <code>{last_error}</code>"

        try:
            raw_clean = raw.strip()
            if raw_clean.startswith("```"):
                raw_clean = re.sub(r"^```(?:json)?\s*", "", raw_clean, flags=re.IGNORECASE)
                raw_clean = re.sub(r"\s*```$", "", raw_clean)
            
            s = raw_clean.find("{")
            e = raw_clean.rfind("}")
            if s != -1 and e != -1:
                raw_clean = raw_clean[s:e+1]
                
            data = json.loads(raw_clean)
            bulletin = self._json_to_html(data, run_date)
            logger.info("Bulletin successfully generated via JSON (%d chars).", len(bulletin))
            return bulletin
        except Exception as e:
            logger.error("JSON Error: %s | Raw: %s", e, raw[:200])
            return "<b>⚠️ AI veriyi json formatına çevirirken hata yaptı.</b>"

# ===========================================================================
# MODULE 4: NOTIFIER
# ===========================================================================
class TelegramNotifier:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    def _split(self, text: str) -> list[str]:
        max_len = 4000
        if len(text) <= max_len: return [text]
        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break
            cut = text.rfind("\n", 0, max_len)
            if cut == -1: cut = max_len
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        return chunks

    async def send(self, text: str) -> None:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.error("Cannot send Telegram message: Credentials missing.")
            return

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
                logger.info("Telegram HTML message sent (%d/%d).", idx, len(chunks))
            except Exception as e:
                logger.error("Telegram HTML failed (%d/%d): %s", idx, len(chunks), e)
            if len(chunks) > 1:
                await asyncio.sleep(1)

# ===========================================================================
# MAIN FLOW
# ===========================================================================
async def main() -> None:
    logger.info("=" * 60)
    logger.info("FirsatKaziyici v3.0 started -- %s", datetime.now(TZ_ISTANBUL).isoformat())
    logger.info("=" * 60)

    missing = [k for k in ["GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"] if not os.environ.get(k)]
    
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0, follow_redirects=True) as http_client:
        notifier = TelegramNotifier(http_client)
        if missing:
            return logger.error("Missing secrets: %s", missing)

        try:
            scraper = ForumScraper(http_client)
            raw_posts = await scraper.scrape_all()
            if not raw_posts: return await notifier.send("<b>ℹ️ Fırsat yok.</b>")

            valid_posts = await ValidationEngine(http_client).validate_all(raw_posts)
            valid_posts.sort(key=lambda p: p.likes, reverse=True)

            agent = LLMAgent()
            bulletin = await agent.analyze(valid_posts) # Sends ALL valid posts
            await notifier.send(bulletin)
        except Exception as e:
            logger.exception("Critical err: %s", e)

if __name__ == "__main__":
    asyncio.run(main())