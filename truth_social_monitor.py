import os
import requests
from bs4 import BeautifulSoup
from pymongo import MongoClient
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
HF_API_TOKEN = os.getenv('HF_API_TOKEN')
MONGODB_URI = os.getenv('MONGODB_URI')

# Connect to MongoDB
client = MongoClient(MONGODB_URI)
db = client['truth_social_monitor']
collection = db['last_processed']

# Function to get the last processed post ID
def get_last_processed():
    try:
        doc = collection.find_one()
        return doc['post_id'] if doc else None
    except Exception as e:
        logger.error(f"MongoDB error getting last processed: {e}")
        return None

# Function to update the last processed post ID
def update_last_processed(post_id):
    try:
        collection.update_one({}, {'$set': {'post_id': post_id}}, upsert=True)
    except Exception as e:
        logger.error(f"MongoDB error updating last processed: {e}")

# Function to send a Telegram message
def send_telegram_message(message):
    url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
    payload = {'chat_id': CHAT_ID, 'text': message}
    try:
        response = requests.post(url, data=payload)
        if response.status_code != 200:
            logger.error(f"Failed to send Telegram message: {response.text}")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# Function to classify a post using Hugging Face API
def classify_post(post_text):
    url = 'https://api-inference.huggingface.co/models/facebook/bart-large-mnli'
    headers = {'Authorization': f'Bearer {HF_API_TOKEN}'}
    payload = {
        'inputs': post_text,
        'parameters': {'candidate_labels': ['buy stocks', 'sell stocks', 'neutral']}
    }
    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            result = response.json()
            return result['labels'][0]
        else:
            logger.error(f"Hugging Face API error: {response.text}")
            return None
    except Exception as e:
        logger.error(f"API request error: {e}")
        return None

# Main function to monitor Truth Social
def main():
    last_processed = get_last_processed()
    
    # Scrape Truth Social
    url = 'https://truthsocial.com/@realDonaldTrump'
    try:
        response = requests.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Adjust selectors based on actual Truth Social HTML structure
        posts = soup.find_all('div', class_='post')  # Placeholder class
        if not posts:
            logger.warning("No posts found. Check HTML selectors.")
            return
        
        for post in posts:
            # Assuming each post has an 'id' attribute and text in a 'post-text' div
            post_id = post.get('id')
            if not post_id:
                continue
            if post_id == last_processed:
                break
            
            post_text_elem = post.find('div', class_='post-text')  # Placeholder class
            if not post_text_elem:
                continue
            post_text = post_text_elem.text.strip()
            
            # Classify the post
            classification = classify_post(post_text)
            if classification in ['buy stocks', 'sell stocks']:
                message = f"{classification.upper()}: {post_text}"
                send_telegram_message(message)
                logger.info(f"Sent alert for post ID {post_id}: {classification}")
            
            # Update the last processed post ID
            update_last_processed(post_id)
    except requests.RequestException as e:
        logger.error(f"Scraping error: {e}")

if __name__ == '__main__':
    main()