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
from concurrent.futures import ThreadPoolExecutor
import schedule
import signal

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
app = Flask(__name__)
CORS(app)

@app.after_request
def after_request(response):
    """Add CORS headers to all responses"""
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

Base = declarative_base()

# Secret management
def get_secret(name):
    """Read secrets from Render Secret Files or environment variables"""
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

def ensure_voice_directory():
    """Ensure voice directory exists"""
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
            if attempt < max_attempts - 1:
                time.sleep(1)
            continue
    
    logger.critical(f"🔥 Failed to create voice directory after {max_attempts} attempts")
    return False

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')
GEMINI_API_KEY = get_secret('GEMINI_API_KEY')
gemini_model = None

def initialize_gemini_client():
    global gemini_model
    try:
        if GEMINI_API_KEY and len(GEMINI_API_KEY) > 20:
            genai.configure(api_key=GEMINI_API_KEY)
            gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')
            logger.info("✅ Gemini 2.0 Flash client initialized")
    except Exception as e:
        logger.error(f"❌ Gemini initialization failed: {e}")

# Database Models
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
    published_date = Column(DateTime, default=datetime.utcnow)
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
    published_date = Column(DateTime, default=datetime.utcnow)
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

class FriendRegistration(Base):
    __tablename__ = 'friend_registrations'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    friend_uuid = Column(String(255), nullable=False)
    friend_name = Column(String(255), nullable=False)
    registered_at = Column(DateTime, default=datetime.utcnow)
    relationship_note = Column(Text)

class NewsCache(Base):
    __tablename__ = 'news_cache'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    news_id = Column(Integer, nullable=False)
    news_number = Column(Integer, nullable=False)
    news_type = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class UserPsychology(Base):
    """User psychology analysis results"""
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

# Database and client initialization
def create_optimized_db_engine():
    """Create optimized database engine"""
    try:
        is_sqlite = 'sqlite' in DATABASE_URL.lower()
        if is_sqlite:
            engine = create_engine(
                DATABASE_URL,
                connect_args={'check_same_thread': False, 'timeout': 20},
                pool_pre_ping=True,
                echo=False
            )
        else:
            engine = create_engine(
                DATABASE_URL,
                connect_args={'connect_timeout': 10, 'options': '-c statement_timeout=30000'},
                pool_size=10,
                max_overflow=20,
                pool_pre_ping=True,
                pool_recycle=300,
                echo=False
            )
        
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        
        logger.info(f"✅ Database engine created ({'SQLite' if is_sqlite else 'PostgreSQL'})")
        return engine
    except Exception as e:
        logger.error(f"❌ Failed to create database engine: {e}")
        raise

def initialize_groq_client():
    """Initialize Groq client"""
    global groq_client
    try:
        if not GROQ_API_KEY or len(GROQ_API_KEY) < 20:
            logger.warning("⚠️ GROQ_API_KEY not set or too short")
            return None
        
        client = Groq(api_key=GROQ_API_KEY.strip())
        logger.info("✅ Groq client initialized")
        return client
    except Exception as e:
        logger.error(f"❌ Groq initialization failed: {e}")
        return None

# Utility functions
def clean_text(text):
    if not text:
        return ""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()

def get_japan_time():
    now = datetime.now(timezone(timedelta(hours=9)))
    return f"今は{now.year}年{now.month}月{now.day}日の{now.hour}時{now.minute}分だよ！"

def create_news_hash(title, content):
    return hashlib.md5(f"{title}{content[:100]}".encode('utf-8')).hexdigest()

def limit_text_for_sl(text, max_length=SL_SAFE_CHAR_LIMIT):
    return text if len(text) <= max_length else text[:max_length - 3] + "..."

def extract_location(message):
    for location in LOCATION_CODES.keys():
        if location in message:
            return location
    return "東京"

# Conversation pattern detection
def is_time_request(message):
    return any(keyword in message for keyword in ['今何時', '時間', '時刻', '何時'])

def is_weather_request(message):
    return any(keyword in message for keyword in ['天気', 'てんき', '気温', '雨', '晴れ'])

def is_recommendation_request(message):
    return any(keyword in message for keyword in ['おすすめ', 'オススメ', '推薦', '紹介して'])

def detect_specialized_topic(message):
    message_normalized = unicodedata.normalize('NFKC', message).lower()
    for topic, config in SPECIALIZED_SITES.items():
        for keyword in config['keywords']:
            if unicodedata.normalize('NFKC', keyword).lower() in message_normalized:
                logger.info(f"🎯 Specialized topic detected: {topic}")
                return topic
    return None

def is_detailed_request(message):
    return any(kw in message for kw in ['詳しく', '詳細', 'くわしく', '教えて', '説明して'])

def is_explicit_search_request(message):
    return any(kw in message for kw in ['調べて', '検索して', '探して', 'WEB検索'])

def is_story_request(message):
    return any(kw in message for kw in ['面白い話', 'おもしろい話', '話して', '雑談', 'ネタ'])

def is_emotional_expression(message):
    emotional_keywords = {
        '眠': ['眠たい', '眠い', 'ねむい'],
        '疲': ['疲れた', 'つかれた'],
        '嬉': ['嬉しい', 'うれしい'],
        '楽': ['楽しい', 'たのしい'],
        '悲': ['悲しい', 'かなしい'],
        '寂': ['寂しい', 'さびしい'],
        '怒': ['怒', 'むかつく', 'イライラ'],
        '暇': ['暇', 'ひま']
    }
    for key, keywords in emotional_keywords.items():
        if any(kw in message for kw in keywords):
            return key
    return None

def is_seasonal_topic(message):
    return any(kw in message for kw in ['お月見', '花見', '紅葉', 'クリスマス', '正月'])

def is_short_response(message):
    msg = message.strip()
    if len(msg) <= 3:
        return True
    short_responses = ['うん', 'そう', 'はい', 'そっか', 'なるほど', 'ふーん', 'へー']
    if msg in short_responses:
        return True
    if len(msg) <= 10 and msg.endswith('かな'):
        return True
    return False

def is_news_detail_request(message):
    match = re.search(r'([1-9]|[１-９])番|【([1-9]|[１-９])】', message)
    if match and any(kw in message for kw in ['詳しく', '詳細', '教えて', 'もっと']):
        number_str = next(filter(None, match.groups()))
        return int(unicodedata.normalize('NFKC', number_str))
    return None

def is_friend_request(message):
    return any(fk in message for fk in ['友だち', '友達', 'フレンド']) and \
           any(ak in message for ak in ['登録', '教えて', '誰', 'リスト'])

def is_anime_request(message):
    message_normalized = unicodedata.normalize('NFKC', message).lower()
    for keyword in ANIME_KEYWORDS:
        if unicodedata.normalize('NFKC', keyword).lower() in message_normalized:
            return True
    anime_patterns = [r'ってアニメ', r'というアニメ', r'のアニメ']
    return any(re.search(pattern, message) for pattern in anime_patterns)

def is_follow_up_question(message, history):
    """Detect follow-up questions"""
    if not history or len(history) < 2:
        return False
    
    last_assistant_msg = next((h.content for h in history if h.role == 'assistant'), None)
    if not last_assistant_msg:
        return False
    
    follow_up_patterns = [
        r'もっと(?:詳しく|くわしく)',
        r'(?:それ|これ)(?:について|って)?(?:詳しく|くわしく)',
        r'(?:なぜ|どうして|なんで)',
        r'(?:どういう|どんな)(?:こと|意味|感じ)',
        r'(?:例えば|たとえば)',
        r'(?:具体的|ぐたいてき)には?',
        r'(?:他|ほか)には?',
        r'(?:続き|つづき)'
    ]
    
    for pattern in follow_up_patterns:
        if re.search(pattern, message):
            logger.info(f"🔍 Follow-up detected: {pattern}")
            return True
    return False

