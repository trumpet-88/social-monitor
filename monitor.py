import os
import re
import logging
import cloudscraper
from pymongo import MongoClient

# Configure logging to show in console
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', force=True)
logger = logging.getLogger(__name__)

# Load environment variables
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
HF_API_TOKEN = os.getenv('HF_API_TOKEN')
MONGODB_URI = os.getenv('MONGODB_URI')

# Confidence threshold for actionable signals
CONFIDENCE_THRESHOLD = 0.80

# Define keywords that you consider "actionable"
ACTIONABLE_KEYWORDS = ['buy', 'sell', 'stocks', 'tariff', 'sanctions']

# Connect to MongoDB
client = MongoClient(MONGODB_URI)
db = client['truth_social_monitor']
collection = db['last_processed']

def get_last_processed():
    try:
        doc = collection.find_one()
        return doc['post_id'] if doc else None
    except Exception as e:
        logger.error(f"MongoDB error getting last processed: {e}")
        return None

def update_last_processed(post_id):
    try:
        collection.update_one({}, {'$set': {'post_id': post_id}}, upsert=True)
        logger.info(f"Updated last processed post ID to: {post_id}")
    except Exception as e:
        logger.error(f"MongoDB error updating last processed: {e}")

def send_telegram_message(message):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {'chat_id': CHAT_ID, 'text': message}
    try:
        # Use cloudscraper to send the Telegram message
        response = cloudscraper.create_scraper().post(url, data=payload)
        if response.status_code == 200:
            logger.info("Telegram message sent successfully")
        else:
            logger.error(f"Failed to send Telegram message: {response.text}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def classify_post(post_text):
    api_url = 'https://api-inference.huggingface.co/models/facebook/bart-large-mnli'
    headers = {'Authorization': f'Bearer {HF_API_TOKEN}'}
    payload = {
        'inputs': post_text,
        'parameters': {'candidate_labels': ['buy stocks', 'sell stocks', 'neutral']}
    }
    try:
        logger.info(f"Classifying post: {post_text[:50]}...")
        response = cloudscraper.create_scraper().post(api_url, headers=headers, json=payload)
        if response.status_code == 200:
            result = response.json()
            # Ensure we got both labels and scores
            if isinstance(result, dict) and 'labels' in result and 'scores' in result:
                label = result['labels'][0]
                score = result['scores'][0]
                logger.info(f"Model classified as '{label}' with confidence {score:.2f}")
                return label, score
            else:
                logger.error(f"Unexpected API response format: {result}")
                return None, None
        else:
            logger.error(f"Hugging Face API error: {response.status_code} - {response.text}")
            return None, None
    except Exception as e:
        logger.error(f"API request error: {e}")
        return None, None

def strip_html(html_content):
    # Remove any HTML tags from the content.
    return re.sub(r'<[^>]+>', '', html_content).strip()

def contains_actionable_keywords(text):
    text_lower = text.lower()
    return any(kw in text_lower for kw in ACTIONABLE_KEYWORDS)

def main():
    logger.info("Starting Truth Social monitor script...")
    last_processed = get_last_processed()
    logger.info(f"Last processed post ID: {last_processed}")

    api_url = 'https://truthsocial.com/api/v1/accounts/107780257626128497/statuses?exclude_replies=true&only_replies=false&with_muted=true'

    try:
        scraper = cloudscraper.create_scraper()
        # Set browser-like headers
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "en-GB,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36",
            "sec-ch-ua": "\"Google Chrome\";v=\"131\", \"Chromium\";v=\"131\", \"Not_A Brand\";v=\"24\"",
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": "\"macOS\"",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Referer": "https://truthsocial.com/"
        }

        logger.info(f"Fetching posts from API: {api_url}")
        response = scraper.get(api_url, headers=headers)
        if response.status_code != 200:
            logger.error(f"Error fetching posts: {response.status_code} - {response.text}")
            return

        posts = response.json()
        logger.info(f"Fetched {len(posts)} posts from the API")

        for post in posts:
            post_id = post.get('id')
            if not post_id:
                logger.warning("Post missing id, skipping...")
                continue

            # Skip posts already processed (if available)
            if last_processed and post_id == last_processed:
                logger.info("Reached last processed post, stopping...")
                break

            raw_content = post.get('content', '')
            post_text = strip_html(raw_content)
            if not post_text:
                logger.warning(f"No text content for post ID {post_id}, skipping...")
                update_last_processed(post_id)
                continue

            logger.info(f"Processing post ID {post_id}: {post_text[:50]}...")
            label, score = classify_post(post_text)
            # If classification fails or confidence is too low, do not update mongo
            if label is None or score is None or score < CONFIDENCE_THRESHOLD:
                logger.warning(f"Uncertain classification for post ID {post_id} (score: {score}). Will retry next time.")
                continue

            # If label is 'neutral', mark as processed without alerting
            if label == 'neutral':
                logger.info(f"Post ID {post_id} classified as neutral.")
                update_last_processed(post_id)
                continue

            # Optional: ensure the post text has at least one actionable keyword.
            if not contains_actionable_keywords(post_text):
                logger.info(f"Post ID {post_id} lacks actionable keywords. Marking as neutral.")
                update_last_processed(post_id)
                continue

            # If we've reached here, we consider the post actionable.
            message = f"{label.upper()}: {post_text}"
            send_telegram_message(message)
            logger.info(f"Sent alert for post ID {post_id}: {label} (confidence {score:.2f})")

            # Mark as processed after a successful actionable alert
            update_last_processed(post_id)

    except Exception as e:
        logger.error(f"Error in main: {e}")

if __name__ == '__main__':
    main()
