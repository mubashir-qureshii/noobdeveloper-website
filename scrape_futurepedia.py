"""
Futurepedia AI Agents Scraper
==============================
Scrapes https://www.futurepedia.io/ai-tools/ai-agents using Selenium with
infinite-scroll detection.  Saves results to Excel (.xlsx) and CSV.

Usage:
    pip install selenium openpyxl pandas
    # ChromeDriver must be on PATH or managed by webdriver-manager
    python scrape_futurepedia.py
"""

import time
import re
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)

# ── Optional: automatic ChromeDriver management ────────────────────────────────
try:
    from webdriver_manager.chrome import ChromeDriverManager

    _USE_WDM = True
except ImportError:
    _USE_WDM = False

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
TARGET_URL = "https://www.futurepedia.io/ai-tools/ai-agents"
OUTPUT_EXCEL = "futurepedia_ai_agents.xlsx"
OUTPUT_CSV = "futurepedia_ai_agents.csv"

SCROLL_PAUSE = 2.5          # seconds to wait after each scroll
MAX_STALE_SCROLLS = 8       # stop after this many scrolls with no new cards
INITIAL_PAGE_WAIT = 8       # seconds to wait for first render
TOOL_CARD_SELECTOR = "a[href*='/tool/']"


# ── Data model ─────────────────────────────────────────────────────────────────
@dataclass
class AITool:
    name: str = "Unknown"
    description: str = ""
    pricing: str = "Check website"
    rating: Optional[float] = None
    reviews: Optional[int] = None
    url: str = ""


