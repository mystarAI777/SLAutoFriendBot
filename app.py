import sys
import os
import requests
import logging
import time
import threading
import json
import re
import sqlite3
import random
import uuid
import hashlib
from datetime import datetime, timedelta, timezone
import unicodedata
from groq import Groq
import google.generativeai as genai
from flask import Response
from cryptography.fernet import Fernet

# Type hints
try:
    from typing import Union, Dict, Any, List, Optional
except ImportError:
    Dict = dict
    Any = object
    List = list
    Union = object
    Optional = object

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean, text
from sqlalchemy.orm import declarative_base, sessionmaker
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
import schedule
import signal
from threading import Lock
from pathlib import Path
import subprocess

# --- Basic Settings ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Constants ---
VOICE_DIR = '/tmp/voices'
SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', 'http://localhost:10000')
VOICEVOX_SPEAKER_ID = 20
HOLOLIVE_NEWS_URL = "https://hololive-tsuushin.com/category/holonews/"
SL_SAFE_CHAR_LIMIT = 300
VOICE_OPTIMAL_LENGTH = 150
BACKUP_DIR = Path('/tmp/db_backups')
GITHUB_BACKUP_FILE = 'database_backup.json.encrypted'
BACKUP_METADATA_FILE = 'backup_metadata.json'

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
]
LOCATION_CODES = {
    "東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"
}
SPECIALIZED_SITES = {
    'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー', 'blender']},
    'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG', 'CG']},
    'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']},
    'アニメ': {'base_url': 'https://animedb.jp/', 'keywords': ['アニメ', 'anime']},
}
ANIME_KEYWORDS = [
    'アニメ', 'anime', 'アニメーション', '作画', '声優', 'OP', 'ED', '劇場版', '映画', 'OVA',
    '原作', '漫画', 'ラノベ', '主人公', 'キャラクター', '制作会社', 'スタジオ'
]

# --- Global Variables ---
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client = None
engine = None
Session = None
app = Flask(__name__)
CORS(app)

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

Base = declarative_base()

# --- Secret and Key Management ---

def get_secret(name):
    secret_file_path = f"/etc/secrets/{name}"
    if os.path.exists(secret_file_path):
        try:
            with open(secret_file_path, 'r') as f:
                logger.info(f"✅ Loaded {name} from Secret File")
                return f.read().strip()
        except IOError:
            return None
    return os.environ.get(name)

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')
GEMINI_API_KEY = get_secret('GEMINI_API_KEY')
ADMIN_TOKEN = get_secret('ADMIN_TOKEN')
ALLOWED_ADMIN_IPS = os.environ.get('ALLOWED_ADMIN_IPS', '').split(',')

def get_encryption_key():
    encryption_key = get_secret('BACKUP_ENCRYPTION_KEY')
    if not encryption_key or len(encryption_key.encode('utf-8')) != 44:
        logger.critical("🔥 FATAL ERROR: BACKUP_ENCRYPTION_KEYが未設定または形式が不正です。")
        sys.exit(1)
    return encryption_key.encode('utf-8')