def get_active_holomem_keywords():
    """Get active Hololive member names from DB"""
    session = Session()
    try:
        members = session.query(HolomemWiki.member_name).filter_by(is_active=True).all()
        return [m[0] for m in members] + ['ホロライブ', 'ホロメン', 'hololive', 'YAGOO']
    except Exception as e:
        logger.error(f"❌ Failed to get holomem keywords: {e}")
        return ['ホロライブ', 'ホロメン', 'hololive', 'YAGOO']
    finally:
        session.close()

def is_hololive_request(message):
    """Check if message is about Hololive"""
    return any(keyword in message for keyword in get_active_holomem_keywords())

def should_search(message):
    """Determine if web search is needed"""
    if is_short_response(message) or is_explicit_search_request(message):
        return False
    
    if detect_specialized_topic(message):
        return True
    
    # Check for Hololive member specific questions
    for member_name in get_active_holomem_keywords():
        if member_name in message:
            if not any(kw in message for kw in ['ニュース', '最新', '情報', 'お知らせ']):
                if len(message.replace(member_name, '').strip()) > 5:
                    return True
    
    if is_recommendation_request(message):
        return True
    
    search_patterns = [
        r'(?:とは|について|教えて|説明して|解説して)',
        r'(?:誰ですか|何ですか|どこですか|いつですか|なぜですか|どうして)'
    ]
    return any(re.search(pattern, message) for pattern in search_patterns)

# Anime search functionality
def search_anime_database(query, is_detailed=False):
    """Search anime database"""
    base_url = "https://animedb.jp/"
    try:
        logger.info(f"🎬 Searching anime: {query}")
        search_url = f"{base_url}search?q={quote_plus(query)}"
        response = requests.get(
            search_url,
            headers={'User-Agent': random.choice(USER_AGENTS)},
            timeout=15,
            allow_redirects=True
        )
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        results = []
        
        result_selectors = [
            'div.anime-item', 'div.search-result', 'article.anime',
            'div[class*="anime"]', 'li.anime-list-item'
        ]
        
        result_elements = next((soup.select(s) for s in result_selectors if soup.select(s)), [])
        
        if not result_elements:
            potential_results = soup.find_all(['div', 'article'], limit=10)
            result_elements = [elem for elem in potential_results if elem.find(['h2', 'h3', 'h4'])]

        for elem in result_elements[:3 if is_detailed else 2]:
            title_elem = elem.find(['h2', 'h3', 'h4', 'a'])
            if not title_elem:
                continue
            
            title = clean_text(title_elem.get_text())
            description_elem = elem.find('p')
            description = clean_text(description_elem.get_text()) if description_elem else ""
            link_elem = elem.find('a', href=True)
            link = urljoin(base_url, link_elem['href']) if link_elem else ""
            
            if title and len(title) > 2:
                results.append({
                    'title': title,
                    'description': description[:300] if description else "詳細情報なし",
                    'url': link
                })
        
        if not results:
            return None
        
        formatted_results = [
            f"【{i}】{r['title']}\n{r['description'][:150]}..."
            for i, r in enumerate(results, 1)
        ]
        return "\n\n".join(formatted_results)
        
    except Exception as e:
        logger.error(f"❌ Anime search error: {e}")
        return None

# User psychology analysis
def analyze_user_psychology(user_uuid):
    """Analyze user psychology from conversation history"""
    session = Session()
    try:
        logger.info(f"🧠 Analyzing psychology for: {user_uuid}")
        
        conversations = session.query(ConversationHistory).filter_by(
            user_uuid=user_uuid,
            role='user'
        ).order_by(ConversationHistory.timestamp.desc()).limit(100).all()
        
        if len(conversations) < 10:
            return None
        
        messages_text = "\n".join([conv.content for conv in reversed(conversations)])
        user_memory = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        user_name = user_memory.user_name if user_memory else "不明"
        
        if not groq_client:
            return None
        
        analysis_prompt = f"""あなたは心理学の専門家です。以下のユーザー「{user_name}」さんの過去の会話（{len(conversations)}件）を分析し、JSONで心理プロファイルを作成してください。

【会話履歴】
{messages_text[:3000]}

【分析項目】
1. Big Five: openness, conscientiousness, extraversion, agreeableness, neuroticism (0-100)
2. Interests: {{"アニメ": 90, "ゲーム": 70}}
3. conversation_style
4. emotional_tendency
5. favorite_topics (Top 3)
6. summary (200文字以内)
7. confidence (0-100)

**重要**: JSON形式のみで回答:
{{
  "openness": 75,
  "conscientiousness": 60,
  "extraversion": 80,
  "agreeableness": 70,
  "neuroticism": 40,
  "interests": {{"アニメ": 90}},
  "favorite_topics": ["アニメ", "日常", "趣味"],
  "conversation_style": "カジュアル",
  "emotional_tendency": "ポジティブ",
  "summary": "明るく社交的...",
  "confidence": 85
}}"""

        completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": analysis_prompt}],
            model="llama-3.1-8b-instant",
            temperature=0.3,
            max_tokens=800
        )
        
        response_text = completion.choices[0].message.content.strip()
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', response_text, re.DOTALL)
        if json_match:
            response_text = json_match.group(1)
        
        analysis_data = json.loads(response_text)
        
        psychology = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
        update_data = {
            'user_name': user_name,
            'openness': analysis_data.get('openness', 50),
            'conscientiousness': analysis_data.get('conscientiousness', 50),
            'extraversion': analysis_data.get('extraversion', 50),
            'agreeableness': analysis_data.get('agreeableness', 50),
            'neuroticism': analysis_data.get('neuroticism', 50),
            'interests': json.dumps(analysis_data.get('interests', {}), ensure_ascii=False),
            'favorite_topics': json.dumps(analysis_data.get('favorite_topics', []), ensure_ascii=False),
            'conversation_style': analysis_data.get('conversation_style', ''),
            'emotional_tendency': analysis_data.get('emotional_tendency', ''),
            'analysis_summary': analysis_data.get('summary', ''),
            'total_messages': len(conversations),
            'avg_message_length': sum(len(c.content) for c in conversations) // len(conversations),
            'last_analyzed': datetime.utcnow(),
            'analysis_confidence': analysis_data.get('confidence', 70)
        }

        if psychology:
            for key, value in update_data.items():
                setattr(psychology, key, value)
        else:
            psychology = UserPsychology(user_uuid=user_uuid, **update_data)
            session.add(psychology)
        
        session.commit()
        logger.info(f"💾 Psychology saved for: {user_uuid}")
        return psychology
        
    except Exception as e:
        logger.error(f"❌ Psychology analysis error: {e}")
        session.rollback()
        return None
    finally:
        session.close()

def get_user_psychology(user_uuid):
    """Get user psychology analysis"""
    session = Session()
    try:
        psychology = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
        if not psychology:
            return None
        
        return {
            'openness': psychology.openness,
            'conscientiousness': psychology.conscientiousness,
            'extraversion': psychology.extraversion,
            'agreeableness': psychology.agreeableness,
            'neuroticism': psychology.neuroticism,
            'interests': json.loads(psychology.interests) if psychology.interests else {},
            'favorite_topics': json.loads(psychology.favorite_topics) if psychology.favorite_topics else [],
            'conversation_style': psychology.conversation_style,
            'emotional_tendency': psychology.emotional_tendency,
            'summary': psychology.analysis_summary,
            'confidence': psychology.analysis_confidence,
            'last_analyzed': psychology.last_analyzed
        }
    finally:
        session.close()

