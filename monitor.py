#!/usr/bin/env python3
"""
Truth-Social market-signal monitor
──────────────────────────────────
• 1 direct fetch → up to 5 rotating proxies (env-proxy first)
• Cloudflare-aware (Cloudscraper)
• Strict Trump-post macro prompt → 2-line output parsed with regex
• Telegram alerts for BULLISH / BEARISH posts
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import random
import re
import sys
import time
from typing import List, Optional, Tuple

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
if not GROQ_API_TOKEN:
    logger.critical("GROQ_API_TOKEN is not set – exiting.")
    sys.exit(1)

GROQ_BASE_URL = os.getenv("GROQ_API_BASE", "https://api.groq.com/openai/v1")
# Switched to a more powerful, instruction-following model available on Groq.
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama3-70b-8192")
CONFIDENCE_THRESHOLD = 0.80

MONGODB_URI = os.getenv("MONGODB_URI")
MANUAL_PROXY = os.getenv("PROXY_URL")       # optional user-supplied proxy
CF_CLEARANCE = os.getenv("CF_CLEARANCE")    # optional Cloudflare cookie

# ──────────────────────────────────────────────────────────────
# Prompt (REVISED FOR ACCURACY)
# ──────────────────────────────────────────────────────────────
# This prompt has been significantly enhanced to reduce false positives by forcing
# the model to follow a strict, multi-step analysis.
SYSTEM_PROMPT = (
    "You are a highly skeptical and rigorous financial analyst specializing in political risk. "
    "Your single task is to identify immediate, credible, and market-moving economic policy "
    "actions announced by Donald Trump in a given post.\n\n"
    "**Your analysis must follow these five steps strictly:**\n\n"
    "1.  **Policy Extraction:**\n"
    "    * Identify ONLY specific, quantifiable, and actionable economic policy proposals.\n"
    "    * Examples of valid policies: 'I am signing an executive order to place a 10% tariff on all imported goods,' "
    "'We will cut the corporate tax rate to 15%.'\n"
    "    * Extract these as potential policy bullets. If none, the post is NEUTRAL.\n\n"
    "2.  **Actor & Authority Test:**\n"
    "    * For each extracted bullet, confirm the actor is Donald Trump or his administration with the direct authority to implement it.\n"
    "    * DISCARD any bullet that is:\n"
    "        * **Criticism of others:** e.g., 'Jerome Powell should lower rates.' (Trump can't force him).\n"
    "        * **Ascribing intent to opponents:** e.g., 'The Democrats want to raise your taxes by 68%.'\n"
    "        * **A conditional threat/promise:** e.g., 'If this bill doesn't pass, taxes will rise.'\n"
    "        * **A call to action for others:** e.g., 'Congress must pass this bill.'\n\n"
    "3.  **Rhetoric & Reality Check:**\n"
    "    * Examine the remaining bullets.\n"
    "    * DISCARD any bullet that is clearly:\n"
    "        * **Rhetorical Blame or Praise:** Blaming past administrations, praising his own unquantified achievements.\n"
    "        * **Vague Hyperbole:** 'A boom is coming,' 'We will have the best economy ever.'\n"
    "        * **Fundraising or Gimmickry:** e.g., Selling 'Trump Cards' for fantastical sums of money. This is not fiscal policy.\n\n"
    "4.  **Final Classification:**\n"
    "    * Review the bullets that survived all prior steps.\n"
    "    * If **zero** bullets remain, the classification MUST be **NEUTRAL**.\n"
    "    * If bullets remain, classify based on the following strict definitions:\n"
    "        * **BULLISH:** A specific action likely to immediately **boost** broad market indices (e.g., tax cuts, deregulation, lifting tariffs).\n"
    "        * **BEARISH:** A specific action likely to immediately **weigh on** broad market indices (e.g., new tariffs, escalating military/trade conflicts).\n"
    "        * **NEUTRAL:** Anything else. This is the default.\n\n"
    "5.  **Output Format:**\n"
    "    * You MUST provide your response in exactly two lines, and nothing else.\n"
    "    * Line 1: `Classification: <BULLISH|BEARISH|NEUTRAL>`\n"
    "    * Line 2: `Explanation: <A justification of ≤ 20 words based *only* on the surviving policy bullets.>`\n"
    "    * If NEUTRAL, the explanation should state why (e.g., 'Political rhetoric with no specific policy action.')."
)
USER_PROMPT_TMPL = (
    "Classify the following Trump post and respond in the required two-line format.\n\nPost:\n{post}\n"
)

# ──────────────────────────────────────────────────────────────
# Proxy infrastructure (GitHub raw lists only)
# ──────────────────────────────────────────────────────────────
FREE_LISTS: List[str] = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/main/http.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/proxies/http.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
]
MAX_CANDIDATES = 30
PICKER_TIMEOUT = 20
TEST_TIMEOUT = 5

def _proxy_works(proxy: str) -> bool:
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    try:
        r = requests.get(
            "https://api.ipify.org?format=json",
            timeout=TEST_TIMEOUT,
            proxies={"http": proxy, "https": proxy},
        )
        return r.ok and "application/json" in r.headers.get("Content-Type", "")
    except Exception:
        return False

def pick_free_proxy() -> Optional[str]:
    start = time.time()
    def timed_out(): return time.time() - start > PICKER_TIMEOUT
    lists = FREE_LISTS.copy()
    random.shuffle(lists)

    for url in lists:
        if timed_out():
            break
        try:
            txt = requests.get(url, timeout=7).text
            pool = [ln.strip() for ln in txt.splitlines() if ln.strip()]
            random.shuffle(pool)
            pool = pool[:MAX_CANDIDATES]
            if not pool:
                continue
            logger.info(f"Testing {len(pool)} proxies from {url.split('/',5)[2]}")
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as exe:
                for proxy, ok in zip(pool, exe.map(_proxy_works, pool)):
                    if ok:
                        logger.info(f"Working proxy: {proxy}")
                        return proxy if "://" in proxy else f"http://{proxy}"
        except Exception as exc:
            logger.debug(f"Proxy source {url} failed: {exc}")
    return None

# ──────────────────────────────────────────────────────────────
# Database
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
# HTTP helpers
# ──────────────────────────────────────────────────────────────
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

BASE_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Pragma": "no-cache",
}

def make_scraper(proxy: Optional[str] = None) -> cloudscraper.CloudScraper:
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

def fetch_json_with_retries(url: str, timeout: int = 25, max_proxy_attempts: int = 5):
    attempt = 0
    proxy: Optional[str] = None
    while True:
        scraper = make_scraper(proxy)
        headers = {**BASE_HEADERS, "User-Agent": scraper.headers["User-Agent"]}
        via = "direct" if proxy is None else f"proxy ({proxy.split('://')[-1]})"
        logger.info(f"Attempt {attempt + 1}: GET {url.split('/',3)[2]} via {via}")

        try:
            resp = scraper.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in {403, 429, 502, 503}:
                raise requests.RequestException(f"HTTP {resp.status_code}")
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(f"{exc} – switching route")
            attempt += 1
            if attempt > max_proxy_attempts:
                break
            if attempt == 1 and MANUAL_PROXY:
                proxy = MANUAL_PROXY
            else:
                proxy = pick_free_proxy()
            if not proxy:
                logger.warning("No proxy available – retrying direct")
                proxy = None
            continue

    logger.critical("All attempts failed – aborting.")
    sys.exit(1)

# ──────────────────────────────────────────────────────────────
# Telegram helper
# ──────────────────────────────────────────────────────────────
def send_telegram_message(message: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram credentials missing – cannot send alert.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        scraper = make_scraper()
        r = scraper.post(url, data=payload, timeout=15)
        r.raise_for_status()
        logger.info("Telegram message sent")
    except Exception as exc:
        logger.error(f"Telegram error: {exc}")

# ──────────────────────────────────────────────────────────────
# Groq chat completion
# ──────────────────────────────────────────────────────────────
_cls_pat = re.compile(r"classification\s*:\s*(bullish|bearish|neutral)", re.I)
_exp_pat = re.compile(r"explanation\s*:\s*(.+)", re.I)

def _last_match(regex: re.Pattern, text: str) -> Optional[str]:
    matches = regex.findall(text)
    return matches[-1].strip() if matches else None

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
        r.raise_for_status()
    except Exception as exc:
        logger.error(f"Groq API request error: {exc}")
        return None, None, 0.0

    content = r.json()["choices"][0]["message"]["content"]

    classification = _last_match(_cls_pat, content)
    explanation   = _last_match(_exp_pat, content)

    if classification and explanation:
        return classification.lower(), explanation, 1.0

    logger.error(f"Could not parse Groq answer:\n{content}")
    return None, None, 0.0

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────
def strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").strip()

# ──────────────────────────────────────────────────────────────
# Main
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

    # Process oldest to newest to maintain order
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

        logger.info(f"Analyzing post {post_id}...")
        classification, explanation, conf = analyze_post_with_groq(post_text)
        if classification is None or conf < CONFIDENCE_THRESHOLD:
            logger.warning(f"Could not classify post {post_id}; will retry later.")
            continue

        logger.info(f"Result for {post_id}: {classification.upper()} – {explanation}")
        if classification in {"bullish", "bearish"}:
            send_telegram_message(
                f"{classification.upper()}: {post_text}\n\nReason: {explanation}"
            )
        update_last_processed(post_id)

    logger.info("Run completed successfully.")

if __name__ == "__main__":
    main()