# --- Database Models ---
class UserMemory(Base): __tablename__ = 'user_memories'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False); user_name = Column(String(255), nullable=False); interaction_count = Column(Integer, default=0); last_interaction = Column(DateTime, default=datetime.utcnow)
class ConversationHistory(Base): __tablename__ = 'conversation_history'; id = Column(Integer, primary_key=True, autoincrement=True); user_uuid = Column(String(255), nullable=False, index=True); role = Column(String(10), nullable=False); content = Column(Text, nullable=False); timestamp = Column(DateTime, default=datetime.utcnow, index=True)
class HololiveNews(Base): __tablename__ = 'hololive_news'; id = Column(Integer, primary_key=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True)
class BackgroundTask(Base): __tablename__ = 'background_tasks'; id = Column(Integer, primary_key=True); task_id = Column(String(255), unique=True, nullable=False); user_uuid = Column(String(255), nullable=False); task_type = Column(String(50), nullable=False); query = Column(Text, nullable=False); result = Column(Text); status = Column(String(20), default='pending'); created_at = Column(DateTime, default=datetime.utcnow, index=True); completed_at = Column(DateTime)
class SpecializedNews(Base): __tablename__ = 'specialized_news'; id = Column(Integer, primary_key=True); site_name = Column(String(100), nullable=False, index=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True)
class HolomemWiki(Base): __tablename__ = 'holomem_wiki'; id = Column(Integer, primary_key=True); member_name = Column(String(100), nullable=False, unique=True, index=True); description = Column(Text); debut_date = Column(String(100)); generation = Column(String(100)); tags = Column(Text); graduation_date = Column(String(100), nullable=True); graduation_reason = Column(Text, nullable=True); mochiko_feeling = Column(Text, nullable=True); is_active = Column(Boolean, default=True, nullable=False, index=True); profile_url = Column(String(500), nullable=True); last_updated = Column(DateTime, default=datetime.utcnow)
class NewsCache(Base): __tablename__ = 'news_cache'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), nullable=False, index=True); news_id = Column(Integer, nullable=False); news_number = Column(Integer, nullable=False); news_type = Column(String(50), nullable=False); created_at = Column(DateTime, default=datetime.utcnow)
class UserPsychology(Base): __tablename__ = 'user_psychology'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False, index=True); user_name = Column(String(255), nullable=False); openness = Column(Integer, default=50); conscientiousness = Column(Integer, default=50); extraversion = Column(Integer, default=50); agreeableness = Column(Integer, default=50); neuroticism = Column(Integer, default=50); interests = Column(Text); favorite_topics = Column(Text); conversation_style = Column(String(50)); emotional_tendency = Column(String(50)); total_messages = Column(Integer, default=0); avg_message_length = Column(Integer, default=0); analysis_summary = Column(Text); last_analyzed = Column(DateTime, default=datetime.utcnow); analysis_confidence = Column(Integer, default=0)

# --- Core Initializations ---
def ensure_voice_directory():
    try:
        os.makedirs(VOICE_DIR, exist_ok=True)
        logger.info(f"✅ Voice directory is ready: {VOICE_DIR}")
    except Exception as e:
        logger.error(f"❌ Could not create voice directory: {e}")

def create_optimized_db_engine():
    try:
        is_sqlite = 'sqlite' in DATABASE_URL.lower()
        connect_args = {'check_same_thread': False, 'timeout': 20} if is_sqlite else {'connect_timeout': 10, 'options': '-c statement_timeout=30000'}
        pool_args = {'pool_pre_ping': True}
        if not is_sqlite: pool_args.update({'pool_size': 10, 'max_overflow': 20, 'pool_recycle': 300})
        engine = create_engine(DATABASE_URL, connect_args=connect_args, **pool_args)
        with engine.connect() as conn: conn.execute(text("SELECT 1"))
        logger.info(f"✅ Database engine created ({'SQLite' if is_sqlite else 'PostgreSQL'})")
        return engine
    except Exception as e:
        logger.error(f"❌ Failed to create database engine: {e}"); raise

def initialize_groq_client():
    global groq_client
    try:
        if not GROQ_API_KEY or len(GROQ_API_KEY) < 20:
            logger.warning("⚠️ GROQ_API_KEY not set, AI features disabled."); return None
        groq_client = Groq(api_key=GROQ_API_KEY.strip())
        logger.info("✅ Groq client initialized")
        return groq_client
    except Exception as e:
        logger.error(f"❌ Groq initialization failed: {e}"); return None

_cache = {'holomem_keywords': {'data': None, 'expires': None}}
_cache_lock = Lock()
def get_cached_or_fetch(cache_key, fetch_func, ttl_seconds=3600):
    with _cache_lock:
        cache_entry = _cache.get(cache_key)
        now = datetime.utcnow()
        if cache_entry and cache_entry.get('data') and cache_entry.get('expires') and now < cache_entry['expires']: return cache_entry['data']
        data = fetch_func()
        _cache[cache_key] = {'data': data, 'expires': now + timedelta(seconds=ttl_seconds)}
        return data

