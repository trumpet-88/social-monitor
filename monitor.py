import os
import re
import sys
import json
import logging
from typing import Tuple, Optional

import requests
import cloudscraper
from pymongo import MongoClient, errors as mongo_errors

# -------------------------------------------------------------
# Configuration & Environment
# -------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)

# Environment variables (fail–fast if any critical ones are missing)
TELEGRAM_TOKEN: Optional[str] = os.getenv("TELEGRAM_TOKEN")
CHAT_ID: Optional[str] = os.getenv("CHAT_ID")
GROQ_API_TOKEN: Optional[str] = os.getenv("GROQ_API_TOKEN")
MONGODB_URI: Optional[str] = os.getenv("MONGODB_URI")

if GROQ_API_TOKEN is None:
    logger.critical("GROQ_API_TOKEN is not set – exiting.")
    sys.exit(1)

# Allow overriding the base URL / model via ENV for flexibility
GROQ_BASE_URL = os.getenv("GROQ_API_BASE", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

CONFIDENCE_THRESHOLD = 0.80

# -------------------------------------------------------------
# Database helpers (connect once, fail if unreachable)
# -------------------------------------------------------------
client: Optional[MongoClient] = None
collection = None
if MONGODB_URI:
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10_000)
        # Force connection on a request as the
        client.admin.command("ping")
        db = client["truth_social_monitor"]
        collection = db["last_processed"]
    except mongo_errors.PyMongoError as err:
        logger.critical(f"Unable to connect to MongoDB at {MONGODB_URI}: {err}")
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

# -------------------------------------------------------------
# Telegram helper
# -------------------------------------------------------------

def send_telegram_message(message: str) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram credentials missing – cannot send alert.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        r = cloudscraper.create_scraper().post(url, data=payload, timeout=15)
        r.raise_for_status()
        logger.info("Telegram message sent")
    except Exception as exc:
        logger.error(f"Telegram error: {exc}")

# -------------------------------------------------------------
# Groq chat completion wrapper
# -------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a seasoned financial market analyst.  "
    "Classify Trump posts into: bullish, bearish, or neutral – based on likely market impact.  "
    "Respond with exactly two lines: 1) 'Classification: <bullish|bearish|neutral>' 2) 'Explanation: <reason>'."
)
USER_PROMPT_TMPL = """Classify the following Trump post and respond in the required two‑line format.\n\nPost:\n{post}\n"""

def analyze_post_with_groq(post_text: str) -> Tuple[Optional[str], Optional[str], float]:
    api_url = f"{GROQ_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TMPL.format(post=post_text)},
        ],
    }

    try:
        r = requests.post(api_url, headers=headers, json=payload, timeout=30)
        if r.status_code != 200:
            logger.error(
                f"Groq API {r.status_code}: {r.text[:200]} – payload model={GROQ_MODEL}"
            )
            return None, None, 0.0
        data = r.json()
        content = data["choices"][0]["message"]["content"].strip()
        lines = [ln.strip() for ln in content.split("\n") if ln.strip()]
        if len(lines) < 2:
            logger.error(f"Unexpected Groq output: {content}")
            return None, None, 0.0
        classification = lines[0].split(":", 1)[-1].strip().lower()
        explanation = lines[1].split(":", 1)[-1].strip()
        return classification, explanation, 1.0
    except Exception as exc:
        logger.error(f"Groq API request error: {exc}")
        return None, None, 0.0

# -------------------------------------------------------------
# Helpers
# -------------------------------------------------------------

def strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "").strip()

# -------------------------------------------------------------
# Main
# -------------------------------------------------------------

def main() -> None:
    logger.info("Truth Social monitor starting …")
    last_processed = get_last_processed()

    api_url = (
        "https://truthsocial.com/api/v1/accounts/107780257626128497/"
        "statuses?exclude_replies=true&only_replies=false&with_muted=true"
    )

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; TruthMonitor/1.0)",
        "Cache-Control": "no-cache",
    }

    scraper = cloudscraper.create_scraper()
    try:
        response = scraper.get(api_url, headers=headers, timeout=30)
        if response.status_code != 200:
            logger.critical(
                f"Truth Social API HTTP {response.status_code}: {response.text[:200]}"
            )
            sys.exit(1)
        try:
            posts = response.json()
        except json.JSONDecodeError as je:
            logger.critical(f"Truth Social response is not JSON: {je}")
            sys.exit(1)
    except Exception as exc:
        logger.critical(f"Error fetching Truth Social posts: {exc}")
        sys.exit(1)

    logger.info(f"Fetched {len(posts)} posts …")

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
        if classification is None or conf < CONFIDENCE_THRESHOLD:
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