# ── Scraper class ──────────────────────────────────────────────────────────────
class FuturepediaScraper:
    """Scrapes all AI-agent tool cards from Futurepedia using Selenium."""

    # CSS selectors / text patterns tried in order
    _NAME_SELECTORS = [
        "h2", "h3", "h4",
        "[class*='title']", "[class*='name']", "[class*='heading']",
        "strong", "b",
    ]
    _DESC_SELECTORS = [
        "p",
        "[class*='desc']", "[class*='summary']", "[class*='body']",
        "[class*='text']", "[class*='content']",
    ]
    _PRICING_KEYWORDS = {
        "free": "Free",
        "freemium": "Freemium",
        "paid": "Paid",
        "premium": "Paid",
        "subscription": "Paid",
        "contact for pricing": "Paid",
        "enterprise": "Paid",
    }
    _PRICING_SELECTORS = [
        "[class*='pric']", "[class*='badge']", "[class*='tag']",
        "[class*='plan']", "[class*='tier']",
    ]

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.driver: Optional[webdriver.Chrome] = None
        self._tools: list[AITool] = []

    # ── Driver lifecycle ───────────────────────────────────────────────────────
    def _build_driver(self) -> webdriver.Chrome:
        opts = Options()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
        # Suppress "Chrome is being controlled" bar / logging noise
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument("--log-level=3")

        if _USE_WDM:
            service = Service(ChromeDriverManager().install())
            return webdriver.Chrome(service=service, options=opts)
        return webdriver.Chrome(options=opts)

    def start(self):
        log.info("Launching Chrome …")
        self.driver = self._build_driver()

    def stop(self):
        if self.driver:
            self.driver.quit()
            self.driver = None
            log.info("Browser closed.")

    # ── Scrolling ──────────────────────────────────────────────────────────────
    def _current_card_count(self) -> int:
        return len(self.driver.find_elements(By.CSS_SELECTOR, TOOL_CARD_SELECTOR))

    def _scroll_to_bottom(self):
        self.driver.execute_script(
            "window.scrollTo(0, document.body.scrollHeight);"
        )

    def load_all_tools(self):
        """Scroll the page until no new tool cards appear."""
        log.info("Navigating to %s", TARGET_URL)
        self.driver.get(TARGET_URL)

        log.info("Waiting %ds for initial page render …", INITIAL_PAGE_WAIT)
        try:
            WebDriverWait(self.driver, INITIAL_PAGE_WAIT + 5).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, TOOL_CARD_SELECTOR)
                )
            )
        except TimeoutException:
            log.warning("Timed out waiting for first card; continuing anyway.")

        time.sleep(INITIAL_PAGE_WAIT)

        stale_count = 0
        scroll_n = 0

        while stale_count < MAX_STALE_SCROLLS:
            before = self._current_card_count()
            self._scroll_to_bottom()
            time.sleep(SCROLL_PAUSE)
            after = self._current_card_count()

            scroll_n += 1
            log.info("Scroll #%d — Loaded %d tools so far …", scroll_n, after)

            if after == before:
                stale_count += 1
                # Extra wait on the first stale hit in case content is slow
                if stale_count == 1:
                    log.info("No new cards – waiting extra 4 s …")
                    time.sleep(4)
            else:
                stale_count = 0  # reset whenever we get new cards

        log.info(
            "Scrolling complete. Total tool cards found: %d",
            self._current_card_count(),
        )

    # ── Extraction helpers ─────────────────────────────────────────────────────
    @staticmethod
    def _safe_text(element, selector: str) -> str:
        try:
            el = element.find_element(By.CSS_SELECTOR, selector)
            return el.text.strip()
        except (NoSuchElementException, StaleElementReferenceException):
            return ""

    @staticmethod
    def _all_text(element, selector: str) -> list[str]:
        try:
            els = element.find_elements(By.CSS_SELECTOR, selector)
            return [e.text.strip() for e in els if e.text.strip()]
        except StaleElementReferenceException:
            return []

    def _extract_name(self, card) -> str:
        for sel in self._NAME_SELECTORS:
            txt = self._safe_text(card, sel)
            if txt:
                return txt
        return ""

    def _extract_description(self, card) -> str:
        for sel in self._DESC_SELECTORS:
            txts = self._all_text(card, sel)
            # Pick longest non-trivial paragraph
            candidates = [t for t in txts if len(t) > 30]
            if candidates:
                return max(candidates, key=len)
        return ""

    def _classify_pricing(self, raw: str) -> str:
        low = raw.lower()
        # Freemium must be checked before Free
        if "freemium" in low:
            return "Freemium"
        for kw, label in self._PRICING_KEYWORDS.items():
            if kw in low:
                return label
        return ""

    def _extract_pricing(self, card) -> str:
        # 1. Try dedicated pricing/badge elements
        for sel in self._PRICING_SELECTORS:
            for txt in self._all_text(card, sel):
                label = self._classify_pricing(txt)
                if label:
                    return label

        # 2. Fall back to full card text
        try:
            full_text = card.text
        except StaleElementReferenceException:
            full_text = ""

        label = self._classify_pricing(full_text)
        return label if label else "Check website"

    def _extract_rating_reviews(self, card) -> tuple[Optional[float], Optional[int]]:
        rating: Optional[float] = None
        reviews: Optional[int] = None

        try:
            full_text = card.text
        except StaleElementReferenceException:
            return rating, reviews

        # Rating: look for patterns like "4.5", "4.5/5", "4.5 stars"
        m_rating = re.search(
            r"\b([0-9]\.[0-9])\s*(?:/\s*5|stars?)?\b", full_text, re.IGNORECASE
        )
        if m_rating:
            val = float(m_rating.group(1))
            if 0.0 <= val <= 5.0:
                rating = val

        # Reviews: look for "(123)", "123 reviews", "123 ratings"
        m_reviews = re.search(
            r"\(?([0-9][0-9,]*)\)?\s*(?:reviews?|ratings?)", full_text, re.IGNORECASE
        )
        if not m_reviews:
            # Parenthesised number after a rating  e.g. "4.5 (200)"
            m_reviews = re.search(r"\(([0-9][0-9,]+)\)", full_text)
        if m_reviews:
            reviews = int(m_reviews.group(1).replace(",", ""))

        return rating, reviews

    def _extract_url(self, card) -> str:
        try:
            href = card.get_attribute("href") or ""
            return href.strip()
        except StaleElementReferenceException:
            return ""

    # ── Parse all cards ────────────────────────────────────────────────────────
    def parse_tools(self):
        """Extract structured data from every loaded tool card."""
        cards = self.driver.find_elements(By.CSS_SELECTOR, TOOL_CARD_SELECTOR)
        log.info("Parsing %d tool cards …", len(cards))

        seen_urls: set[str] = set()

        for idx, card in enumerate(cards, start=1):
            try:
                url = self._extract_url(card)
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                name = self._extract_name(card)
                description = self._extract_description(card)

                # Fallback: use first 200 chars of card text if no description
                if not description:
                    try:
                        fallback = card.text.strip()
                        description = fallback[:200] if fallback else "No description available"
                    except StaleElementReferenceException:
                        description = "No description available"

                pricing = self._extract_pricing(card)
                rating, reviews = self._extract_rating_reviews(card)

                tool = AITool(
                    name=name or "Unknown",
                    description=description,
                    pricing=pricing,
                    rating=rating,
                    reviews=reviews,
                    url=url,
                )
                self._tools.append(tool)

                if idx % 100 == 0:
                    log.info("  … parsed %d / %d cards", idx, len(cards))

            except StaleElementReferenceException:
                log.debug("Stale element at index %d – skipped.", idx)
                continue

        log.info("Parsing done. Unique tools: %d", len(self._tools))

    # ── Persistence ────────────────────────────────────────────────────────────
    def _build_dataframe(self) -> pd.DataFrame:
        rows = [asdict(t) for t in self._tools]
        df = pd.DataFrame(rows, columns=["name", "description", "pricing",
                                          "rating", "reviews", "url"])
        df.columns = ["Tool Name", "Description", "Pricing",
                      "Rating", "Reviews", "URL"]
        return df

    def save_excel(self, path: str = OUTPUT_EXCEL):
        df = self._build_dataframe()
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="AI Agents")
            ws = writer.sheets["AI Agents"]

            # Column widths
            col_widths = {
                "A": 30,   # Tool Name
                "B": 60,   # Description
                "C": 18,   # Pricing
                "D": 10,   # Rating
                "E": 12,   # Reviews
                "F": 60,   # URL
            }
            for col, width in col_widths.items():
                ws.column_dimensions[col].width = width

            # Wrap description column
            from openpyxl.styles import Alignment
            for row in ws.iter_rows(min_row=2, min_col=2, max_col=2):
                for cell in row:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")

        log.info("Excel saved → %s  (%d rows)", path, len(df))

    def save_csv(self, path: str = OUTPUT_CSV):
        df = self._build_dataframe()
        df.to_csv(path, index=False, encoding="utf-8-sig")
        log.info("CSV   saved → %s  (%d rows)", path, len(df))

    # ── Public entry point ─────────────────────────────────────────────────────
    def run(self):
        self.start()
        try:
            self.load_all_tools()
            self.parse_tools()

            if not self._tools:
                log.error("No tools were extracted. Check selectors / network.")
                return

            self.save_excel()
            self.save_csv()
            log.info("✅  Done!  %d AI-agent tools saved.", len(self._tools))
        finally:
            self.stop()


# ── CLI entry ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    scraper = FuturepediaScraper(headless=True)
    scraper.run()