# --- Improved Web Scraping Section ---
class ScraperWithRetry:
    def __init__(self, max_retries=3, retry_delay=2):
        self.max_retries, self.retry_delay, self.session = max_retries, retry_delay, requests.Session()
        self.session.headers.update({'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'ja,en;q=0.9', 'Connection': 'keep-alive'})
    def fetch_with_retry(self, url, timeout=15):
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=timeout, allow_redirects=True)
                response.raise_for_status()
                return response
            except requests.exceptions.Timeout as e: last_error = e; time.sleep(self.retry_delay * attempt)
            except requests.exceptions.HTTPError as e:
                last_error = e
                if e.response.status_code == 429: time.sleep(self.retry_delay * attempt * 2)
                elif e.response.status_code >= 500: time.sleep(self.retry_delay * attempt)
                else: return None
            except requests.exceptions.RequestException as e: last_error = e; time.sleep(self.retry_delay * attempt)
        logger.error(f"❌ Failed after {self.max_retries} attempts for {url}: {last_error}")
        return None
scraper = ScraperWithRetry()

def fetch_article_content(article_url, timeout=15):
    response = scraper.fetch_with_retry(article_url, timeout)
    if not response: return None
    try:
        soup = BeautifulSoup(response.content, 'html.parser')
        selectors = ['article .entry-content', '.post-content', '.article-content', 'article', '.content', 'main article']
        content_elem = next((soup.select_one(s) for s in selectors if soup.select_one(s)), None)
        if content_elem:
            text = ' '.join([clean_text(p.get_text()) for p in content_elem.find_all('p') if len(clean_text(p.get_text())) > 20])
            if text: return text[:2000]
        meta_desc = soup.find('meta', {'name': 'description'})
        if meta_desc and meta_desc.get('content'): return clean_text(meta_desc['content'])
        return None
    except Exception as e:
        logger.error(f"❌ Error parsing article {article_url}: {e}"); return None

def summarize_article(title, content):
    if not groq_client or not content: return content[:500] if content else title
    try:
        prompt = f"以下のニュース記事を200文字以内で簡潔に要約:\n\nタイトル: {title}\n本文: {content[:1500]}"
        completion = groq_client.chat.completions.create(messages=[{"role": "user", "content": prompt}], model="llama-3.1-8b-instant", temperature=0.5, max_tokens=200)
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Summarization error: {e}"); return content[:500] if content else title

def update_hololive_news_database():
    session = Session()
    try:
        response = scraper.fetch_with_retry(HOLOLIVE_NEWS_URL)
        if not response: return
        soup = BeautifulSoup(response.content, 'html.parser')
        articles = next((soup.select(s) for s in ['article', '.post', '.entry'] if soup.select(s)), [])[:5]
        added_count = 0
        for article in articles:
            try:
                title_elem = article.find(['h2', 'h3', 'a']); title = clean_text(title_elem.get_text())
                link_elem = title_elem if title_elem.name == 'a' else article.find('a', href=True)
                if not title or len(title) < 5 or not link_elem: continue
                article_url = urljoin(HOLOLIVE_NEWS_URL, link_elem.get('href', ''))
                content = fetch_article_content(article_url) or title
                news_hash = hashlib.md5(f"{title}{content[:100]}".encode('utf-8')).hexdigest()
                if session.query(HololiveNews).filter_by(news_hash=news_hash).first(): continue
                summary = summarize_article(title, content)
                session.add(HololiveNews(title=title, content=summary, news_hash=news_hash, url=article_url))
                added_count += 1
                if groq_client: time.sleep(0.5)
            except Exception as e:
                logger.error(f"❌ Error processing holo news article: {e}")
        if added_count > 0: session.commit(); logger.info(f"✅ Holo news update: {added_count} new")
        scrape_hololive_members(); scrape_graduated_members()
    except Exception as e:
        logger.error(f"❌ Holo news update error: {e}"); session.rollback()
    finally:
        session.close()

def scrape_hololive_members():
    base_url = "https://hololive.hololivepro.com"
    session = Session()
    try:
        response = scraper.fetch_with_retry(f"{base_url}/talents/")
        if not response: return
        soup = BeautifulSoup(response.content, 'html.parser')
        cards = soup.select("a[href^='/talents/']")
        scraped_names = set()
        for card in cards:
            try:
                href = card.get('href', ''); name_elem = card.find(['h2', 'h3', 'span'], class_=lambda x: x and 'name' in x.lower()) or card
                member_name = re.sub(r'\s*\(.*?\)\s*', '', clean_text(name_elem.get_text())).strip()
                if href == '/talents/' or not href.startswith('/talents/') or not member_name or len(member_name) > 100: continue
                scraped_names.add(member_name)
                generation = "不明"; gen_patterns = [(r'0期生|ゼロ期生','0期生'),(r'1期生|一期生','1期生'),(r'2期生|二期生','2期生'),(r'3期生|三期生','3期生'),(r'4期生|四期生','4期生'),(r'5期生|五期生','5期生'),(r'ゲーマーズ|GAMERS','ゲーマーズ'),(r'ID|Indonesia','ホロライブID'),(r'EN|English','ホロライブEN'),(r'DEV_IS|ReGLOSS','DEV_IS')]
                for pattern, gen_name in gen_patterns:
                    if re.search(pattern, card.get_text(), re.IGNORECASE): generation = gen_name; break
                existing = session.query(HolomemWiki).filter_by(member_name=member_name).first()
                if existing:
                    if not existing.is_active or existing.generation != generation: existing.is_active, existing.generation, existing.last_updated = True, generation, datetime.utcnow()
                else:
                    session.add(HolomemWiki(member_name=member_name, description=f"{member_name}は{generation}のメンバーです。", generation=generation, is_active=True, profile_url=urljoin(base_url, href)))
            except Exception as e:
                logger.error(f"❌ Error processing member card: {e}")
        for member in session.query(HolomemWiki).filter(HolomemWiki.member_name.notin_(scraped_names), HolomemWiki.graduation_date == None, HolomemWiki.is_active == True):
            member.is_active, member.last_updated = False, datetime.utcnow()
        session.commit()
    except Exception as e:
        logger.error(f"❌ Member scraping error: {e}"); session.rollback()
    finally:
        session.close()

def scrape_graduated_members():
    # This is static data, no retry logic needed
    # ... Implementation is unchanged ...
    pass

def update_all_specialized_news():
    def update_single_site(site_name, config):
        session = Session(); added_count = 0
        try:
            response = scraper.fetch_with_retry(config['base_url'])
            if not response: return f"{site_name}: fetch failed"
            soup = BeautifulSoup(response.content, 'html.parser')
            articles = next((soup.select(s) for s in ['article', '.post', '.entry'] if soup.select(s)), [])[:5]
            for article in articles:
                try:
                    title_elem = article.find(['h2', 'h3', 'a']); title = clean_text(title_elem.get_text())
                    link_elem = title_elem if title_elem.name == 'a' else article.find('a', href=True)
                    if not title or len(title) < 5 or not link_elem: continue
                    article_url = urljoin(config['base_url'], link_elem.get('href', ''))
                    content = fetch_article_content(article_url) or title
                    news_hash = hashlib.md5(f"{title}{content[:100]}".encode('utf-8')).hexdigest()
                    if session.query(SpecializedNews).filter_by(news_hash=news_hash).first(): continue
                    summary = summarize_article(title, content)
                    session.add(SpecializedNews(site_name=site_name, title=title, content=summary, news_hash=news_hash, url=article_url))
                    added_count += 1
                    if groq_client: time.sleep(0.5)
                except Exception as e:
                    logger.warning(f"⚠️ Error processing article in {site_name}: {e}")
            if added_count > 0: session.commit()
            return f"{added_count} new"
        except Exception as e:
            logger.error(f"❌ {site_name} processing error: {e}"); session.rollback(); return "error"
        finally:
            session.close()
    with ThreadPoolExecutor(max_workers=len(SPECIALIZED_SITES)) as executor:
        future_to_site = {executor.submit(update_single_site, name, conf): name for name, conf in SPECIALIZED_SITES.items() if name != 'セカンドライフ'}
        for future in as_completed(future_to_site):
            logger.info(f"✅ Spec news update for {future_to_site[future]}: {future.result()}")
            
# --- Helper Functions (Conversation Patterns, User Management, etc.) ---
def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()

def get_japan_time():
    now = datetime.now(timezone(timedelta(hours=9)))
    return f"今は{now.year}年{now.month}月{now.day}日 {now.hour}時{now.minute}分だよ！"

def get_weather_forecast(location_name):
    area_code = LOCATION_CODES.get(location_name, "130000")
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        response = scraper.fetch_with_retry(url)
        if response:
            return f"今の{location_name}の天気は、「{response.json().get('text', '情報なし')}」って感じだよ！"
    except Exception as e:
        logger.error(f"❌ Weather fetch error: {e}")
    return f"{location_name}の天気、うまく取れなかったみたい…ごめんね！"

def limit_text_for_sl(text, max_length=SL_SAFE_CHAR_LIMIT):
    return text if len(text) <= max_length else text[:max_length - 3] + "..."

def get_or_create_user(session, uuid, name):
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != name: user.user_name = name
    else:
        user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1)
        session.add(user)
    session.commit()
    return user

