#!/usr/bin/env python3
"""
Truth-Social market-signal monitor
──────────────────────────────────
 • Cloudflare-aware (Cloudscraper + rotating UA)
 • First attempts a direct fetch (no proxy)
 • On failure, auto-picks and rotates proxies from GitHub lists (up to 5 tries)
 • Loud, timestamped logging at every step
"""
import os
import re
import sys
import json
import time
import random
import logging
import concurrent.futures
from typing import Tuple, Optional, List

import requests
import cloudscraper
from pymongo import MongoClient, errors as mongo_errors

# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Environment
# ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GROQ_API_TOKEN = os.getenv("GROQ_API_TOKEN")
MONGODB_URI = os.getenv("MONGODB_URI")
CF_CLEARANCE: Optional[str] = os.getenv("CF_CLEARANCE")  # Cloudflare cookie

if not GROQ_API_TOKEN:
    logger.critical("GROQ_API_TOKEN is not set – exiting.")
    sys.exit(1)

GROQ_BASE_URL = os.getenv("GROQ_API_BASE", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama3-70b-8192")
CONFIDENCE_THRESHOLD = 0.80

# ──────────────────────────────────────────────────────────────
# Proxy lists settings
# ──────────────────────────────────────────────────────────────
_FREE_LISTS: List[str] = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/main/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/proxies/http.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
]

_MAX_CANDIDATES = 30      # per list
_PICKER_TIMEOUT = 20      # seconds total for proxy hunt
_TEST_TIMEOUT = 5         # timeout per proxy test

PROXY_URL: Optional[str] = None  # will be set on fallback

# ──────────────────────────────────────────────────────────────
# Helpers to pick and test proxies
# ──────────────────────────────────────────────────────────────
def _proxy_works(proxy: str) -> bool:
    """Return True if proxy can reach api.ipify.org within TEST_TIMEOUT."""
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    try:
        r = requests.get(
            "https://api.ipify.org?format=json",
            timeout=_TEST_TIMEOUT,
            proxies={"http": proxy, "https": proxy},
        )
        return r.ok and "application/json" in r.headers.get("Content-Type", "")
    except Exception:
        return False

def _pick_free_proxy() -> Optional[str]:
    """Pick a working proxy from the GitHub lists (no FreeProxy)."""
    start = time.time()
    def timed_out() -> bool:
        return time.time() - start > _PICKER_TIMEOUT

    random.shuffle(_FREE_LISTS)
    for url in _FREE_LISTS:
        if timed_out():
            break
        try:
            txt = requests.get(url, timeout=7).text
            pool = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            random.shuffle(pool)
            pool = pool[:_MAX_CANDIDATES]
            if not pool:
                continue
            logger.info(f"Testing {len(pool)} proxies from {url.split('/',5)[2]}")
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as exe:
                for proxy, ok in zip(pool, exe.map(_proxy_works, pool)):
                    if ok:
                        chosen = proxy if "://" in proxy else f"http://{proxy}"
                        logger.info(f"Found working proxy {chosen}")
                        return chosen
        except Exception as exc:
            logger.debug(f"Proxy source {url} failed: {exc}")

    logger.warning("No proxies found in lists")
    return None

# ──────────────────────────────────────────────────────────────
# Cloudscraper factory
# ──────────────────────────────────────────────────────────────
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

def get_scraper(proxy: Optional[str] = None) -> cloudscraper.CloudScraper:
    ua = random.choice(UA_POOL)
    scraper = cloudscraper.create_scraper(
        browser={"custom": ua, "platform": "windows" if "Windows" in ua else "mac"},
        delay=10,
    )
    if CF_CLEARANCE:
        scraper.cookies.set("cf_clearance", CF_CLEARANCE, domain=".truthsocial.com")
    if proxy:
        scraper.proxies.update({"http": proxy, "https": proxy})
    return scraper

# ──────────────────────────────────────────────────────────────
# Resilient fetch: direct first, then proxies
# ──────────────────────────────────────────────────────────────
BASE_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Pragma": "no-cache",
}

def fetch_json_with_retries(url: str, max_retries: int = 5, timeout: int = 25):
    # 1) Try direct fetch
    logger.info("Attempting direct fetch (no proxy)")
    try:
        direct_scraper = get_scraper(proxy=None)
        resp = direct_scraper.get(url, headers=BASE_HEADERS, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        else:
            logger.warning(f"Direct fetch HTTP {resp.status_code}, falling back to proxy")
    except Exception as exc:
        logger.warning(f"Direct fetch failed: {exc}")

    # 2) Fallback to proxy attempts
    backoff = 5
    for attempt in range(1, max_retries + 1):
        proxy = _pick_free_proxy()
        if not proxy:
            logger.warning("No proxy available to retry; aborting.")
            break
        logger.info(f"Attempt {attempt}/{max_retries} via proxy {proxy}")
        try:
            scraper = get_scraper(proxy=proxy)
            resp = scraper.get(url, headers=BASE_HEADERS, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"Proxy fetch HTTP {resp.status_code}")
        except Exception as exc:
            logger.warning(f"Proxy fetch failed: {exc}")
        time.sleep(backoff)
        backoff *= 2

    logger.critical("All retries failed; aborting.")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────
# Database setup
# ──────────────────────────────────────────────────────────────
client: Optional[MongoClient] = None
collection = None
if MONGODB_URI:
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10_000)
        client.admin.command("ping")
        db = client["truth_social_monitor"]
        collection = db["last_processed"]
    except mongo_errors.PyMongoError as err:
        logger.critical(f"MongoDB connection error: {err}")
        sys.exit(1)