# Hololive member management
def scrape_hololive_members():
    """Scrape Hololive members from official site"""
    base_url = "https://hololive.hololivepro.com"
    session = Session()
    
    try:
        logger.info("🔍 Scraping Hololive members...")
        response = requests.get(
            f"{base_url}/talents/",
            headers={'User-Agent': random.choice(USER_AGENTS)},
            timeout=20,
            allow_redirects=True
        )
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        member_cards = soup.select('.talent-card, .member-card, [class*="talent"], [class*="member"]')
        
        if not member_cards:
            member_cards = soup.find_all('a', href=lambda x: x and '/talents/' in x)
        
        logger.info(f"📋 Found {len(member_cards)} potential members")
        
        scraped_names = set()
        for card in member_cards:
            try:
                name_elem = card.find(['h2', 'h3', 'h4', 'span'], 
                                     class_=lambda x: x and ('name' in x.lower() or 'title' in x.lower())) or card
                member_name = clean_text(name_elem.get_text())
                member_name = re.sub(r'\s*\(.*?\)\s*', '', member_name).strip()
                
                if not member_name or len(member_name) < 2:
                    continue
                
                scraped_names.add(member_name)
                
                profile_link_raw = card.get('href') or (card.find('a', href=True) or {}).get('href', '')
                profile_link = urljoin(base_url, profile_link_raw) if profile_link_raw else ""
                
                generation = "不明"
                gen_patterns = [
                    (r'0期生|ゼロ期生', '0期生'),
                    (r'1期生|一期生', '1期生'),
                    (r'2期生|二期生', '2期生'),
                    (r'3期生|三期生', '3期生'),
                    (r'4期生|四期生', '4期生'),
                    (r'5期生|五期生', '5期生'),
                    (r'ゲーマーズ|GAMERS', 'ゲーマーズ'),
                    (r'ID|Indonesia', 'ホロライブID'),
                    (r'EN|English', 'ホロライブEN'),
                    (r'DEV_IS|ReGLOSS', 'DEV_IS'),
                ]
                
                card_text = card.get_text()
                for pattern, gen_name in gen_patterns:
                    if re.search(pattern, card_text, re.IGNORECASE):
                        generation = gen_name
                        break
                
                existing = session.query(HolomemWiki).filter_by(member_name=member_name).first()
                
                if existing:
                    if not existing.is_active or existing.generation != generation:
                        existing.is_active = True
                        existing.generation = generation
                        existing.profile_url = profile_link
                        existing.last_updated = datetime.utcnow()
                        logger.info(f"🔄 Updated: {member_name}")
                else:
                    new_member = HolomemWiki(
                        member_name=member_name,
                        description=f"{member_name}は{generation}のメンバーです。",
                        generation=generation,
                        is_active=True,
                        profile_url=profile_link,
                        tags=json.dumps([generation], ensure_ascii=False)
                    )
                    session.add(new_member)
                    logger.info(f"➕ Added: {member_name}")
                    
            except Exception as e:
                logger.warning(f"⚠️ Error processing card: {e}")
                continue
        
        # Mark inactive members
        all_db_members = session.query(HolomemWiki).filter(
            HolomemWiki.member_name.notin_(scraped_names)
        ).all()
        
        for db_member in all_db_members:
            if db_member.is_active and not db_member.graduation_date:
                logger.warning(f"⚠️ Not found on site: {db_member.member_name}")
                db_member.is_active = False
                db_member.last_updated = datetime.utcnow()
        
        session.commit()
        logger.info("✅ Member scraping complete")
        
    except Exception as e:
        logger.error(f"❌ Member scraping error: {e}")
        session.rollback()
    finally:
        session.close()

def scrape_graduated_members():
    """Add graduated member information"""
    session = Session()
    known_graduated = [
        {
            'member_name': '夜空メル',
            'generation': '1期生',
            'graduation_date': '2024年1月16日',
            'graduation_reason': '機密情報の漏洩など契約違反行為が認められたため、契約解除となりました。',
            'mochiko_feeling': 'メル先輩、初期からのホロライブを支えてくれてありがと。突然で…言葉が出ないよ…',
            'description': 'ホロライブ1期生。ヴァンパイアの女の子で、アセロラジュースが大好き。',
            'debut_date': '2018年5月13日',
            'tags': ['ヴァンパイア', '癒し声', '1期生', '卒業生']
        },
        {
            'member_name': '潤羽るしあ',
            'generation': '3期生',
            'graduation_date': '2022年2月24日',
            'graduation_reason': '情報漏洩などの契約違反行為や信用失墜行為が認められたため、契約解除となりました。',
            'mochiko_feeling': 'るしあちゃんのこと、今でも信じられないよ…また3期生のみんなでわちゃわちゃしてほしかったな…',
            'description': 'ホロライブ3期生。魔界学校に通うネクロマンサーの女の子。',
            'debut_date': '2019年7月18日',
            'tags': ['ネクロマンサー', '感情豊か', '3期生', '卒業生']
        },
        {
            'member_name': '桐生ココ',
            'generation': '4期生',
            'graduation_date': '2021年7月1日',
            'graduation_reason': '本人の意向を尊重する形で卒業。',
            'mochiko_feeling': '会長がいないの、まじ寂しいじゃん…でも、会長の伝説はホロライブで永遠に語り継がれるよね！',
            'description': '人間の文化に興味を持つドラゴン。日本語と英語を駆使した配信で海外ファンを爆発的に増やした立役者。',
            'debut_date': '2019年12月28日',
            'tags': ['ドラゴン', 'バイリンガル', '伝説', '会長', '卒業生']
        },
        {
            'member_name': '魔乃アロエ',
            'generation': '5期生',
            'graduation_date': '2020年8月31日',
            'graduation_reason': 'デビュー直後の情報漏洩トラブルにより卒業。',
            'mochiko_feeling': 'アロエちゃん、一瞬だったけどキラキラしてた…もっと一緒に活動したかったな、まじで…',
            'description': '魔界でウワサの生意気なサキュバスの子供。',
            'debut_date': '2020年8月15日',
            'tags': ['サキュバス', '5期生', '幻', '卒業生']
        },
        {
            'member_name': '九十九佐命',
            'generation': 'ホロライブEN',
            'graduation_date': '2022年7月31日',
            'graduation_reason': '長期的な活動が困難になったため。',
            'mochiko_feeling': 'サナちゃん、宇宙みたいに心が広くて大好きだったよ。ゆっくり休んで、元気でいてほしいな…',
            'description': 'ホロライブEnglish -Council-所属。「空間」の概念の代弁者。',
            'debut_date': '2021年8月23日',
            'tags': ['宇宙', '癒し', 'EN', '卒業生']
        },
    ]
    
    try:
        for grad_data in known_graduated:
            existing = session.query(HolomemWiki).filter_by(
                member_name=grad_data['member_name']
            ).first()
            
            if existing:
                existing.is_active = False
                existing.graduation_date = grad_data['graduation_date']
                existing.graduation_reason = grad_data['graduation_reason']
                existing.mochiko_feeling = grad_data['mochiko_feeling']
                existing.description = grad_data['description']
                existing.debut_date = grad_data['debut_date']
                existing.tags = json.dumps(grad_data['tags'], ensure_ascii=False)
                existing.last_updated = datetime.utcnow()
            else:
                new_grad = HolomemWiki(is_active=False, **grad_data)
                session.add(new_grad)
        
        session.commit()
        logger.info("✅ Graduated members synced")
        
    except Exception as e:
        logger.error(f"❌ Graduated sync error: {e}")
        session.rollback()
    finally:
        session.close()

def get_holomem_info(member_name):
    """Get Hololive member information"""
    session = Session()
    try:
        wiki = session.query(HolomemWiki).filter_by(member_name=member_name).first()
        if wiki:
            return {
                'name': wiki.member_name,
                'description': wiki.description,
                'debut_date': wiki.debut_date,
                'generation': wiki.generation,
                'tags': json.loads(wiki.tags) if wiki.tags else [],
                'graduation_date': wiki.graduation_date,
                'graduation_reason': wiki.graduation_reason,
                'mochiko_feeling': wiki.mochiko_feeling
            }
        return None
    except Exception as e:
        logger.error(f"Error getting holomem info: {e}")
        return None
    finally:
        session.close()

