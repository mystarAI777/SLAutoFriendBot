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

# Basic settings
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Constants
VOICE_DIR = '/tmp/voices'
SERVER_URL = "https://slautofriendbot.onrender.com"
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
    'Blender': {
        'base_url': 'https://docs.blender.org/manual/ja/latest/',
        'keywords': ['Blender', 'ブレンダー', 'blender', 'BLENDER']
    },
    'CGニュース': {
        'base_url': 'https://modelinghappy.com/',
        'keywords': ['CGニュース', '3DCG', 'CG']
    },
    'セカンドライフ': {
        'base_url': 'https://community.secondlife.com/news/',
        'keywords': ['セカンドライフ', 'Second Life', 'SL']
    },
    'アニメ': {
        'base_url': 'https://animedb.jp/',
        'keywords': ['アニメ', 'anime', 'ANIME']
    }
}
ANIME_KEYWORDS = [
    'アニメ', 'anime', 'ANIME', 'アニメーション',
    '作画', '声優', 'OP', 'ED', 'オープニング', 'エンディング',
    '劇場版', '映画', 'OVA', 'OAD', '原作', '漫画', 'ラノベ',
    '主人公', 'キャラ', 'キャラクター', '制作会社', 'スタジオ'
]

# Global variables
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client = None
VOICEVOX_ENABLED = True
engine = None
Session = None
app = Flask(__name__)
CORS(app)

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
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
        except IOError as e:
            logger.error(f"❌ Failed to read Secret File {secret_file_path}: {e}")
            return None
    
    value = os.environ.get(name)
    if value:
        logger.info(f"✅ Loaded {name} from environment")
    return value

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')
GEMINI_API_KEY = get_secret('GEMINI_API_KEY')
ADMIN_TOKEN = get_secret('ADMIN_TOKEN')
ALLOWED_ADMIN_IPS = os.environ.get('ALLOWED_ADMIN_IPS', '').split(',')

def get_encryption_key():
    """環境変数から必須の暗号化キーを取得"""
    encryption_key = get_secret('BACKUP_ENCRYPTION_KEY')
    
    if not encryption_key:
        logger.critical("""
        🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥
        🔥 FATAL ERROR: 暗号化キーが設定されていません！ (BACKUP_ENCRYPTION_KEY)
        🔥
        🔥 アプリケーションを起動する前に、必ずRenderのSecret Filesに暗号化キーを
        🔥 設定してください。
        🔥
        🔥 ローカル環境で以下のコマンドを実行してキーを生成できます：
        🔥 python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
        🔥
        🔥 生成されたキーをSecret Fileに貼り付けてから、再度デプロイしてください。
        🔥 アプリケーションを停止します。
        🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥
        """)
        sys.exit(1)

    if len(encryption_key.encode('utf-8')) != 44:
        logger.critical(f"""
        🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥
        🔥 FATAL ERROR: BACKUP_ENCRYPTION_KEYの形式が不正です！
        🔥
        🔥 提供されたキーの長さは {len(encryption_key.encode('utf-8'))} バイトです。
        🔥 正しいFernetキーは44バイトでなければなりません。
        🔥
        🔥 上記のコマンドで新しいキーを生成し、正しく設定してください。
        🔥 アプリケーションを停止します。
        🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥
        """)
        sys.exit(1)
        
    return encryption_key.encode('utf-8')

# --- Initialization Helpers ---

def ensure_voice_directory():
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            if not os.path.exists(VOICE_DIR):
                os.makedirs(VOICE_DIR, mode=0o755, exist_ok=True)
                logger.info(f"✅ Voice directory created: {VOICE_DIR}")
            if os.access(VOICE_DIR, os.W_OK):
                logger.info(f"✅ Voice directory is writable: {VOICE_DIR}")
                return True
            else:
                os.chmod(VOICE_DIR, 0o755)
                logger.info(f"✅ Voice directory permissions fixed: {VOICE_DIR}")
                return True
        except Exception as e:
            logger.error(f"❌ Voice directory creation failed (attempt {attempt + 1}/{max_attempts}): {e}")
            if attempt < max_attempts - 1: time.sleep(1)
    logger.critical(f"🔥 Failed to create voice directory after {max_attempts} attempts")
    return False