else:
    logger.critical("MONGODB_URI is not set – exiting.")
    sys.exit(1)

def get_last_processed() -> Optional[str]:
    try:
        doc = collection.find_one()
        return doc["post_id"] if doc else None
    except mongo_errors.PyMongoError as err:
        logger.error(f"MongoDB read error: {err}")
        return None

def update_last_processed(post_id: str) -> None:
    try:
        collection.update_one({}, {"$set": {"post_id": post_id}}, upsert=True)
    except mongo_errors.PyMongoError as err:
        logger.error(f"MongoDB write error: {err}")

# ──────────────────────────────────────────────────────────────
# Telegram alert helper
# ──────────────────────────────────────────────────────────────
def send_telegram_message(message: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram credentials missing – cannot send alert.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        scraper = get_scraper(proxy=None)
        r = scraper.post(url, data=payload, timeout=15)
        r.raise_for_status()
        logger.info("Telegram message sent")
    except Exception as exc:
        logger.error(f"Telegram error: {exc}")

# ──────────────────────────────────────────────────────────────
# Groq chat completion
# ──────────────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "You are a veteran macro-trader and rigorous policy analyst.\n"
    "**Definitions:**\n"
    "- BULLISH: a policy action likely to **boost** economic growth or markets (e.g. tax cuts, tariffs to protect domestic industry).\n"
    "- BEARISH: a policy action likely to **weigh on** growth or markets (e.g. tax hikes, restrictive trade measures).\n"
    "- NEUTRAL: anything else (blame, vague rhetoric, unquantified forecasts).\n"
    "\n"
    "For the single Trump post provided, do the following:\n"
    "1. **Rhetorical Audit**: List any blame, misleading claims, vague forecasts, hyperbole, or falsehoods (these are NOT policy actions).\n"
    "2. **Policy Extraction**: List only **specific, quantifiable** economic actions (e.g. “15% tariff on steel imports,” “cut corporate tax from 21% to 15%,” “authorize $500 billion infrastructure bill”).\n"
    "3. **Self-Critique**: Review each extracted bullet—discard any lacking a clear % or $ amount or an explicit legislative reference.\n"
    "4. **Classification**: Based *solely* on the remaining bullets, output exactly two lines:\n"
    "   Classification: <bullish|bearish|neutral>\n"
    "   Explanation: ≤20 words, citing the precise lever(s).\n"
    "   If **no** bullets remain after critique, Classification must be NEUTRAL.\n"
)

USER_PROMPT_TMPL = (
    "Classify the following Trump post and respond in the required two-line format.\n\nPost:\n{post}\n"
)

def analyze_post_with_groq(post_text: str) -> Tuple[Optional[str], Optional[str], float]:
    api_url = f"{GROQ_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TMPL.format(post=post_text)},
        ],
    }

    try:
        r = requests.post(api_url, headers=headers, json=payload, timeout=30)
        if r.status_code != 200:
            logger.error(f"Groq API {r.status_code}: {r.text[:200]}")
            return None, None, 0.0
        data = r.json()
        content = data["choices"][0]["message"]["content"].strip()
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if len(lines) < 2:
            logger.error(f"Unexpected Groq output: {content}")
            return None, None, 0.0
        cls = lines[0].split(":", 1)[1].strip().lower()
        expl = lines[1].split(":", 1)[1].strip()
        return cls, expl, 1.0
    except Exception as exc:
        logger.error(f"Groq API request error: {exc}")
        return None, None, 0.0

# ──────────────────────────────────────────────────────────────
# HTML stripping helper
# ──────────────────────────────────────────────────────────────
def strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").strip()

# ──────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────
def main() -> None:
    logger.info("Truth-Social monitor starting …")
    last_processed = get_last_processed()

    api_url = (
        "https://truthsocial.com/api/v1/accounts/107780257626128497/"
        "statuses?exclude_replies=true&only_replies=false&with_muted=true"
    )

    posts = fetch_json_with_retries(api_url)
    logger.info(f"Fetched {len(posts)} posts")

    for post in reversed(posts):
        post_id = post.get("id")
        if not post_id:
            continue
        if last_processed and post_id <= last_processed:
            logger.info(f"{post_id} already processed")
            continue

        post_text = strip_html(post.get("content", ""))
        if not post_text:
            logger.debug(f"Post {post_id} has no text – skipped.")
            update_last_processed(post_id)
            continue

        classification, explanation, conf = analyze_post_with_groq(post_text)
        if not classification or conf < CONFIDENCE_THRESHOLD:
            logger.warning(f"Could not classify post {post_id}; will retry later.")
            continue

        logger.info(f"Post {post_id}: {classification.upper()} – {explanation}")
        if classification in {"bullish", "bearish"}:
            send_telegram_message(
                f"{classification.upper()}: {post_text}\nReason: {explanation}"
            )
        update_last_processed(post_id)

    logger.info("Run completed successfully.")

if __name__ == "__main__":
    main()
