from flask import Flask, jsonify, request
from flask_cors import CORS
import feedparser
import requests
import json
import os
from datetime import datetime, timedelta
import time
import logging
from functools import wraps

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# --- Configuration ---
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', "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")

# RSS Feeds focused on India-US News
RSS_FEEDS = [
    "https://www.thehindu.com/news/international/feeder/default.rss",
    "https://zeenews.india.com/rss/india-national-news.xml",
    "https://timesofindia.indiatimes.com/rssfeeds/7098551.cms",
    "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
    "https://www.reuters.com/news/archive/worldNews",
    "https://www.cfr.org/rss/region/south-asia",
]

# Rate limiting decorator
def rate_limit(max_requests=10, per_minutes=60):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Simple in-memory rate limiting (use Redis in production)
            client_ip = request.remote_addr
            current_time = time.time()
            
            if not hasattr(decorated_function, 'requests'):
                decorated_function.requests = {}
            
            if client_ip not in decorated_function.requests:
                decorated_function.requests[client_ip] = []
            
            # Clean old requests
            decorated_function.requests[client_ip] = [
                req_time for req_time in decorated_function.requests[client_ip]
                if current_time - req_time < per_minutes * 60
            ]
            
            if len(decorated_function.requests[client_ip]) >= max_requests:
                return jsonify({
                    "error": "Rate limit exceeded",
                    "message": f"Maximum {max_requests} requests per {per_minutes} minutes"
                }), 429
            
            decorated_function.requests[client_ip].append(current_time)
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# --- Core Functions ---
def get_articles_from_feeds(feed_urls, hours_back=72):
    """
    Fetches and parses articles from a list of RSS feed URLs.
    """
    all_articles = []
    lookback_period = datetime.now() - timedelta(hours=hours_back)

    logger.info(f"Fetching articles from feeds (looking back {hours_back} hours)...")
    
    for url in feed_urls:
        try:
            feed = feedparser.parse(url, agent="AITrendFinder/1.0")
            feed_title = feed.feed.get('title', url)
            logger.info(f"Checking feed: {feed_title}")
            
            for entry in feed.entries:
                published_time = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    published_time = datetime.fromtimestamp(time.mktime(entry.published_parsed))
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    published_time = datetime.fromtimestamp(time.mktime(entry.updated_parsed))

                if not published_time or published_time < lookback_period:
                    continue

                article = {
                    'title': entry.title,
                    'link': entry.link,
                    'summary': entry.get('summary', entry.get('description', 'No summary available.')),
                    'published': published_time.isoformat() if published_time else None,
                    'source': feed_title
                }
                all_articles.append(article)
                
        except Exception as e:
            logger.error(f"Error fetching or parsing feed {url}: {e}")
    
    logger.info(f"Found {len(all_articles)} new articles from the last {hours_back} hours.")
    return all_articles

def call_groq_api(system_prompt, user_prompt, max_retries=3):
    """
    A centralized function to call the Groq API with retry logic.
    """
    if not GROQ_API_KEY or GROQ_API_KEY == "PASTE_YOUR_GROQ_API_KEY_HERE":
        logger.error("Groq API key not set.")
        return None

    try:
        # Import groq here to handle version issues
        import groq
        client = groq.Groq(api_key=GROQ_API_KEY)
    except Exception as e:
        logger.error(f"Error initializing Groq client: {e}")
        return None

    for attempt in range(max_retries):
        try:
            chat_completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                model="llama3-8b-8192",
                response_format={"type": "json_object"},
                max_tokens=4096,
                temperature=0.3
            )
            
            response_text = chat_completion.choices[0].message.content
            return json.loads(response_text)

        except Exception as e:
            if "rate" in str(e).lower():
                wait_time = 30 * (attempt + 1)
                logger.warning(f"Rate limit hit. Retrying in {wait_time} seconds... (Attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            elif "api" in str(e).lower():
                wait_time = 15 * (attempt + 1)
                logger.error(f"Groq API Error: {e}. Retrying in {wait_time} seconds... (Attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                logger.error(f"Unexpected error: {e}")
                return None

    logger.error("All retries failed.")
    return None

def analyze_articles_in_batches(articles, batch_size=15):
    """
    Analyzes articles in smaller batches to avoid hitting API rate limits.
    """
    all_trends = []
    num_batches = (len(articles) + batch_size - 1) // batch_size

    for i in range(num_batches):
        logger.info(f"Processing Batch {i+1} of {num_batches} with Groq")
        batch = articles[i * batch_size:(i + 1) * batch_size]

        content_for_analysis = ""
        for article in batch:
            content_for_analysis += f"Title: {article['title']}\nSummary: {article['summary'][:200]}...\n\n"

        system_prompt = "You are an expert geopolitical analyst focused on India-US relations. Identify news topics involving BOTH India and USA. Respond ONLY with valid JSON containing 'trends' array."
        user_prompt = f"""
        From these articles, identify topics involving BOTH India and USA. Ignore single-country topics.
        
        Content:
        ---
        {content_for_analysis}
        ---
        
        JSON format:
        {{
            "trends": [
                {{ "trend_name": "US-India Trade Deal", "relevant_articles": ["Article Title 1", "Article Title 2"] }}
            ]
        }}
        """
        
        response_json = call_groq_api(system_prompt, user_prompt)
        if response_json and 'trends' in response_json and isinstance(response_json['trends'], list):
            all_trends.extend(response_json['trends'])
        
        # Wait between batches to avoid rate limits
        if i < num_batches - 1:
            time.sleep(15)

    return all_trends

def consolidate_trends(trends_list):
    """
    Takes a list of trends from all batches and performs a final analysis.
    """
    if not trends_list:
        return None

    logger.info("Consolidating all trends for final report with Groq")
    
    consolidated_text = ""
    for trend in trends_list:
        if isinstance(trend, dict):
             consolidated_text += f"Trend: {trend.get('trend_name', 'N/A')}\n"
             articles = trend.get('relevant_articles', [])
             if isinstance(articles, list):
                  consolidated_text += f"Articles: {', '.join(articles[:3])}\n\n"  # Limit to 3 articles per trend

    system_prompt = "You synthesize India-US relations trends. Respond ONLY with valid JSON containing 'report' array."
    user_prompt = f"""
    Synthesize top 5-7 India-US trends from this data. Merge similar topics.
    
    Data:
    ---
    {consolidated_text[:2000]}  
    ---
    
    JSON format:
    {{
        "report": [
            {{ "trend_name": "Modi-Biden Summit", "explanation": "Recent diplomatic meeting between leaders.", "relevant_articles": ["Title 1", "Title 2"] }}
        ]
    }}
    """
    
    response_json = call_groq_api(system_prompt, user_prompt)
    if response_json and 'report' in response_json:
        return response_json['report']
    return None

# --- API Routes ---

@app.route('/', methods=['GET'])
def home():
    """Health check endpoint"""
    return jsonify({
        "message": "India-US News Trends API",
        "version": "1.0",
        "status": "active",
        "endpoints": {
            "/trends": "GET - Get trending India-US news topics",
            "/articles": "GET - Get recent articles from feeds",
            "/health": "GET - Health check"
        }
    })

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "groq_api_configured": bool(GROQ_API_KEY and GROQ_API_KEY != "PASTE_YOUR_GROQ_API_KEY_HERE")
    })

