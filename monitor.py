import os
import re
import logging
import cloudscraper
from pymongo import MongoClient
import requests

# Configure logging to show in console
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', force=True)
logger = logging.getLogger(__name__)

# Load environment variables
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
COHERE_API_TOKEN = os.getenv('COHERE_API_TOKEN')
MONGODB_URI = os.getenv('MONGODB_URI')

# Confidence threshold for actionable signals
CONFIDENCE_THRESHOLD = 0.80

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
        response = cloudscraper.create_scraper().post(url, data=payload)
        if response.status_code == 200:
            logger.info("Telegram message sent successfully")
        else:
            logger.error(f"Failed to send Telegram message: {response.text}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def analyze_post_with_cohere_chat(post_text):
    api_url = 'https://api.cohere.ai/chat'
    headers = {
        'Authorization': f'Bearer {COHERE_API_TOKEN}',
        'Content-Type': 'application/json',
    }
    payload = {
        'message': f"""Classify this Trump tweet into one of the following categories: "bullish," "bearish," or "neutral" based on its potential impact on financial markets. Provide a brief explanation for your classification.

        **Classification Rules**:
        - **Bullish**: Indicates potential positive market impact (e.g., mentions of economic growth, tax cuts, or market-friendly policies).
        - **Bearish**: Indicates potential negative market impact (e.g., mentions of tariffs, sanctions, or economic restrictions).
        - **Neutral**: No significant market impact (e.g., political statements, accusations, or non-economic content).

        **Examples**:
        - Bullish: "We’re cutting taxes to boost the economy!"
        - Bearish: "I’m imposing new tariffs on foreign imports."
        - Neutral: "The Fake News media is at it again!"

        Tweet: '{post_text}'

        Respond in the format:
        Classification: <bullish/bearish/neutral>
        Explanation: <brief explanation>""",
        'model': 'command-r-plus',  # Updated model
        'temperature': 0.1,
    }
    try:
        logger.info(f"Analyzing post: {post_text[:50]}...")
        response = requests.post(api_url, headers=headers, json=payload)
        if response.status_code == 200:
            result = response.json()
            if 'text' in result:
                response_text = result['text'].strip()
                classification, explanation = response_text.split('\n', 1)
                classification = classification.split(': ')[1].strip().lower()
                explanation = explanation.split(': ')[1].strip()
                return classification, explanation, 1.0  # Confidence is fixed for simplicity
            else:
                logger.error(f"Unexpected API response format: {result}")
                return None, None, None
        else:
            logger.error(f"Cohere API error: {response.status_code} - {response.text}")
            return None, None, None
    except Exception as e:
        logger.error(f"API request error: {e}")
        return None, None, None

def strip_html(html_content):
    return re.sub(r'<[^>]+>', '', html_content).strip()

def main():
    logger.info("Starting Truth Social monitor script...")
    last_processed = get_last_processed()
    logger.info(f"Last processed post ID: {last_processed}")

    api_url = 'https://truthsocial.com/api/v1/accounts/107780257626128497/statuses?exclude_replies=true&only_replies=false&with_muted=true'

    try:
        scraper = cloudscraper.create_scraper()
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

        # Process posts in reverse order (newest first)
        for post in reversed(posts):
            post_id = post.get('id')
            if not post_id:
                logger.warning("Post missing id, skipping...")
                continue

            # Skip posts already processed
            if last_processed and post_id <= last_processed:
                logger.info(f"Post ID {post_id} already processed, skipping...")
                continue

            raw_content = post.get('content', '')
            post_text = strip_html(raw_content)
            if not post_text:
                logger.warning(f"No text content for post ID {post_id}, skipping...")
                update_last_processed(post_id)
                continue

            logger.info(f"Processing post ID {post_id}: {post_text[:50]}...")
            classification, explanation, confidence = analyze_post_with_cohere_chat(post_text)

            if classification is None or explanation is None or confidence < CONFIDENCE_THRESHOLD:
                logger.warning(f"Uncertain classification for post ID {post_id} (confidence: {confidence}). Will retry next time.")
                continue

            # Log the classification result
            logger.info(f"Post ID {post_id} classified as **{classification.upper()}**: {explanation}")

            # Only send alerts for bullish or bearish posts
            if classification in ['bullish', 'bearish']:
                message = f"{classification.upper()}: {post_text}\nReason: {explanation}"
                send_telegram_message(message)
                logger.info(f"Sent alert for post ID {post_id}: {classification.upper()} (confidence {confidence:.2f})")

            # Update last processed post ID regardless of classification
            update_last_processed(post_id)

    except Exception as e:
        logger.error(f"Error in main: {e}")

if __name__ == '__main__':
    main()