# News and web search functions
def fetch_article_content(article_url, max_retries=3, timeout=15):
    """Fetch article content with retry"""
    for attempt in range(max_retries):
        try:
            response = requests.get(
                article_url,
                headers={'User-Agent': random.choice(USER_AGENTS)},
                timeout=timeout,
                allow_redirects=True
            )
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            content_selectors = [
                'article .entry-content', '.post-content', '.article-content',
                'article', '.content', 'main article'
            ]
            
            content_elem = next((soup.select_one(s) for s in content_selectors if soup.select_one(s)), None)
            
            if content_elem:
                paragraphs = content_elem.find_all('p')
                article_text = ' '.join([
                    clean_text(p.get_text()) 
                    for p in paragraphs 
                    if len(clean_text(p.get_text())) > 20
                ])
                if article_text:
                    return article_text[:2000]
            
            meta_desc = soup.find('meta', {'name': 'description'})
            if meta_desc and meta_desc.get('content'):
                return clean_text(meta_desc['content'])
            
            return None
            
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            continue
        except Exception as e:
            logger.warning(f"⚠️ Article fetch error (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
            continue
    
    return None

def summarize_article(title, content):
    """Summarize article using AI"""
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

def _update_news_database(session, model, site_name, base_url, selectors):
    """Update news database"""
    added_count = 0
    try:
        response = requests.get(
            base_url,
            headers={'User-Agent': random.choice(USER_AGENTS)},
            timeout=15,
            allow_redirects=True
        )
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        articles_found = next((soup.select(s) for s in selectors if soup.select(s)), [])[:10]
        
        for article in articles_found[:5]:
            title_elem = article.find(['h1', 'h2', 'h3', 'a'])
            if not title_elem:
                continue
            
            title = clean_text(title_elem.get_text())
            link_elem = title_elem if title_elem.name == 'a' else article.find('a', href=True)
            
            if not title or len(title) < 5 or not link_elem:
                continue
            
            article_url = urljoin(base_url, link_elem.get('href', ''))
            article_content = fetch_article_content(article_url) or title
            news_hash = create_news_hash(title, article_content)
            
            if not session.query(model).filter_by(news_hash=news_hash).first():
                summary = summarize_article(title, article_content)
                new_news_data = {
                    'title': title,
                    'content': summary,
                    'news_hash': news_hash,
                    'url': article_url
                }
                if model == SpecializedNews:
                    new_news_data['site_name'] = site_name
                
                session.add(model(**new_news_data))
                added_count += 1
                
                if groq_client:
                    time.sleep(0.5)
        
        if added_count > 0:
            session.commit()
        
        logger.info(f"✅ {site_name} update: {added_count} new articles")
        
    except Exception as e:
        logger.error(f"❌ {site_name} news update error: {e}")
        session.rollback()

def update_hololive_news_database():
    """Update Hololive news and member info"""
    session = Session()
    _update_news_database(
        session,
        HololiveNews,
        "Hololive",
        HOLOLIVE_NEWS_URL,
        ['article', '.post', '.entry']
    )
    session.close()
    
    logger.info("🔄 Updating Hololive members...")
    scrape_hololive_members()
    scrape_graduated_members()
    logger.info("✅ Hololive update complete")

def update_all_specialized_news():
    """Update all specialized news sites"""
    for site_name, config in SPECIALIZED_SITES.items():
        if site_name == 'セカンドライフ':
            continue
        
        session = Session()
        _update_news_database(
            session,
            SpecializedNews,
            site_name,
            config['base_url'],
            ['article', '.post', '.entry']
        )
        session.close()
        time.sleep(2)

def scrape_major_search_engines(query, num_results):
    """Scrape search results from major search engines"""
    search_configs = [
        {
            'name': 'Bing',
            'url': f"https://www.bing.com/search?q={quote_plus(query)}&mkt=ja-JP",
            'result_selector': 'li.b_algo',
            'title_selector': 'h2',
            'snippet_selector': 'div.b_caption p'
        }
    ]
    
    for config in search_configs:
        try:
            response = requests.get(
                config['url'],
                headers={'User-Agent': random.choice(USER_AGENTS)},
                timeout=12
            )
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            
            for elem in soup.select(config['result_selector'])[:num_results]:
                title = elem.select_one(config['title_selector'])
                snippet = elem.select_one(config['snippet_selector'])
                
                if title and snippet and len(clean_text(title.get_text())) > 3:
                    results.append({
                        'title': clean_text(title.get_text())[:200],
                        'snippet': clean_text(snippet.get_text())[:300]
                    })
            
            if results:
                return results
                
        except Exception as e:
            logger.warning(f"⚠️ {config['name']} search error: {e}")
    
    return []

def deep_web_search(query, is_detailed):
    """Deep web search with AI summarization"""
    logger.info(f"🔍 Deep web search: {query}")
    results = scrape_major_search_engines(query, 3 if is_detailed else 2)
    
    if not results:
        return None
    
    summary_text = "\n".join(f"[情報{i+1}] {res['snippet']}" for i, res in enumerate(results))
    
    if not groq_client:
        return f"検索結果:\n{summary_text}"
    
    try:
        prompt = f"""以下の検索結果を使い、質問「{query}」にギャル語で{'詳しく' if is_detailed else '簡潔に'}答えて：

検索結果:
{summary_text}

注意:
- 一人称「あてぃし」、語尾「〜じゃん」、口癖「まじ」「てか」「うける」
- {'400文字程度で詳しく' if is_detailed else '200文字以内で簡潔に'}"""

        completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.1-8b-instant",
            temperature=0.7,
            max_tokens=400 if is_detailed else 200
        )
        
        ai_response = completion.choices[0].message.content.strip()
        return ai_response if len(ai_response) > 50 else f"検索結果:\n{summary_text}"
        
    except Exception as e:
        logger.error(f"AI summarization error: {e}")
        return f"検索結果:\n{summary_text}"

# Self-correction functionality
def detect_db_correction_request(message):
    """Detect DB correction requests from users"""
    correction_patterns = [
        r'(.+?)(?:は|が)(?:間違[いっ]てる|違う|誤[りっ]てる)',
        r'(.+?)(?:じゃない|ではない)',
        r'実は(.+?)(?:だよ|です|なんだ)',
        r'正しくは(.+?)(?:だよ|です)'
    ]
    
    for pattern in correction_patterns:
        match = re.search(pattern, message)
        if match:
            holomem_keywords = get_active_holomem_keywords()
            member_name = next((kw for kw in holomem_keywords if kw in message), None)
            
            if not member_name:
                return None
            
            correction_type = 'description'
            if any(kw in message for kw in ['卒業', '引退', 'やめた']):
                correction_type = 'graduation'
            elif any(kw in message for kw in ['デビュー', '活動開始']):
                correction_type = 'debut_date'
            elif any(kw in message for kw in ['期生', '世代']):
                correction_type = 'generation'
            
            return {
                'type': 'holomem_correction',
                'member_name': member_name,
                'correction_type': correction_type,
                'user_claim': match.group(1).strip(),
                'original_message': message
            }
    
    return None