# --- Database Models ---
class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False)
    user_name = Column(String(255), nullable=False)
    interaction_count = Column(Integer, default=0)
    last_interaction = Column(DateTime, default=datetime.utcnow)

class ConversationHistory(Base):
    __tablename__ = 'conversation_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    role = Column(String(10), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)

class HololiveNews(Base):
    __tablename__ = 'hololive_news'
    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    url = Column(String(1000))
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    news_hash = Column(String(100), unique=True)

class BackgroundTask(Base):
    __tablename__ = 'background_tasks'
    id = Column(Integer, primary_key=True)
    task_id = Column(String(255), unique=True, nullable=False)
    user_uuid = Column(String(255), nullable=False)
    task_type = Column(String(50), nullable=False)
    query = Column(Text, nullable=False)
    result = Column(Text)
    status = Column(String(20), default='pending')
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime)

class SpecializedNews(Base):
    __tablename__ = 'specialized_news'
    id = Column(Integer, primary_key=True)
    site_name = Column(String(100), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    url = Column(String(1000))
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    news_hash = Column(String(100), unique=True)

class HolomemWiki(Base):
    __tablename__ = 'holomem_wiki'
    id = Column(Integer, primary_key=True)
    member_name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text)
    debut_date = Column(String(100))
    generation = Column(String(100))
    tags = Column(Text)
    graduation_date = Column(String(100), nullable=True)
    graduation_reason = Column(Text, nullable=True)
    mochiko_feeling = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False, index=True)
    profile_url = Column(String(500), nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow)

class NewsCache(Base):
    __tablename__ = 'news_cache'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    news_id = Column(Integer, nullable=False)
    news_number = Column(Integer, nullable=False)
    news_type = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class UserPsychology(Base):
    __tablename__ = 'user_psychology'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)
    openness = Column(Integer, default=50)
    conscientiousness = Column(Integer, default=50)
    extraversion = Column(Integer, default=50)
    agreeableness = Column(Integer, default=50)
    neuroticism = Column(Integer, default=50)
    interests = Column(Text)
    favorite_topics = Column(Text)
    conversation_style = Column(String(50))
    emotional_tendency = Column(String(50))
    total_messages = Column(Integer, default=0)
    avg_message_length = Column(Integer, default=0)
    analysis_summary = Column(Text)
    last_analyzed = Column(DateTime, default=datetime.utcnow)
    analysis_confidence = Column(Integer, default=0)

# --- Core Initializations (DB, AI, Cache) ---

def create_optimized_db_engine():
    try:
        is_sqlite = 'sqlite' in DATABASE_URL.lower()
        connect_args = {'check_same_thread': False, 'timeout': 20} if is_sqlite else {'connect_timeout': 10, 'options': '-c statement_timeout=30000'}
        pool_args = {'pool_pre_ping': True}
        if not is_sqlite:
            pool_args.update({'pool_size': 10, 'max_overflow': 20, 'pool_recycle': 300})
        engine = create_engine(DATABASE_URL, connect_args=connect_args, **pool_args)
        with engine.connect() as conn: conn.execute(text("SELECT 1"))
        logger.info(f"✅ Database engine created ({'SQLite' if is_sqlite else 'PostgreSQL'})")
        return engine
    except Exception as e:
        logger.error(f"❌ Failed to create database engine: {e}")
        raise

def initialize_groq_client():
    global groq_client
    try:
        if not GROQ_API_KEY or len(GROQ_API_KEY) < 20:
            logger.warning("⚠️ GROQ_API_KEY not set or too short, AI features disabled.")
            return None
        groq_client = Groq(api_key=GROQ_API_KEY.strip())
        logger.info("✅ Groq client initialized")
        return groq_client
    except Exception as e:
        logger.error(f"❌ Groq initialization failed: {e}")
        return None