@app.route('/articles', methods=['GET'])
@rate_limit(max_requests=5, per_minutes=10)
def get_articles():
    """Get recent articles from RSS feeds"""
    try:
        hours_back = request.args.get('hours', 72, type=int)
        hours_back = min(max(hours_back, 1), 168)  # Limit between 1 and 168 hours (1 week)
        
        articles = get_articles_from_feeds(RSS_FEEDS, hours_back=hours_back)
        
        return jsonify({
            "success": True,
            "count": len(articles),
            "hours_back": hours_back,
            "articles": articles,
            "timestamp": datetime.now().isoformat()
        })
    
    except Exception as e:
        logger.error(f"Error in get_articles: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/trends', methods=['GET'])
@rate_limit(max_requests=2, per_minutes=60)
def get_trends():
    """Get trending India-US news topics"""
    try:
        hours_back = request.args.get('hours', 72, type=int)
        hours_back = min(max(hours_back, 1), 168)  # Limit between 1 and 168 hours
        
        batch_size = request.args.get('batch_size', 15, type=int)
        batch_size = min(max(batch_size, 10), 20)  # Smaller batch size
        
        # Get articles
        articles = get_articles_from_feeds(RSS_FEEDS, hours_back=hours_back)
        
        if not articles:
            return jsonify({
                "success": True,
                "message": "No recent articles found",
                "trends": [],
                "articles_analyzed": 0,
                "timestamp": datetime.now().isoformat()
            })
        
        # Limit articles to avoid timeout
        if len(articles) > 50:
            articles = articles[:50]
            logger.info(f"Limited to first 50 articles to avoid timeout")
        
        # Analyze trends
        preliminary_trends = analyze_articles_in_batches(articles, batch_size=batch_size)
        final_trends = consolidate_trends(preliminary_trends)
        
        # Create article lookup dictionary
        article_dict = {article['title']: article for article in articles}
        
        # Enhance trends with full article information
        enhanced_trends = []
        if final_trends:
            for trend in final_trends:
                if isinstance(trend, dict):
                    enhanced_trend = {
                        "trend_name": trend.get('trend_name', 'N/A'),
                        "explanation": trend.get('explanation', 'No explanation provided.'),
                        "relevant_articles": []
                    }
                    
                    relevant_articles = trend.get('relevant_articles', [])
                    if isinstance(relevant_articles, list):
                        for title in relevant_articles:
                            if title in article_dict:
                                enhanced_trend["relevant_articles"].append(article_dict[title])
                            else:
                                # If exact match not found, add as title only
                                enhanced_trend["relevant_articles"].append({
                                    "title": title,
                                    "link": "# (Link not found)",
                                    "summary": "Article details not available",
                                    "published": None,
                                    "source": "Unknown"
                                })
                    
                    enhanced_trends.append(enhanced_trend)
        
        return jsonify({
            "success": True,
            "trends_count": len(enhanced_trends),
            "articles_analyzed": len(articles),
            "hours_back": hours_back,
            "trends": enhanced_trends,
            "timestamp": datetime.now().isoformat()
        })
    
    except Exception as e:
        logger.error(f"Error in get_trends: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500

@app.errorhandler(404)
def not_found(error):
    return jsonify({
        "success": False,
        "error": "Endpoint not found",
        "available_endpoints": ["/", "/health", "/articles", "/trends"]
    }), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({
        "success": False,
        "error": "Internal server error",
        "message": "Please try again later"
    }), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_ENV') == 'development'
    
    logger.info(f"Starting Flask app on port {port}")
    logger.info(f"Debug mode: {debug_mode}")
    logger.info(f"Groq API configured: {bool(GROQ_API_KEY and GROQ_API_KEY != 'PASTE_YOUR_GROQ_API_KEY_HERE')}")
    
    app.run(host='0.0.0.0', port=port, debug=debug_mode)