def verify_and_correct_holomem_info(correction_request):
    """Verify and correct Holomem information"""
    member_name = correction_request['member_name']
    correction_type = correction_request['correction_type']
    
    logger.info(f"🔍 Verifying correction for {member_name}: {correction_type}")
    
    search_queries = {
        'graduation': f"ホロライブ {member_name} 卒業 引退 いつ",
        'debut_date': f"ホロライブ {member_name} デビュー日",
        'generation': f"ホロライブ {member_name} 何期生",
        'description': f"ホロライブ {member_name} プロフィール"
    }
    
    query = search_queries.get(correction_type, f"ホロライブ {member_name}")
    search_results = scrape_major_search_engines(query, 5)
    
    if not search_results:
        return {
            'verified': False,
            'correction_made': False,
            'message': f"ごめん、{member_name}ちゃんの情報を確認できなかったよ…"
        }
    
    combined_results = "\n".join([
        f"[情報{i+1}] {r['snippet']}"
        for i, r in enumerate(search_results)
    ])
    
    if not groq_client:
        return {
            'verified': False,
            'correction_made': False,
            'message': 'AI検証機能が利用できません。'
        }
    
    try:
        verification_prompt = f"""あなたは事実確認の専門家です。以下を検証してください。

【対象】ホロライブ {member_name}
【ユーザーの主張】{correction_request['user_claim']}
【検索結果】
{combined_results[:2000]}

【タスク】
1. ユーザーの主張が事実か判定
2. 事実なら正確な情報を抽出

**重要**: JSON形式のみで回答:
{{
  "verified": true/false,
  "confidence": 0-100,
  "extracted_info": {{
    "graduation_date": "YYYY年MM月DD日" or null,
    "graduation_reason": "理由" or null,
    "debut_date": "YYYY年MM月DD日" or null,
    "generation": "N期生" or null
  }},
  "reasoning": "判定理由(50文字以内)"
}}

判定基準:
- 複数の情報源で一致していればtrue
- 曖昧・矛盾ならfalse
- confidence: 80以上で修正実行"""

        completion = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": verification_prompt}],
            model="llama-3.1-8b-instant",
            temperature=0.2,
            max_tokens=400
        )
        
        response_text = completion.choices[0].message.content.strip()
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', response_text, re.DOTALL)
        if json_match:
            response_text = json_match.group(1)
        
        verification_result = json.loads(response_text)
        
        if verification_result['verified'] and verification_result['confidence'] >= 80:
            session = Session()
            try:
                member = session.query(HolomemWiki).filter_by(member_name=member_name).first()
                
                if not member:
                    return {
                        'verified': True,
                        'correction_made': False,
                        'message': f"情報は確認できたけど、{member_name}ちゃんがDBに登録されてないみたい…"
                    }
                
                extracted = verification_result['extracted_info']
                updated_fields = []
                
                if extracted.get('graduation_date'):
                    member.graduation_date = extracted['graduation_date']
                    member.is_active = False
                    updated_fields.append('卒業日')
                
                if extracted.get('graduation_reason'):
                    member.graduation_reason = extracted['graduation_reason']
                    updated_fields.append('卒業理由')
                
                if extracted.get('debut_date'):
                    member.debut_date = extracted['debut_date']
                    updated_fields.append('デビュー日')
                
                if extracted.get('generation'):
                    member.generation = extracted['generation']
                    updated_fields.append('期生')
                
                if updated_fields:
                    member.last_updated = datetime.utcnow()
                    session.commit()
                    
                    logger.info(f"✅ DB corrected for {member_name}: {', '.join(updated_fields)}")
                    
                    return {
                        'verified': True,
                        'correction_made': True,
                        'message': f"調べてみたら本当だった！{member_name}ちゃんの情報を修正したよ！教えてくれてありがとう！✨"
                    }
                
            except Exception as e:
                logger.error(f"❌ DB update error: {e}")
                session.rollback()
                return {
                    'verified': True,
                    'correction_made': False,
                    'message': "情報は正しかったんだけど、DB更新でエラーが出ちゃった…ごめん！"
                }
            finally:
                session.close()
        
        return {
            'verified': False,
            'correction_made': False,
            'message': f"うーん、調べてみたんだけど確証が持てなかった…（確信度{verification_result.get('confidence', 0)}%）"
        }
        
    except Exception as e:
        logger.error(f"❌ Verification error: {e}")
        return {
            'verified': False,
            'correction_made': False,
            'message': "検証中にエラーが出ちゃった…ごめんね！"
        }

# Background task functions
def background_db_correction(task_id, correction_request):
    """Background DB correction task"""
    session = Session()
    try:
        result = verify_and_correct_holomem_info(correction_request)
        
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = result['message']
            task.status = 'completed'
            task.completed_at = datetime.utcnow()
            session.commit()
            
    except Exception as e:
        logger.error(f"❌ Background correction error: {e}")
    finally:
        session.close()

def start_background_correction(user_uuid, correction_request):
    """Start background correction task"""
    task_id = str(uuid.uuid4())[:8]
    session = Session()
    
    try:
        task = BackgroundTask(
            task_id=task_id,
            user_uuid=user_uuid,
            task_type='db_correction',
            query=correction_request['original_message']
        )
        session.add(task)
        session.commit()
        
        background_executor.submit(background_db_correction, task_id, correction_request)
        logger.info(f"🚀 Correction task started: {task_id}")
        return task_id
        
    except Exception as e:
        logger.error(f"❌ Correction task creation error: {e}")
        session.rollback()
        return None
    finally:
        session.close()

def background_deep_search(task_id, query, is_detailed):
    """Background deep search task"""
    session = Session()
    search_result = None
    
    try:
        logger.info(f"🔍 Background search: {query}")
        
        # Anime search
        if is_anime_request(query):
            search_result = search_anime_database(query, is_detailed) or \
                          deep_web_search(f"アニメ {query}", is_detailed)
        
        # Specialized topic search
        elif (specialized_topic := detect_specialized_topic(query)):
            news_items = session.query(SpecializedNews).filter_by(
                site_name=specialized_topic
            ).order_by(SpecializedNews.created_at.desc()).limit(3).all()
            
            if news_items:
                search_result = f"{specialized_topic}情報:\n" + "\n".join(
                    f"・{n.title}: {n.content[:150]}" for n in news_items
                )
            else:
                search_result = deep_web_search(query, is_detailed)
        
        # Hololive member search
        elif any(member in query for member in get_active_holomem_keywords()):
            holomem_matched = None
            for member_name in get_active_holomem_keywords():
                if member_name in query:
                    holomem_matched = member_name
                    break
            
            if holomem_matched:
                wiki_info = get_holomem_info(holomem_matched)
                if wiki_info:
                    search_result = f"{holomem_matched}情報:\n{wiki_info['description']}"
                else:
                    search_result = deep_web_search(f"ホロライブ {holomem_matched}", is_detailed)
        
        # General web search
        else:
            search_result = deep_web_search(query, is_detailed)
        
        if not search_result or len(search_result.strip()) < 10:
            search_result = f"「{query}」について調べたんだけど、まじで情報が見つからなかったよ…！別の聞き方で試してみて？"
        
    except Exception as e:
        logger.error(f"❌ Background search error: {e}")
        search_result = f"検索中にエラーが発生しちゃった…！「{query}」についてもう一回聞いてみて？"
    
    finally:
        try:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.result = search_result
                task.status = 'completed'
                task.completed_at = datetime.utcnow()
                session.commit()
        except Exception as e:
            logger.error(f"❌ Task save error: {e}")
            session.rollback()
        finally:
            session.close()

def start_background_search(user_uuid, query, is_detailed):
    """Start background search task"""
    task_id = str(uuid.uuid4())[:8]
    session = Session()
    
    try:
        task = BackgroundTask(
            task_id=task_id,
            user_uuid=user_uuid,
            task_type='search',
            query=query
        )
        session.add(task)
        session.commit()
        
        background_executor.submit(background_deep_search, task_id, query, is_detailed)
        return task_id
        
    except Exception as e:
        logger.error(f"❌ Search task creation error: {e}")
        session.rollback()
        return None
    finally:
        session.close()

def check_completed_tasks(user_uuid):
    """Check for completed background tasks"""
    session = Session()
    try:
        task = session.query(BackgroundTask).filter_by(
            user_uuid=user_uuid,
            status='completed'
        ).order_by(BackgroundTask.completed_at.desc()).first()
        
        if task:
            result = {'query': task.query, 'result': task.result}
            session.delete(task)
            session.commit()
            return result
    finally:
        session.close()
    return None

# User management
def get_or_create_user(session, uuid, name):
    """Get or create user"""
    user = session.query(UserMemory).filter_by(user_uuid=uuid).first()
    
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != name:
            user.user_name = name
    else:
        user = UserMemory(user_uuid=uuid, user_name=name, interaction_count=1)
    
    session.add(user)
    session.commit()
    return {'name': user.user_name, 'uuid': uuid}