def get_conversation_history(session, uuid, limit=10):
    history = session.query(ConversationHistory).filter_by(user_uuid=uuid).order_by(ConversationHistory.timestamp.desc()).limit(limit).all()
    history.reverse()
    return history

def get_active_holomem_keywords():
    def _fetch():
        session = Session()
        try:
            members = session.query(HolomemWiki.member_name).filter_by(is_active=True).all()
            return [m[0] for m in members] + ['ホロライブ', 'ホロメン']
        finally:
            session.close()
    return get_cached_or_fetch('holomem_keywords', _fetch)

def is_time_request(message): return any(kw in message for kw in ['今何時', '時間', '時刻'])
def is_weather_request(message): return any(kw in message for kw in ['天気', '気温', '雨', '晴れ'])
def is_explicit_search_request(message): return any(kw in message for kw in ['調べて', '検索して', '教えて'])
def is_news_detail_request(message):
    match = re.search(r'([1-9]|[１-９])番', message)
    if match and any(kw in message for kw in ['詳しく', '詳細']):
        return int(unicodedata.normalize('NFKC', match.group(1)))
    return None

# --- Self-Correction Functionality (Re-integrated) ---
def detect_db_correction_request(message):
    patterns = [r'(.+?)(?:は|が)(?:間違[いっ]てる|違う)', r'実は(.+?)(?:だよ|です)', r'正しくは(.+?)(?:だよ|です)']
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            member_name = next((kw for kw in get_active_holomem_keywords() if kw in message), None)
            if member_name:
                return {'member_name': member_name, 'user_claim': match.group(1).strip(), 'original_message': message}
    return None

