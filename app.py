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

# --- Database Models (All models are included) ---
class UserMemory(Base): __tablename__ = 'user_memories'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False); user_name = Column(String(255), nullable=False); interaction_count = Column(Integer, default=0); last_interaction = Column(DateTime, default=datetime.utcnow)
class ConversationHistory(Base): __tablename__ = 'conversation_history'; id = Column(Integer, primary_key=True, autoincrement=True); user_uuid = Column(String(255), nullable=False, index=True); role = Column(String(10), nullable=False); content = Column(Text, nullable=False); timestamp = Column(DateTime, default=datetime.utcnow, index=True)
class HololiveNews(Base): __tablename__ = 'hololive_news'; id = Column(Integer, primary_key=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True)
class BackgroundTask(Base): __tablename__ = 'background_tasks'; id = Column(Integer, primary_key=True); task_id = Column(String(255), unique=True, nullable=False); user_uuid = Column(String(255), nullable=False); task_type = Column(String(50), nullable=False); query = Column(Text, nullable=False); result = Column(Text); status = Column(String(20), default='pending'); created_at = Column(DateTime, default=datetime.utcnow, index=True); completed_at = Column(DateTime)
class SpecializedNews(Base): __tablename__ = 'specialized_news'; id = Column(Integer, primary_key=True); site_name = Column(String(100), nullable=False, index=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True)
class HolomemWiki(Base): __tablename__ = 'holomem_wiki'; id = Column(Integer, primary_key=True); member_name = Column(String(100), nullable=False, unique=True, index=True); description = Column(Text); debut_date = Column(String(100)); generation = Column(String(100)); tags = Column(Text); graduation_date = Column(String(100), nullable=True); graduation_reason = Column(Text, nullable=True); mochiko_feeling = Column(Text, nullable=True); is_active = Column(Boolean, default=True, nullable=False, index=True); profile_url = Column(String(500), nullable=True); last_updated = Column(DateTime, default=datetime.utcnow)
class NewsCache(Base): __tablename__ = 'news_cache'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), nullable=False, index=True); news_id = Column(Integer, nullable=False); news_number = Column(Integer, nullable=False); news_type = Column(String(50), nullable=False); created_at = Column(DateTime, default=datetime.utcnow)
class UserPsychology(Base): __tablename__ = 'user_psychology'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False, index=True); user_name = Column(String(255), nullable=False); openness = Column(Integer, default=50); conscientiousness = Column(Integer, default=50); extraversion = Column(Integer, default=50); agreeableness = Column(Integer, default=50); neuroticism = Column(Integer, default=50); interests = Column(Text); favorite_topics = Column(Text); conversation_style = Column(String(50)); emotional_tendency = Column(String(50)); total_messages = Column(Integer, default=0); avg_message_length = Column(Integer, default=0); analysis_summary = Column(Text); last_analyzed = Column(DateTime, default=datetime.utcnow); analysis_confidence = Column(Integer, default=0)

# --- Core Initializations (DB, AI, Cache) ---
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
    # ... implementation is unchanged ...
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
            
# ... (Full application code continues below) ...