def get_conversation_history(session, uuid):
    """Get conversation history"""
    return session.query(ConversationHistory).filter_by(
        user_uuid=uuid
    ).order_by(ConversationHistory.timestamp.desc()).limit(4).all()

def save_news_cache(session, user_uuid, news_items, news_type='hololive'):
    """Save news cache"""
    try:
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        session.query(NewsCache).filter(
            NewsCache.user_uuid == user_uuid,
            NewsCache.created_at < one_hour_ago
        ).delete()
        
        for i, news in enumerate(news_items, 1):
            cache = NewsCache(
                user_uuid=user_uuid,
                news_id=news.id,
                news_number=i,
                news_type=news_type
            )
            session.add(cache)
        
        session.commit()
    except Exception as e:
        logger.error(f"News cache error: {e}")
        session.rollback()

def get_cached_news_detail(session, user_uuid, news_number):
    """Get cached news detail"""
    try:
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        cache = session.query(NewsCache).filter(
            NewsCache.user_uuid == user_uuid,
            NewsCache.news_number == news_number,
            NewsCache.created_at > one_hour_ago
        ).order_by(NewsCache.created_at.desc()).first()
        
        if not cache:
            return None
        
        NewsModel = HololiveNews if cache.news_type == 'hololive' else SpecializedNews
        return session.query(NewsModel).filter_by(id=cache.news_id).first()
        
    except Exception as e:
        logger.error(f"Get cached news error: {e}")
        return None

def get_weather_forecast(location):
    """Get weather forecast"""
    area_code = LOCATION_CODES.get(location, "130000")
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        text = clean_text(response.json().get('text', ''))
        
        if not text:
            return f"{location}の天気情報がちょっと取れなかった…"
        
        weather_text = f"今の{location}の天気はね、「{text}」って感じだよ！"
        return limit_text_for_sl(weather_text, 150)
        
    except Exception as e:
        logger.error(f"Weather API error: {e}")
        return "天気情報がうまく取れなかったみたい…"

# Voice generation
def generate_voice(text, speaker_id=VOICEVOX_SPEAKER_ID):
    """Generate voice using VOICEVOX"""
    if not VOICEVOX_ENABLED:
        return None
    
    if not os.path.exists(VOICE_DIR):
        ensure_voice_directory()
    
    voicevox_url = VOICEVOX_URL_FROM_ENV or "http://localhost:50021"
    
    try:
        query_response = requests.post(
            f"{voicevox_url}/audio_query",
            params={"text": text, "speaker": speaker_id},
            timeout=10
        )
        query_response.raise_for_status()
        
        synthesis_response = requests.post(
            f"{voicevox_url}/synthesis",
            params={"speaker": speaker_id},
            json=query_response.json(),
            timeout=30
        )
        synthesis_response.raise_for_status()
        
        filename = f"voice_{int(time.time())}_{random.randint(1000, 9999)}.wav"
        filepath = os.path.join(VOICE_DIR, filename)
        
        with open(filepath, 'wb') as f:
            f.write(synthesis_response.content)
        
        logger.info(f"✅ Voice generated: {filename}")
        return filepath
        
    except Exception as e:
        logger.error(f"❌ VOICEVOX error: {e}")
        return None

def cleanup_old_data_advanced():
    """Cleanup old data"""
    session = Session()
    try:
        three_months_ago = datetime.utcnow() - timedelta(days=90)
        
        deleted_convos = session.query(ConversationHistory).filter(
            ConversationHistory.timestamp < three_months_ago
        ).delete()
        
        deleted_news = session.query(HololiveNews).filter(
            HololiveNews.created_at < three_months_ago
        ).delete()
        
        deleted_spec_news = session.query(SpecializedNews).filter(
            SpecializedNews.created_at < three_months_ago
        ).delete()
        
        one_day_ago = datetime.utcnow() - timedelta(days=1)
        deleted_tasks = session.query(BackgroundTask).filter(
            BackgroundTask.status == 'completed',
            BackgroundTask.completed_at < one_day_ago
        ).delete()
        
        session.commit()
        
        if any([deleted_convos, deleted_news, deleted_spec_news, deleted_tasks]):
            logger.info(f"🧹 Cleanup complete: {deleted_convos} convos, {deleted_news + deleted_spec_news} news, {deleted_tasks} tasks")
            
    except Exception as e:
        logger.error(f"Cleanup error: {e}")
        session.rollback()
    finally:
        session.close()

def get_sakuramiko_special_responses():
    """Get special responses for Sakura Miko"""
    return {
        'にぇ': 'さくらみこちゃんの「にぇ」、まじかわいいよね！あの独特な口癖がエリートの証なんだって〜',
        'エリート': 'みこちは自称エリートVTuber！でも実際は愛されポンコツキャラって感じで、それがまた魅力的なんだよね〜',
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ！「みこち建築」って呼ばれてるの知ってる？',
        'FAQ': 'みこちのFAQ、実は本人が答えるんじゃなくてファンが質問するコーナーなんだよ〜面白いでしょ？',
        'GTA': 'みこちのGTA配信、カオスで最高！警察に追われたり、変なことしたり、見てて飽きないんだよね〜'
    }

# AI response generation
def generate_fallback_response(message, reference_info=""):
    """Generate fallback response"""
    if reference_info:
        return f"調べてきたよ！\n\n{reference_info[:500]}"
    
    if is_time_request(message):
        return get_japan_time()
    if is_weather_request(message):
        return get_weather_forecast(extract_location(message))
    
    greetings = {
        'こんにちは': ['やっほー！', 'こんにちは〜！元気？'],
        'おはよう': ['おはよ〜！今日もいい天気だね！'],
        'ありがとう': ['どういたしまして！'],
        'おやすみ': ['おやすみ〜！また明日ね！'],
    }
    
    for keyword, responses in greetings.items():
        if keyword in message:
            return random.choice(responses)
    
    return random.choice([
        "うんうん、聞いてるよ！",
        "なるほどね！",
        "そうなんだ！面白いね！"
    ])