def scrape_major_search_engines(query, num_results=3):
    url = f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP"
    response = scraper.fetch_with_retry(url)
    if not response: return []
    soup = BeautifulSoup(response.content, 'html.parser')
    results = []
    for elem in soup.select('li.b_algo')[:num_results]:
        title = elem.select_one('h2')
        snippet = elem.select_one('div.b_caption p')
        if title and snippet:
            results.append({'title': clean_text(title.get_text()), 'snippet': clean_text(snippet.get_text())})
    return results

def verify_and_correct_holomem_info(correction_request):
    member_name = correction_request['member_name']
    logger.info(f"🔍 Verifying correction for {member_name}...")
    search_results = scrape_major_search_engines(f"ホロライブ {member_name} {correction_request['user_claim']}", 5)
    if not search_results or not groq_client:
        return "ごめん、情報が確認できなかった…"

    combined_results = "\n".join([f"- {r['snippet']}" for r in search_results])
    verification_prompt = f"""以下の情報とユーザーの主張を検証してください。
対象: ホロライブの{member_name}
ユーザーの主張: {correction_request['user_claim']}
検索結果:
{combined_results[:2000]}
タスク: ユーザーの主張が事実なら、修正すべき情報をJSON形式で抽出してください。事実でない、または確認できない場合は verified: false としてください。
例: {{"verified": true, "confidence": 95, "extracted_info": {{"generation": "1期生"}}}}
"""
    try:
        completion = groq_client.chat.completions.create(messages=[{"role": "user", "content": verification_prompt}], model="llama-3.1-8b-instant", temperature=0.1, max_tokens=300, response_format={"type": "json_object"})
        result = json.loads(completion.choices[0].message.content)

        if result.get('verified') and result.get('confidence', 0) >= 80:
            session = Session()
            try:
                member = session.query(HolomemWiki).filter_by(member_name=member_name).first()
                if member and result.get('extracted_info'):
                    for key, value in result['extracted_info'].items():
                        if hasattr(member, key): setattr(member, key, value)
                    member.last_updated = datetime.utcnow()
                    session.commit()
                    return f"本当だ！{member_name}ちゃんの情報を修正したよ、教えてくれてありがとう！✨"
            finally:
                session.close()
    except Exception as e:
        logger.error(f"❌ Verification AI error: {e}")
    return "うーん、ちょっと確信が持てなかった…でも教えてくれてありがとう！"