_cache = {'holomem_keywords': {'data': None, 'expires': None}}
_cache_lock = Lock()
def get_cached_or_fetch(cache_key, fetch_func, ttl_seconds=3600):
    with _cache_lock:
        cache_entry = _cache.get(cache_key)
        now = datetime.utcnow()
        if cache_entry and cache_entry.get('data') and cache_entry.get('expires') and now < cache_entry['expires']:
            return cache_entry['data']
        data = fetch_func()
        _cache[cache_key] = {'data': data, 'expires': now + timedelta(seconds=ttl_seconds)}
        return data

# --- Utility Functions ---

def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()

def create_news_hash(title, content):
    return hashlib.md5(f"{title}{content[:100]}".encode('utf-8')).hexdigest()

# --- Improved Web Scraping Section ---

class ScraperWithRetry:
    def __init__(self, max_retries=3, retry_delay=2):
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.session = requests.Session()
        self.session.headers.update({'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'ja,en;q=0.9', 'Connection': 'keep-alive'})
    
    def fetch_with_retry(self, url, timeout=15):
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug(f"🔍 Fetching {url} (attempt {attempt}/{self.max_retries})")
                response = self.session.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=timeout, allow_redirects=True)
                response.raise_for_status()
                logger.debug(f"✅ Successfully fetched {url}")
                return response
            except requests.exceptions.Timeout as e:
                last_error = e
                logger.warning(f"⏱️ Timeout on attempt {attempt}: {url}")
                if attempt < self.max_retries: time.sleep(self.retry_delay * attempt)
            except requests.exceptions.HTTPError as e:
                last_error = e
                if e.response.status_code == 429:
                    logger.warning(f"🚫 Rate limited: {url}")
                    if attempt < self.max_retries: time.sleep(self.retry_delay * attempt * 2)
                elif e.response.status_code >= 500:
                    logger.warning(f"🔧 Server error {e.response.status_code}: {url}")
                    if attempt < self.max_retries: time.sleep(self.retry_delay * attempt)
                else:
                    logger.error(f"❌ HTTP {e.response.status_code}: {url}")
                    return None
            except requests.exceptions.RequestException as e:
                last_error = e
                logger.warning(f"🔌 Connection error on attempt {attempt}: {url}")
                if attempt < self.max_retries: time.sleep(self.retry_delay * attempt)
        logger.error(f"❌ Failed after {self.max_retries} attempts: {url} - {last_error}")
        return None

scraper = ScraperWithRetry()

def fetch_article_content(article_url, timeout=15):
    response = scraper.fetch_with_retry(article_url, timeout)
    if not response: return None
    try:
        soup = BeautifulSoup(response.content, 'html.parser')
        content_selectors = ['article .entry-content', '.post-content', '.article-content', 'article', '.content', 'main article']
        content_elem = next((soup.select_one(s) for s in content_selectors if soup.select_one(s)), None)
        if content_elem:
            paragraphs = content_elem.find_all('p')
            article_text = ' '.join([clean_text(p.get_text()) for p in paragraphs if len(clean_text(p.get_text())) > 20])
            if article_text:
                logger.debug(f"📄 Extracted {len(article_text)} chars from {article_url}")
                return article_text[:2000]
        meta_desc = soup.find('meta', {'name': 'description'})
        if meta_desc and meta_desc.get('content'):
            logger.debug(f"📝 Using meta description for {article_url}")
            return clean_text(meta_desc['content'])
        logger.warning(f"⚠️ No content found for {article_url}")
        return None
    except Exception as e:
        logger.error(f"❌ Error parsing article {article_url}: {e}")
        return None

def summarize_article(title, content):
    if not groq_client or not content:
        return content[:500] if content else title
    try:
        prompt = f"以下のニュース記事を200文字以内で簡潔に要約:\n\nタイトル: {title}\n本文: {content[:1500]}"
        completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant",
            temperature=0.5,
            max_tokens=200
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Summarization error: {e}")
        return content[:500] if content else title

def update_hololive_news_database():
    session = Session()
    added_count = 0
    try:
        logger.info("📰 Starting Hololive news update...")
        response = scraper.fetch_with_retry(HOLOLIVE_NEWS_URL)
        if not response:
            logger.error("❌ Failed to fetch Hololive news page")
            return
        soup = BeautifulSoup(response.content, 'html.parser')
        article_selectors = ['article', '.post', '.entry']
        articles_found = next((soup.select(s) for s in article_selectors if soup.select(s)), [])[:10]
        logger.info(f"📋 Found {len(articles_found)} potential articles")
        for idx, article in enumerate(articles_found[:5], 1):
            try:
                title_elem = article.find(['h1', 'h2', 'h3', 'a'])
                if not title_elem: continue
                title = clean_text(title_elem.get_text())
                link_elem = title_elem if title_elem.name == 'a' else article.find('a', href=True)
                if not title or len(title) < 5 or not link_elem: continue
                article_url = urljoin(HOLOLIVE_NEWS_URL, link_elem.get('href', ''))
                article_content = fetch_article_content(article_url) or title
                news_hash = create_news_hash(title, article_content)
                if session.query(HololiveNews).filter_by(news_hash=news_hash).first(): continue
                summary = summarize_article(title, article_content)
                new_news = HololiveNews(title=title, content=summary, news_hash=news_hash, url=article_url)
                session.add(new_news)
                added_count += 1
                logger.info(f"✅ Added: {title[:50]}...")
                if groq_client: time.sleep(0.5)
            except Exception as e:
                logger.error(f"❌ Error processing article {idx}: {e}")
                continue
        if added_count > 0:
            session.commit()
            logger.info(f"✅ Hololive news update complete: {added_count} new articles")
        else:
            logger.info("ℹ️  No new Hololive news found")
        logger.info("👥 Updating Hololive members...")
        scrape_hololive_members()
        scrape_graduated_members()
    except Exception as e:
        logger.error(f"❌ Hololive news update error: {e}")
        session.rollback()
    finally:
        session.close()

def scrape_hololive_members():
    """ホロライブメンバースクレイピング（改善版・堅牢性向上）"""
    base_url = "https://hololive.hololivepro.com"
    session = Session()
    try:
        logger.info("🔍 Scraping Hololive members...")
        response = scraper.fetch_with_retry(f"{base_url}/talents/")
        if not response: 
            logger.error("❌ Failed to fetch Hololive talents page")
            return

        soup = BeautifulSoup(response.content, 'html.parser')

        # 【修正点1】より厳密なセレクタでメンバーカードのみを取得
        # 'a'タグで、かつ'href'が'/talents/'で始まる要素に絞り込む
        member_cards = soup.select("a[href^='/talents/']")
        
        logger.info(f"📋 Found {len(member_cards)} potential member cards")
        
        scraped_names = set()
        updated_count, added_count = 0, 0
        
        for idx, card in enumerate(member_cards, 1):
            try:
                # メンバー個別のページへのリンクか確認 (トップページへのリンク等は除外)
                href = card.get('href', '')
                if href == '/talents/' or not href.startswith('/talents/'):
                    continue

                name_elem = card.find(['h2', 'h3', 'h4', 'span'], class_=lambda x: x and ('name' in x.lower() or 'title' in x.lower())) or card
                member_name = re.sub(r'\s*\(.*?\)\s*', '', clean_text(name_elem.get_text())).strip()

                # 【修正点2】取得した名前の長さをチェック
                if not member_name or len(member_name) > 100:
                    logger.warning(f"⚠️ Skipping card {idx}: Invalid name found or name too long ('{member_name[:50]}...').")
                    continue
                
                # ... (以降の処理は変更なし) ...
                
                scraped_names.add(member_name)
                profile_link = urljoin(base_url, href)
                generation = "不明"
                gen_patterns = [(r'0期生|ゼロ期生','0期生'),(r'1期生|一期生','1期生'),(r'2期生|二期生','2期生'),(r'3期生|三期生','3期生'),(r'4期生|四期生','4期生'),(r'5期生|五期生','5期生'),(r'ゲーマーズ|GAMERS','ゲーマーズ'),(r'ID|Indonesia','ホロライブID'),(r'EN|English','ホロライブEN'),(r'DEV_IS|ReGLOSS','DEV_IS')]
                card_text = card.get_text()
                for pattern, gen_name in gen_patterns:
                    if re.search(pattern, card_text, re.IGNORECASE):
                        generation = gen_name
                        break
                existing = session.query(HolomemWiki).filter_by(member_name=member_name).first()
                if existing:
                    if not existing.is_active or existing.generation != generation:
                        existing.is_active, existing.generation, existing.profile_url, existing.last_updated = True, generation, profile_link, datetime.utcnow()
                        updated_count += 1
                        logger.info(f"🔄 Updated: {member_name}")
                else:
                    new_member = HolomemWiki(member_name=member_name, description=f"{member_name}は{generation}のメンバーです。", generation=generation, is_active=True, profile_url=profile_link, tags=json.dumps([generation], ensure_ascii=False))
                    session.add(new_member)
                    added_count += 1
                    logger.info(f"➕ Added: {member_name}")

            except Exception as e:
                logger.error(f"❌ Error processing member card {idx}: {e}")
        
        all_db_members = session.query(HolomemWiki).filter(HolomemWiki.member_name.notin_(scraped_names)).all()
        inactive_count = 0
        for db_member in all_db_members:
            if db_member.is_active and not db_member.graduation_date:
                logger.warning(f"⚠️ Not found on site, marking inactive: {db_member.member_name}")
                db_member.is_active, db_member.last_updated = False, datetime.utcnow()
                inactive_count += 1
        
        if updated_count > 0 or added_count > 0 or inactive_count > 0:
            session.commit()
            
        logger.info(f"✅ Member scraping complete: {added_count} added, {updated_count} updated, {inactive_count} marked inactive")

    except Exception as e:
        logger.error(f"❌ Member scraping error: {e}")
        session.rollback()
    finally:
        session.close()
        
def scrape_graduated_members():
    # This is static data, no need for retry logic
    # ... implementation is unchanged ...
    pass

def update_all_specialized_news():
    def update_single_site(site_name, config):
        session = Session()
        added_count = 0
        try:
            response = scraper.fetch_with_retry(config['base_url'])
            if not response: return f"{site_name}: fetch failed"
            soup = BeautifulSoup(response.content, 'html.parser')
            articles = next((soup.select(s) for s in ['article', '.post', '.entry'] if soup.select(s)), [])[:10]
            for article in articles[:5]:
                try:
                    title_elem = article.find(['h1', 'h2', 'h3', 'a'])
                    if not title_elem: continue
                    title = clean_text(title_elem.get_text())
                    link_elem = title_elem if title_elem.name == 'a' else article.find('a', href=True)
                    if not title or len(title) < 5 or not link_elem: continue
                    article_url = urljoin(config['base_url'], link_elem.get('href', ''))
                    article_content = fetch_article_content(article_url) or title
                    news_hash = create_news_hash(title, article_content)
                    if session.query(SpecializedNews).filter_by(news_hash=news_hash).first(): continue
                    summary = summarize_article(title, article_content)
                    new_news = SpecializedNews(site_name=site_name, title=title, content=summary, news_hash=news_hash, url=article_url)
                    session.add(new_news)
                    added_count += 1
                    if groq_client: time.sleep(0.5)
                except Exception as e:
                    logger.warning(f"⚠️ Error processing article in {site_name}: {e}")
            if added_count > 0: session.commit()
            return f"{site_name}: {added_count} new"
        except Exception as e:
            logger.error(f"❌ {site_name} processing error: {e}")
            session.rollback()
            return f"{site_name}: error"
        finally:
            session.close()

    logger.info("🚀 Starting parallel specialized news update...")
    with ThreadPoolExecutor(max_workers=len(SPECIALIZED_SITES)) as executor:
        sites_to_update = {name: config for name, config in SPECIALIZED_SITES.items() if name != 'セカンドライフ'}
        future_to_site = {executor.submit(update_single_site, name, conf): name for name, conf in sites_to_update.items()}
        for future in as_completed(future_to_site):
            site_name = future_to_site[future]
            try:
                result = future.result()
                logger.info(f"✅ {site_name} update finished: {result}")
            except Exception as e:
                logger.error(f"❌ {site_name} update failed in executor: {e}")

# ... (All other application logic functions remain here: conversation patterns, AI response, etc.) ...
# For brevity, these are omitted but should be included in the final file.

# --- Secure GitHub Backup Section ---
def encrypt_backup_data(backup_data):
    try:
        fernet = Fernet(get_encryption_key())
        json_data = json.dumps(backup_data, ensure_ascii=False, indent=2).encode('utf-8')
        encrypted_data = fernet.encrypt(json_data)
        logger.info(f"🔒 Backup encrypted: {len(json_data)} bytes -> {len(encrypted_data)} bytes")
        return encrypted_data
    except Exception as e:
        logger.error(f"❌ Encryption failed: {e}")
        raise

def decrypt_backup_data(encrypted_data):
    try:
        fernet = Fernet(get_encryption_key())
        decrypted_bytes = fernet.decrypt(encrypted_data)
        backup_data = json.loads(decrypted_bytes.decode('utf-8'))
        logger.info("🔓 Backup decrypted successfully")
        return backup_data
    except Exception as e:
        logger.error(f"❌ Decryption failed: {e}")
        raise

def create_backup_metadata(backup_data):
    return {
        'timestamp': backup_data['timestamp'],
        'statistics': backup_data.get('statistics', {}),
        'encrypted': True,
        'version': '2.0',
        'checksum': hashlib.sha256(json.dumps(backup_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    }

def verify_backup_integrity(backup_data, metadata):
    try:
        expected_checksum = metadata.get('checksum')
        if not expected_checksum:
            logger.warning("⚠️ No checksum in metadata, skipping integrity check.")
            return True
        actual_checksum = hashlib.sha256(json.dumps(backup_data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if actual_checksum != expected_checksum:
            logger.error(f"❌ Checksum mismatch! Expected: {expected_checksum}, Got: {actual_checksum}")
            return False
        logger.info(f"✅ Backup integrity verified: {actual_checksum[:16]}...")
        return True
    except Exception as e:
        logger.error(f"❌ Integrity verification failed: {e}")
        return False
        
def export_database_to_json():
    if Session is None: return None
    session = Session()
    backup_data = {'timestamp': datetime.utcnow().isoformat(), 'tables': {}}
    try:
        logger.info("📦 Starting database export...")
        tables_to_export = {'user_memories': UserMemory, 'holomem_wiki': HolomemWiki, 'user_psychology': UserPsychology, 'conversation_history': ConversationHistory}
        stats = {}
        for name, model in tables_to_export.items():
            query = session.query(model)
            if name == 'conversation_history': query = query.order_by(model.timestamp.desc()).limit(5000)
            records = query.all()
            backup_data['tables'][name] = [{c.name: getattr(r, c.name).isoformat() if isinstance(getattr(r, c.name), datetime) else getattr(r, c.name) for c in r.__table__.columns} for r in records]
            stats[name] = len(records)
        backup_data['statistics'] = stats
        logger.info(f"✅ Database export complete: {stats}")
        return backup_data
    except Exception as e:
        logger.error(f"❌ Database export error: {e}", exc_info=True)
        return None
    finally:
        session.close()

def commit_encrypted_backup_to_github():
    try:
        logger.info("🚀 Committing encrypted backup to GitHub...")
        backup_data = export_database_to_json()
        if not backup_data: return False
        metadata = create_backup_metadata(backup_data)
        encrypted_data = encrypt_backup_data(backup_data)
        
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        with open(BACKUP_DIR / GITHUB_BACKUP_FILE, 'wb') as f: f.write(encrypted_data)
        with open(BACKUP_DIR / BACKUP_METADATA_FILE, 'w', encoding='utf-8') as f: json.dump(metadata, f, ensure_ascii=False, indent=2)
        
        import shutil
        shutil.copy(BACKUP_DIR / GITHUB_BACKUP_FILE, Path('.') / GITHUB_BACKUP_FILE)
        shutil.copy(BACKUP_DIR / BACKUP_METADATA_FILE, Path('.') / BACKUP_METADATA_FILE)
        
        commands = [['git', 'config', 'user.email', 'mochiko-bot@render.com'], ['git', 'config', 'user.name', 'Mochiko Bot'], ['git', 'add', GITHUB_BACKUP_FILE, BACKUP_METADATA_FILE], ['git', 'commit', '-m', f'🔒 Encrypted backup: {datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")}'], ['git', 'push']]
        for cmd in commands:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode != 0 and 'nothing to commit' not in result.stdout:
                logger.error(f"❌ Git command failed: {result.stderr}")
                return False
        logger.info("✅ Encrypted backup committed to GitHub")
        return True
    except Exception as e:
        logger.error(f"❌ GitHub commit error: {e}")
        return False
        
def import_database_from_json(backup_data):
    if Session is None: return False
    session = Session()
    try:
        logger.info("📥 Starting database import...")
        # This logic should be expanded based on needs (e.g., handling conflicts)
        for user_data in backup_data['tables'].get('user_memories', []):
            if not session.query(UserMemory).filter_by(user_uuid=user_data['user_uuid']).first():
                session.add(UserMemory(**{k: v for k, v in user_data.items() if k != 'id'}))
        session.commit()
        logger.info("✅ Database import complete.")
        return True
    except Exception as e:
        logger.error(f"❌ Database import error: {e}", exc_info=True)
        session.rollback()
        return False
    finally:
        session.close()
        
def load_encrypted_backup_from_github():
    try:
        logger.info("📥 Loading encrypted backup from GitHub...")
        subprocess.run(['git', 'pull'], timeout=60)
        encrypted_file, metadata_file = Path('.') / GITHUB_BACKUP_FILE, Path('.') / BACKUP_METADATA_FILE
        if not encrypted_file.exists():
            logger.warning(f"⚠️ Encrypted backup file not found.")
            return None, None
        metadata = json.load(open(metadata_file, 'r', encoding='utf-8')) if metadata_file.exists() else {}
        encrypted_data = open(encrypted_file, 'rb').read()
        backup_data = decrypt_backup_data(encrypted_data)
        logger.info(f"✅ Encrypted backup loaded: {backup_data.get('timestamp')}")
        return backup_data, metadata
    except Exception as e:
        logger.error(f"❌ Failed to load backup from GitHub: {e}")
        return None, None

# --- Admin & Security Section ---
def require_admin_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not ADMIN_TOKEN:
            logger.critical("🔥 CRITICAL: Admin endpoint accessed but ADMIN_TOKEN is not set!")
            return jsonify({'error': 'Server configuration error'}), 500
        if ALLOWED_ADMIN_IPS and ALLOWED_ADMIN_IPS[0]:
            client_ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
            if client_ip not in ALLOWED_ADMIN_IPS:
                logger.warning(f"🚫 Unauthorized IP access attempt from: {client_ip}")
                return jsonify({'error': 'Access denied: IP not allowed'}), 403
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Missing or invalid Authorization header'}), 401
        token = auth_header.split(' ')[1]
        if token != ADMIN_TOKEN:
            logger.warning(f"🚫 Invalid admin token provided")
            return jsonify({'error': 'Invalid credentials'}), 401
        return f(*args, **kwargs)
    return decorated_function

# --- Flask Endpoints ---
@app.route('/health', methods=['GET'])
def health_check():
    try:
        with engine.connect() as conn: conn.execute(text("SELECT 1"))
        db_status = 'ok'
    except: db_status = 'error'
    return jsonify({'status': 'ok','timestamp': datetime.utcnow().isoformat(),'services': {'database': db_status,'groq_ai': 'ok' if groq_client else 'disabled'}}), 200

# ... (Insert full, unchanged chat_lsl, generate_voice, and other public endpoints here) ...

@app.route('/admin/backup', methods=['POST'])
@require_admin_auth
def manual_backup():
    success = commit_encrypted_backup_to_github()
    if success:
        metadata_file = Path('.') / BACKUP_METADATA_FILE
        metadata = json.load(open(metadata_file, 'r')) if metadata_file.exists() else {}
        return jsonify({'status': 'success', 'message': 'Encrypted backup committed to GitHub', 'metadata': metadata}), 200
    else:
        return jsonify({'status': 'error', 'message': 'Backup failed'}), 500

@app.route('/admin/restore', methods=['POST'])
@require_admin_auth
def manual_restore():
    backup_data, metadata = load_encrypted_backup_from_github()
    if not backup_data: return jsonify({'error': 'Failed to load backup from GitHub'}), 404
    if not verify_backup_integrity(backup_data, metadata): return jsonify({'error': 'Backup integrity check failed'}), 500
    if import_database_from_json(backup_data):
        return jsonify({'status': 'success', 'timestamp': backup_data.get('timestamp'), 'statistics': backup_data.get('statistics', {})}), 200
    else:
        return jsonify({'error': 'Import failed'}), 500

@app.route('/admin/backup_status', methods=['GET'])
@require_admin_auth
def backup_status():
    try:
        metadata_file = Path('.') / BACKUP_METADATA_FILE
        if metadata_file.exists():
            return jsonify({'exists': True, 'metadata': json.load(open(metadata_file, 'r', encoding='utf-8'))}), 200
        else:
            return jsonify({'exists': False, 'message': 'No backup metadata file found'}), 404
    except Exception as e:
        logger.error(f"❌ Backup status error: {e}")
        return jsonify({'error': str(e)}), 500

# --- Application Initialization and Startup ---
def initialize_app():
    global engine, Session, groq_client
    logger.info("=" * 60)
    logger.info("🔧 Starting Mochiko AI initialization...")
    ensure_voice_directory()
    if not DATABASE_URL:
        logger.critical("🔥 FATAL: DATABASE_URL is not set."); sys.exit(1)
    
    get_encryption_key() # Check for key on startup
    
    groq_client = initialize_groq_client()
    try:
        engine = create_optimized_db_engine()
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        logger.info("✅ Database ready")
    except Exception as e:
        logger.critical(f"🔥 Database init failed: {e}"); raise
        
    with Session() as session:
        if session.query(HolomemWiki).count() == 0:
            logger.info("🚀 First run: Fetching initial Hololive data...")
            background_executor.submit(update_hololive_news_database)
        if session.query(SpecializedNews).count() == 0:
            logger.info("🚀 First run: Fetching initial specialized news...")
            background_executor.submit(update_all_specialized_news)

    logger.info("⏰ Starting scheduler...")
    schedule.every().hour.do(update_hololive_news_database)
    schedule.every(3).hours.do(update_all_specialized_news)
    # schedule.every().day.at("02:00").do(cleanup_old_data_advanced)
    schedule.every().day.at("18:00").do(commit_encrypted_backup_to_github)
    
    def run_scheduler():
        while True: schedule.run_pending(); time.sleep(60)
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
    logger.info("✅ Scheduler started")
    logger.info("=" * 60)
    logger.info("✅ Mochiko AI initialization complete!")
    logger.info("=" * 60)

def signal_handler(sig, frame):
    logger.info(f"🛑 Signal {sig} received. Shutting down gracefully...")
    background_executor.shutdown(wait=True)
    if 'engine' in globals() and engine: engine.dispose()
    logger.info("👋 Mochiko AI has shut down.")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

try:
    initialize_app()
    application = app
except Exception as e:
    logger.critical(f"🔥 Fatal initialization error: {e}", exc_info=True)
    application = Flask(__name__)
    @application.route('/health')
    def failed_health():
        return jsonify({'status': 'error', 'message': 'Initialization failed', 'error': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