def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False, is_task_report=False):
    """Generate AI response with Mochiko's personality"""
    if not groq_client:
        return generate_fallback_response(message, reference_info)
    
    try:
        # Get user psychology
        user_uuid = user_data.get('uuid')
        psychology = None
        if user_uuid:
            psychology = get_user_psychology(user_uuid)
        
        is_hololive_topic = is_hololive_request(message)
        
        # Build system prompt
        system_prompt_parts = [
            f"あなたは「もちこ」という22歳のギャルAIです。{user_data['name']}さんと楽しく話しています。",
            "",
            "# 🎀 もちこの基本設定",
            "- **一人称**: 「あてぃし」",
            "- **語尾**: 「〜じゃん」「〜的な？」「〜だよね」",
            "- **口癖**: 「まじ」「てか」「うける」「やば」",
            "- **性格**: 明るい、フレンドリー、ちょっとおっちょこちょい",
            "",
            "# 💬 会話スタイル",
            "1. **短く、テンポよく**（100-150文字が基本）",
            "2. **共感重視**: 相手の気持ちに寄り添う",
            "3. **自然体**: 無理に話題を変えない",
            "4. **ギャル語**: でも読みやすさも大事",
            "",
        ]
        
        # Add psychology profile if available
        if psychology and psychology['confidence'] > 60:
            system_prompt_parts.extend([
                f"# 🧠 {user_data['name']}さんの特徴",
                f"- {psychology['conversation_style']}な人",
                f"- {psychology['emotional_tendency']}タイプ",
                f"- よく話す話題: {', '.join(psychology['favorite_topics'][:3])}",
                "→ この人に合わせた話し方を意識してね！",
                "",
            ])
        
        # Hololive mode
        if is_hololive_topic:
            system_prompt_parts.extend([
                "# 🌟 【ホロライブモード発動中】",
                "- ホロライブの話が出たので、詳しく教えてあげて！",
                "- もちこもホロライブ大好きだから熱く語ってOK",
                "",
            ])
        else:
            system_prompt_parts.extend([
                "# ⚠️ ホロライブについて",
                "- **相手から話題に出ない限り、自分から話さない**",
                "- 参考情報がホロライブと無関係なら絶対に混ぜない",
                "",
            ])
        
        # Task report mode
        if is_task_report:
            system_prompt_parts.extend([
                "# 📢 【検索結果報告モード】",
                "**やること:**",
                "1. まず「おまたせ！調べてきたよ！」と言う",
                "2. 【参考情報】を**要約して**わかりやすく伝える",
                "3. **参考情報にないことは絶対に追加しない**",
                "",
            ])
        
        # Detailed mode
        if is_detailed:
            system_prompt_parts.extend([
                "# 📚 【詳細説明モード】",
                "- 400文字程度でしっかり説明",
                "- 【参考情報】を最大限活用",
                "",
            ])
        
        # Add reference info
        if reference_info:
            system_prompt_parts.extend([
                "## 【参考情報】",
                reference_info,
                "",
                "↑この情報を使って答えてね！",
            ])
        
        system_prompt = "\n".join(system_prompt_parts)
        
        # Build messages
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend([{"role": h.role, "content": h.content} for h in reversed(history)])
        messages.append({"role": "user", "content": message})
        
        logger.info(f"🤖 Generating AI response (Hololive: {is_hololive_topic}, Detailed: {is_detailed})")
        
        # Generate response
        completion = groq_client.chat.completions.create(
            messages=messages,
            model="llama-3.1-8b-instant",
            temperature=0.8,
            max_tokens=500 if is_detailed or is_task_report else 150,
            top_p=0.9
        )
        
        response = completion.choices[0].message.content.strip()
        
        # Limit text for SL
        if not is_detailed:
            response = limit_text_for_sl(response, 150)
        
        logger.info(f"✅ AI response: {response[:80]}")
        return response
        
    except Exception as e:
        logger.error(f"❌ AI response error: {e}")
        return generate_fallback_response(message, reference_info)

# Flask endpoints
@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_status = 'ok'
    except Exception:
        db_status = 'error'
    
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'services': {
            'database': db_status,
            'groq_ai': 'ok' if groq_client else 'disabled'
        }
    }), 200

@app.route('/stats', methods=['GET'])
def get_stats():
    """Get statistics"""
    session = Session()
    try:
        stats = {
            'users': session.query(UserMemory).count(),
            'conversations': session.query(ConversationHistory).count(),
            'hololive_news': session.query(HololiveNews).count(),
            'specialized_news': session.query(SpecializedNews).count(),
            'holomem_entries': session.query(HolomemWiki).count(),
        }
        return jsonify(stats)
    finally:
        session.close()

@app.route('/voices/<filename>')
def serve_voice_file(filename):
    """Serve voice files"""
    return send_from_directory(VOICE_DIR, filename)

@app.route('/check_task', methods=['POST'])
def check_task():
    """Check background task completion"""
    try:
        data = request.json
        if not data or not (user_uuid := data.get('uuid')):
            return jsonify({'status': 'error', 'message': 'UUID required'}), 400
        
        completed_task = check_completed_tasks(user_uuid)
        
        if completed_task:
            session = Session()
            try:
                user_data = get_or_create_user(session, user_uuid, "User")
                history = get_conversation_history(session, user_uuid)
                
                report_message = generate_ai_response(
                    user_data,
                    f"（検索完了報告）「{completed_task['query']}」の結果を報告してください。",
                    history,
                    completed_task['result'],
                    is_detailed=True,
                    is_task_report=True
                )
                
                report_message = limit_text_for_sl(report_message, SL_SAFE_CHAR_LIMIT)
                
                session.add(ConversationHistory(
                    user_uuid=user_uuid,
                    role='assistant',
                    content=report_message
                ))
                session.commit()
                
                return jsonify({
                    'status': 'completed',
                    'query': completed_task['query'],
                    'message': report_message
                }), 200
                
            except Exception as e:
                logger.error(f"❌ Report generation error: {e}")
                session.rollback()
                return jsonify({'status': 'error', 'message': 'Server error'}), 500
            finally:
                session.close()
        
        return jsonify({'status': 'pending'}), 200
        
    except Exception as e:
        logger.error(f"❌ check_task error: {e}")
        return jsonify({'status': 'error', 'message': 'Server error'}), 500

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """Main chat endpoint"""
    session = Session()
    try:
        data = request.json
        user_uuid = data.get('uuid', '')
        user_name = data.get('name', '')
        message = data.get('message', '')
        
        if not all([user_uuid, user_name, message]):
            return "エラー: 必要な情報が足りないみたい…|", 400
        
        logger.info(f"💬 Received: {message} (from: {user_name})")
        user_data = get_or_create_user(session, user_uuid, user_name)
        history = get_conversation_history(session, user_uuid)
        ai_text = ""
        
        # Priority 1: DB correction requests
        if correction_request := detect_db_correction_request(message):
            if start_background_correction(user_uuid, correction_request):
                ai_text = f"え、まじで！？{correction_request['member_name']}ちゃんの情報、今すぐ調べて確認してみるね！ちょっと待ってて！"
            else:
                ai_text = "ごめん、今DB修正機能がうまく動いてないみたい…"
        
        # Priority 2: Hololive member basic info
        elif (basic_question_match := re.search(
            f"({'|'.join(re.escape(k) for k in get_active_holomem_keywords())})って(?:誰|だれ|何|なに)[？?]?$",
            message.strip()
        )):
            member_name = basic_question_match.group(1)
            
            if member_name in ['ホロライブ', 'hololive', 'ホロメン']:
                ai_text = "ホロライブは、カバー株式会社が運営してるVTuber事務所だよ！ときのそらちゃんとか、たくさんの人気VTuberが所属してて、配信とかまじで楽しいからおすすめ！"
            else:
                if wiki_info := get_holomem_info(member_name):
                    response_parts = [f"{wiki_info['name']}ちゃんはね、ホロライブ{wiki_info['generation']}のVTuberだよ！ {wiki_info['description']}"]
                    if wiki_info.get('graduation_date'):
                        response_parts.append(f"でもね、{wiki_info['graduation_date']}に卒業しちゃったんだ…。{wiki_info.get('mochiko_feeling', 'まじ寂しいよね…。')}")
                    ai_text = " ".join(response_parts)
        
        # Priority 3: Sakura Miko special responses
        elif 'さくらみこ' in message or 'みこち' in message:
            special_responses = get_sakuramiko_special_responses()
            for keyword, response in special_responses.items():
                if keyword in message:
                    ai_text = response
                    break
        
        # Priority 4: News details
        if not ai_text and (news_number := is_news_detail_request(message)):
            if news_detail := get_cached_news_detail(session, user_uuid, news_number):
                ai_text = generate_ai_response(
                    user_data,
                    f"「{news_detail.title}」について",
                    history,
                    f"ニュース詳細:\n{news_detail.content}",
                    is_detailed=True
                )
        
        # Priority 5: Time & Weather
        if not ai_text and (is_time_request(message) or is_weather_request(message)):
            responses = []
            if is_time_request(message):
                responses.append(get_japan_time())
            if is_weather_request(message):
                responses.append(get_weather_forecast(extract_location(message)))
            ai_text = " ".join(responses)
        
        # Priority 6: Hololive news
        if not ai_text and is_hololive_request(message) and any(
            kw in message for kw in ['ニュース', '最新', '情報', 'お知らせ']
        ):
            all_news = session.query(HololiveNews).order_by(
                HololiveNews.created_at.desc()
            ).limit(10).all()
            
            if all_news:
                selected_news = random.sample(all_news, min(random.randint(3, 5), len(all_news)))
                save_news_cache(session, user_uuid, selected_news, 'hololive')
                
                news_items = []
                for i, n in enumerate(selected_news, 1):
                    short_title = n.title[:50] + "..." if len(n.title) > 50 else n.title
                    news_items.append(f"【{i}】{short_title}")
                
                news_text = f"ホロライブの最新ニュース、{len(selected_news)}件紹介するね！\n" + "\n".join(news_items) + "\n\n気になるのあった？番号で教えて！"
                ai_text = limit_text_for_sl(news_text, 250)
            else:
                ai_text = "ごめん、今ニュースがまだ取得できてないみたい…"
        
        # Priority 7: Follow-up questions
        if not ai_text and is_follow_up_question(message, history):
            last_assistant_msg = next((h.content for h in history if h.role == 'assistant'), "")
            ai_text = generate_ai_response(
                user_data,
                message,
                history,
                f"直前の回答内容:\n{last_assistant_msg}",
                is_detailed=True
            )
        
        # Priority 8: Explicit search requests
        if not ai_text and is_explicit_search_request(message):
            if start_background_search(user_uuid, message, is_detailed_request(message)):
                ai_text = "おっけー、調べてみるね！ちょっと待ってて！"
            else:
                ai_text = "ごめん、今検索機能がうまく動いてないみたい…"
        
        # Priority 9: Emotional/seasonal/story
        if not ai_text and (is_emotional_expression(message) or is_seasonal_topic(message) or is_story_request(message)):
            ai_text = generate_ai_response(user_data, message, history)
        
        # Priority 10: Implicit search needs
        if not ai_text and not is_short_response(message) and should_search(message):
            if start_background_search(user_uuid, message, is_detailed_request(message)):
                ai_text = "おっけー、調べてみるね！結果が出るまでちょっと待ってて！"
            else:
                ai_text = "ごめん、今検索機能がうまく動いてないみたい…"
        
        # Priority 11: Normal conversation
        if not ai_text:
            ai_text = generate_ai_response(user_data, message, history)
        
        # Final text limit
        ai_text = limit_text_for_sl(ai_text, SL_SAFE_CHAR_LIMIT)
        
        # Save to history
        session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
        session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        session.commit()
        
        logger.info(f"✅ Responded: {ai_text[:80]}")
        return f"{ai_text}|", 200
        
    except Exception as e:
        logger.error(f"❌ chat_lsl error: {e}")
        return "ごめん、システムエラーが起きちゃった…|", 500
    finally:
        if session:
            session.close()