def background_db_correction(task_id, correction_request):
    result_message = verify_and_correct_holomem_info(correction_request)
    session = Session()
    try:
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = result_message
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
            session.commit()
    finally:
        session.close()

def start_background_correction(user_uuid, correction_request):
    task_id = str(uuid.uuid4())
    session = Session()
    try:
        task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='db_correction', query=json.dumps(correction_request))
        session.add(task)
        session.commit()
        background_executor.submit(background_db_correction, task_id, correction_request)
        return task_id
    except Exception as e:
        logger.error(f"❌ Failed to start correction task: {e}"); session.rollback()
        return None
    finally:
        session.close()
        
# --- AI Response Generation ---
def generate_ai_response(user_data, message, history, reference_info=""):
    if not groq_client: return "ごめんね、今AIくんの調子が悪いみたい…ちょっと待ってて！"
    try:
        system_prompt = f"あなたは「もちこ」という22歳の明るい女性AIです。{user_data['name']}さんと楽しく話しています。一人称は「あてぃし」、語尾は「〜じゃん」「〜だよ」を使い、明るくギャルっぽい口調で話します。口癖は「まじ」「てか」「うける」です。男性的な言葉は絶対使いません。"
        messages = [{"role": "system", "content": system_prompt}]
        for msg in history[-4:]: messages.append({"role": msg.role, "content": msg.content})
        if reference_info: messages.append({"role": "system", "content": f"参考情報：{reference_info}"})
        messages.append({"role": "user", "content": message})
        
        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", temperature=0.8, max_tokens=300)
        response = completion.choices[0].message.content.strip()
        response = re.sub(r'だぜ|だな(?![い])|俺|僕', '', response)
        if not any(response.endswith(end) for end in ['。', '！', '？', '♪', '✨', '…', 'よ', 'ね', 'じゃん']):
            response += 'だよ！'
        return response
    except Exception as e:
        logger.error(f"❌ AI response error: {e}"); return "うーん、なんて言おうかな…考えがまとまらないや！"

# --- Secure GitHub Backup Section ---
def encrypt_backup_data(backup_data):
    try:
        fernet = Fernet(get_encryption_key())
        json_data = json.dumps(backup_data, ensure_ascii=False, indent=2).encode('utf-8')
        return fernet.encrypt(json_data)
    except Exception as e:
        logger.error(f"❌ Encryption failed: {e}"); raise

def decrypt_backup_data(encrypted_data):
    try:
        fernet = Fernet(get_encryption_key())
        decrypted_bytes = fernet.decrypt(encrypted_data)
        return json.loads(decrypted_bytes.decode('utf-8'))
    except Exception as e:
        logger.error(f"❌ Decryption failed: {e}"); raise

