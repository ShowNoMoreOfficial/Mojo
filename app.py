
import feedparser
import requests
import json
import os
from datetime import datetime, timedelta
import time
import groq
from flask import Flask, jsonify

# --- Configuration ---

# Initialize Flask App
app = Flask(__name__)

# It's best practice to get the API key from an environment variable
# On your deployment server, you will set this environment variable.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")

# Calibrated RSS Feeds for India-US News
RSS_FEEDS = [
    "https://www.thehindu.com/news/international/feeder/default.rss",
    "https://zeenews.india.com/rss/india-national-news.xml",
    "https://timesofindia.indiatimes.com/rssfeeds/7098551.cms", # US News from Times of India
    "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
    "https://www.reuters.com/news/archive/worldNews",
    "https://www.cfr.org/rss/region/south-asia", # Council on Foreign Relations - South Asia
]

# --- Core Logic Functions (from the original script) ---

def get_articles_from_feeds(feed_urls):
    """
    Fetches and parses articles from a list of RSS feed URLs.
    """
    all_articles = []
    lookback_period = datetime.now() - timedelta(hours=72)
    print("Fetching articles from feeds (looking back 72 hours)...")
    for url in feed_urls:
        try:
            feed = feedparser.parse(url, agent="AITrendFinder/1.0")
            print(f"-> Checking feed: {feed.feed.get('title', url)}")
            for entry in feed.entries:
                published_time = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    published_time = datetime.fromtimestamp(time.mktime(entry.published_parsed))
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    published_time = datetime.fromtimestamp(time.mktime(entry.updated_parsed))

                if not published_time or published_time < lookback_period:
                    continue
                article = {'title': entry.title, 'link': entry.link, 'summary': entry.get('summary', 'No summary available.')}
                all_articles.append(article)
        except Exception as e:
            print(f"Error fetching or parsing feed {url}: {e}")
    print(f"\nFound {len(all_articles)} new articles from the last 72 hours.")
    return all_articles

def call_groq_api(system_prompt, user_prompt, max_retries=3):
    """
    A centralized function to call the Groq API with retry logic.
    """
    if not GROQ_API_KEY:
        print("Groq API key not set.")
        return None
    try:
        client = groq.Groq(api_key=GROQ_API_KEY)
    except Exception as e:
        print(f"Error initializing Groq client: {e}")
        return None
    for attempt in range(max_retries):
        try:
            chat_completion = client.chat.completions.create(
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                model="llama3-8b-8192",
                response_format={"type": "json_object"},
                max_tokens=8192,
            )
            response_text = chat_completion.choices[0].message.content
            return json.loads(response_text)
        except Exception as e:
            print(f"API call attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                return None
    return None

def analyze_articles_in_batches(articles, batch_size=25):
    """
    Analyzes articles in smaller batches.
    """
    all_trends = []
    num_batches = (len(articles) + batch_size - 1) // batch_size
    for i in range(num_batches):
        print(f"\n--- Processing Batch {i+1} of {num_batches} with Groq ---")
        batch = articles[i * batch_size:(i + 1) * batch_size]
        content_for_analysis = "".join([f"Title: {a['title']}\nSummary: {a['summary']}\n\n" for a in batch])
        system_prompt = "You are an expert geopolitical analyst on India-US relations. Identify topics involving an interaction between India and the United States. Respond ONLY with a valid JSON object with a 'trends' key."
        user_prompt = f"From the articles below, identify topics involving BOTH India and the USA. For each, provide a name and relevant article titles.\n\nContent:\n{content_for_analysis}"
        response_json = call_groq_api(system_prompt, user_prompt)
        if response_json and 'trends' in response_json:
            all_trends.extend(response_json['trends'])
        if i < num_batches - 1:
            time.sleep(10)
    return all_trends

def consolidate_trends(trends_list):
    """
    Performs a final analysis to find the top overall trends.
    """
    if not trends_list:
        return None
    print("\n--- Consolidating all trends for final report with Groq ---")
    consolidated_text = "".join([f"Trend: {t.get('trend_name', 'N/A')}\nRelevant Articles: {', '.join(t.get('relevant_articles', []))}\n\n" for t in trends_list if isinstance(t, dict)])
    system_prompt = "You are an AI assistant synthesizing a report on India-US relations. Respond ONLY with a valid JSON object with a 'report' key."
    user_prompt = f"From the topics below, synthesize the top 7-10 trending topics about the India-USA relationship. Merge duplicates. For each, provide a 'trend_name', 'explanation', and 'relevant_articles'.\n\nTopics:\n{consolidated_text}"
    response_json = call_groq_api(system_prompt, user_prompt)
    if response_json and 'report' in response_json:
        return response_json['report']
    return None

# --- API Endpoints ---

@app.route('/', methods=['GET'])
def home():
    """A simple endpoint to check if the API is running."""
    return "<h1>Indo-American News API</h1><p>API is running. Use the /get-trends endpoint to fetch data.</p>"


@app.route('/get-trends', methods=['GET'])
def get_trends_api():
    """
    This is the main API endpoint. It runs the analysis and returns the trends.
    """
    print("API endpoint /get-trends hit. Starting analysis...")
    try:
        articles = get_articles_from_feeds(RSS_FEEDS)
        if not articles:
            return jsonify({"error": "No articles found in the last 72 hours."}), 404
        
        preliminary_trends = analyze_articles_in_batches(articles)
        if not preliminary_trends:
            return jsonify({"error": "Could not determine preliminary trends from articles."}), 500

        final_trends = consolidate_trends(preliminary_trends)
        if not final_trends:
            return jsonify({"error": "Could not consolidate trends into a final report."}), 500

        # Add the article links back into the final report
        article_dict = {article['title']: article['link'] for article in articles}
        for trend in final_trends:
            trend['articles_with_links'] = []
            for title in trend.get('relevant_articles', []):
                trend['articles_with_links'].append({
                    "title": title,
                    "link": article_dict.get(title, "#")
                })

        return jsonify(final_trends)

    except Exception as e:
        print(f"An unexpected error occurred in the API endpoint: {e}")
        return jsonify({"error": "An internal server error occurred."}), 500

# --- Main Execution Block ---
# --- CHANGE: Added host and port for better deployment compatibility ---
if __name__ == '__main__':
    # For production, a proper WSGI server like Gunicorn should be used.
    # The host='0.0.0.0' makes it accessible from outside the container.
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