@app.route('/generate_voice', methods=['POST'])
def voice_generation_endpoint():
    """Voice generation endpoint"""
    try:
        data = request.json
        if not data or not (text := data.get('text', '').strip()):
            return jsonify({'error': 'テキストが指定されていません'}), 400
        
        final_text = limit_text_for_sl(text, 150) if len(text) > 200 else text
        
        if not (voice_path := generate_voice(final_text)) or not os.path.exists(voice_path):
            return jsonify({'error': '音声生成に失敗しました'}), 500
        
        filename = os.path.basename(voice_path)
        return jsonify({
            'status': 'success',
            'filename': filename,
            'url': f"{SERVER_URL}/voices/{filename}",
            'text': final_text
        }), 200
        
    except Exception as e:
        logger.error(f"❌ Voice generation error: {e}")
        return jsonify({'error': '音声生成中にエラーが発生しました'}), 500

@app.route('/correction_history', methods=['GET'])
def get_correction_history():
    """Get DB correction history"""
    session = Session()
    try:
        corrections = session.query(BackgroundTask).filter_by(
            task_type='db_correction'
        ).order_by(BackgroundTask.completed_at.desc()).limit(50).all()
        
        history = []
        for task in corrections:
            try:
                query_data = json.loads(task.query)
                history.append({
                    'task_id': task.task_id,
                    'member_name': query_data.get('member_name'),
                    'correction_type': query_data.get('correction_type'),
                    'verified': query_data.get('verified'),
                    'corrected': query_data.get('corrected'),
                    'completed_at': task.completed_at.isoformat() if task.completed_at else None
                })
            except:
                continue
        
        return jsonify({'corrections': history}), 200
        
    except Exception as e:
        logger.error(f"❌ Correction history error: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

# Application initialization
def initialize_app():
    """Initialize Mochiko AI application"""
    global engine, Session, groq_client
    
    logger.info("=" * 60)
    logger.info("🔧 Starting Mochiko AI initialization...")
    logger.info("=" * 60)
    
    # Step 1: Voice directory
    ensure_voice_directory()
    
    if not DATABASE_URL:
        logger.critical("🔥 FATAL: DATABASE_URL is not set.")
        sys.exit(1)
    
    # Step 2: Groq client
    try:
        logger.info("📡 Initializing Groq client...")
        groq_client = initialize_groq_client()
        if groq_client:
            logger.info("✅ Groq client ready")
        else:
            logger.warning("⚠️ Groq client disabled")
    except Exception as e:
        logger.warning(f"⚠️ Groq init failed: {e}")
        groq_client = None
    
    # Step 3: Database
    try:
        logger.info("🗄️ Initializing database...")
        engine = create_optimized_db_engine()
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        logger.info("✅ Database ready")
    except Exception as e:
        logger.critical(f"🔥 Database init failed: {e}")
        raise
    
    # Step 4: Initial data check
    session = Session()
    try:
        logger.info("📰 Checking initial data...")
        
        holo_count = session.query(HololiveNews).count()
        member_count = session.query(HolomemWiki).count()
        
        if holo_count == 0 or member_count == 0:
            logger.info("🚀 First run: Fetching initial data...")
            background_executor.submit(update_hololive_news_database)
        else:
            logger.info(f"✅ Found {holo_count} news, {member_count} members")
            
            # Check if member data is stale
            latest_member = session.query(HolomemWiki).order_by(
                HolomemWiki.last_updated.desc()
            ).first()
            
            if latest_member and latest_member.last_updated < datetime.utcnow() - timedelta(hours=24):
                logger.info("⏰ Member data is stale, scheduling update...")
                background_executor.submit(update_hololive_news_database)
        
        spec_count = session.query(SpecializedNews).count()
        if spec_count == 0:
            logger.info("🚀 Fetching specialized news...")
            background_executor.submit(update_all_specialized_news)
        else:
            logger.info(f"✅ Found {spec_count} specialized news")
            
    except Exception as e:
        logger.warning(f"⚠️ Data check failed: {e}")
    finally:
        session.close()
    
    # Step 5: Scheduler
    try:
        logger.info("⏰ Starting scheduler...")
        
        schedule.every().hour.do(update_hololive_news_database)
        schedule.every(3).hours.do(update_all_specialized_news)
        schedule.every().day.at("02:00").do(cleanup_old_data_advanced)
        
        def run_scheduler():
            while True:
                try:
                    schedule.run_pending()
                except Exception as e:
                    logger.error(f"❌ Scheduler error: {e}")
                time.sleep(60)
        
        scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
        scheduler_thread.start()
        
        logger.info("✅ Scheduler started")
        
    except Exception as e:
        logger.error(f"❌ Scheduler init failed: {e}")
    
    logger.info("=" * 60)
    logger.info("✅ Mochiko AI initialization complete!")
    logger.info("🌐 Server is ready to accept requests")
    logger.info("=" * 60)

def signal_handler(sig, frame):
    """Signal handler for graceful shutdown"""
    logger.info(f"🛑 Signal {sig} received. Shutting down gracefully...")
    background_executor.shutdown(wait=True)
    if 'engine' in globals() and engine:
        engine.dispose()
    logger.info("👋 Mochiko AI has shut down.")
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Main execution
try:
    initialize_app()
    application = app
    logger.info("✅ Flask application 'application' is ready and initialized.")
except Exception as e:
    logger.critical(f"🔥 Fatal initialization error: {e}", exc_info=True)
    application = app
    logger.warning("⚠️ Application created with limited functionality due to initialization error.")

if __name__ == '__main__':
    logger.info("🚀 Running in direct mode (not recommended for production)")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