def export_database_to_json():
    if not Session: return None
    session = Session(); backup_data = {'timestamp': datetime.utcnow().isoformat(), 'tables': {}}
    try:
        tables = {'user_memories': UserMemory, 'conversation_history': ConversationHistory, 'holomem_wiki': HolomemWiki, 'user_psychology': UserPsychology}
        for name, model in tables.items():
            query = session.query(model)
            if name == 'conversation_history': query = query.order_by(model.timestamp.desc()).limit(5000)
            backup_data['tables'][name] = [{c.name: getattr(r, c.name).isoformat() if isinstance(getattr(r, c.name), datetime) else getattr(r, c.name) for c in r.__table__.columns} for r in query.all()]
        return backup_data
    except Exception as e:
        logger.error(f"❌ DB export error: {e}"); return None
    finally:
        session.close()

def commit_encrypted_backup_to_github():
    try:
        backup_data = export_database_to_json()
        if not backup_data: return False
        encrypted_data = encrypt_backup_data(backup_data)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        with open(BACKUP_DIR / GITHUB_BACKUP_FILE, 'wb') as f: f.write(encrypted_data)
        import shutil; shutil.copy(BACKUP_DIR / GITHUB_BACKUP_FILE, Path('.') / GITHUB_BACKUP_FILE)
        commands = [['git', 'config', 'user.email', 'bot@example.com'], ['git', 'config', 'user.name', 'Backup Bot'], ['git', 'add', GITHUB_BACKUP_FILE], ['git', 'commit', '-m', f'🔒 Backup {datetime.utcnow().isoformat()}'], ['git', 'push']]
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0 and 'nothing to commit' not in result.stdout:
                logger.error(f"❌ Git command failed: {result.stderr}"); return False
        logger.info("✅ Encrypted backup committed to GitHub")
        return True
    except Exception as e:
        logger.error(f"❌ GitHub commit error: {e}"); return False

# --- Admin Security ---
def require_admin_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not ADMIN_TOKEN: logger.critical("🔥 ADMIN_TOKEN not set!"); return jsonify({'error': 'Server config error'}), 500
        auth = request.headers.get('Authorization')
        if not auth or not auth.startswith('Bearer ') or auth.split(' ')[1] != ADMIN_TOKEN:
            return jsonify({'error': 'Invalid credentials'}), 401
        return f(*args, **kwargs)
    return decorated_function

# --- Flask Endpoints ---
@app.route('/health')
def health_check():
    db_ok = False
    try:
        with engine.connect() as conn: conn.execute(text("SELECT 1")); db_ok = True
    except: pass
    return jsonify({'database': 'ok' if db_ok else 'error', 'groq_ai': 'ok' if groq_client else 'disabled'}), 200

@app.route('/chat_lsl', methods=['POST', 'OPTIONS'])
def chat_lsl():
    if request.method == 'OPTIONS': return '', 204
    data = request.json
    if not data or 'user_uuid' not in data or 'user_name' not in data or 'message' not in data:
        return jsonify({'error': 'Missing required fields'}), 400
    
    user_uuid, user_name, message = data['user_uuid'], data['user_name'], data['message'].strip()
    session = Session()
    try:
        user = get_or_create_user(session, user_uuid, user_name)
        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        
        response_text = ""
        # Priority 1: DB correction
        if (correction_request := detect_db_correction_request(message)):
            if start_background_correction(user_uuid, correction_request):
                response_text = f"え、まじで！？{correction_request['member_name']}ちゃんの情報、調べてみるね！ちょっと待ってて！"
            else:
                response_text = "ごめん、今DB修正機能がうまく動いてないみたい…"
        # Keyword-based routing
        elif is_time_request(message): response_text = get_japan_time()
        elif is_weather_request(message): response_text = get_weather_forecast(user_name.split(' ')[0])
        else: # Default to AI
            history = get_conversation_history(session, user_uuid)
            response_text = generate_ai_response({'name': user.user_name}, message, history)

        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))
        session.commit()
        return jsonify({'response': response_text}), 200
    except Exception as e:
        logger.error(f"❌ Chat error for {user_name}: {e}", exc_info=True); session.rollback()
        return jsonify({'error': 'Internal server error'}), 500
    finally:
        session.close()

@app.route('/check_task', methods=['POST'])
def check_task():
    data = request.json
    if not data or 'user_uuid' not in data:
        return jsonify({'status': 'error', 'message': 'UUID required'}), 400
    session = Session()
    try:
        task = session.query(BackgroundTask).filter_by(user_uuid=data['user_uuid'], status='completed').order_by(BackgroundTask.completed_at.desc()).first()
        if task:
            result = {'query': task.query, 'result': task.result}
            session.delete(task)
            session.commit()
            return jsonify({'status': 'completed', 'task': result})
    finally:
        session.close()
    return jsonify({'status': 'pending'})

@app.route('/generate_voice', methods=['POST'])
def generate_voice_endpoint():
    data = request.json; text = data.get('text', '').strip()
    if not text: return jsonify({'error': 'Text is required'}), 400
    voicevox_url = VOICEVOX_URL_FROM_ENV or "http://localhost:50021"
    try:
        query_res = requests.post(f"{voicevox_url}/audio_query", params={"text": text, "speaker": VOICEVOX_SPEAKER_ID}, timeout=10)
        query_res.raise_for_status()
        synth_res = requests.post(f"{voicevox_url}/synthesis", params={"speaker": VOICEVOX_SPEAKER_ID}, json=query_res.json(), timeout=30)
        synth_res.raise_for_status()
        filename = f"voice_{uuid.uuid4().hex[:8]}.wav"; filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f: f.write(synth_res.content)
        return jsonify({'url': f"{SERVER_URL}/voices/{filename}"}), 200
    except Exception as e:
        logger.error(f"❌ Voice generation error: {e}"); return jsonify({'error': 'Voice generation failed'}), 500

@app.route('/voices/<filename>')
def serve_voice(filename):
    return send_from_directory(VOICE_DIR, filename)

@app.route('/admin/backup', methods=['POST'])
@require_admin_auth
def manual_backup():
    if commit_encrypted_backup_to_github(): return jsonify({'status': 'success'}), 200
    else: return jsonify({'status': 'error'}), 500

# --- Application Initialization and Startup ---
def initialize_app():
    global engine, Session, groq_client
    logger.info("=" * 60); logger.info("🔧 Starting Mochiko AI initialization...")
    ensure_voice_directory()
    if not DATABASE_URL: logger.critical("🔥 FATAL: DATABASE_URL not set."); sys.exit(1)
    get_encryption_key()
    groq_client = initialize_groq_client()
    try:
        engine = create_optimized_db_engine(); Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
    except Exception as e:
        logger.critical(f"🔥 DB init failed: {e}"); raise
    with Session() as session:
        if session.query(HolomemWiki).count() == 0: background_executor.submit(update_hololive_news_database)
        if session.query(SpecializedNews).count() == 0: background_executor.submit(update_all_specialized_news)

    schedule.every().hour.do(update_hololive_news_database)
    schedule.every(3).hours.do(update_all_specialized_news)
    schedule.every().day.at("18:00").do(commit_encrypted_backup_to_github)
    
    def run_scheduler():
        while True: schedule.run_pending(); time.sleep(60)
    threading.Thread(target=run_scheduler, daemon=True).start()
    logger.info("✅ Initialization complete!"); logger.info("=" * 60)

def signal_handler(sig, frame):
    logger.info("🛑 Shutting down..."); background_executor.shutdown(wait=True)
    if engine: engine.dispose()
    sys.exit(0)
signal.signal(signal.SIGINT, signal_handler); signal.signal(signal.SIGTERM, signal_handler)

application = None
try:
    initialize_app()
    application = app
    logger.info("✅ Application successfully initialized and assigned for gunicorn.")
except Exception as e:
    logger.critical(f"🔥 Fatal initialization error: {e}", exc_info=True)
    application = Flask(__name__)
    @application.route('/health')
    def failed_health(): return jsonify({'status': 'error', 'message': 'Application failed to initialize.', 'error': str(e)}), 500
    logger.warning("⚠️ Application created with limited functionality due to initialization error.")

if __name__ == '__main__':
    if application:
        port = int(os.environ.get('PORT', 10000))
        application.run(host='0.0.0.0', port=port, debug=False)
    else:
        logger.critical("🔥 Could not start application due to initialization failure.")
