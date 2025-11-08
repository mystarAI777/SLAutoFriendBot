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
from flask import Response
from cryptography.fernet import Fernet

try:
    from typing import Union, Dict, Any, List, Optional
except ImportError:
    Dict, Any, List, Union, Optional = dict, object, list, object, object

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean, text
from sqlalchemy.orm import declarative_base, sessionmaker
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
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
SL_SAFE_CHAR_LIMIT = 250
USER_AGENTS = ['Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36']
LOCATION_CODES = {"東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"}
SPECIALIZED_SITES = {
    'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー']},
    'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG', 'CG']},
    '脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学', '脳']},
    'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']},
    'アニメ': {'base_url': 'https://animedb.jp/', 'keywords': ['アニメ', 'anime']},
}
ANIME_KEYWORDS = [
    'アニメ', 'anime', 'アニメーション', '作画', '声優', 'OP', 'ED', '劇場版', '映画', 'OVA', '原作', '漫画', 'ラノベ', 'キャラクター', '制作会社', 'スタジオ'
]

# --- Global Variables & App Setup ---
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client, engine, Session = None, None, None
app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
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
    path = f"/etc/secrets/{name}"
    if os.path.exists(path):
        try:
            with open(path, 'r') as f: return f.read().strip()
        except IOError: return None
    return os.environ.get(name)

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./test.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
ADMIN_TOKEN = get_secret('ADMIN_TOKEN')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

def get_encryption_key():
    key = get_secret('BACKUP_ENCRYPTION_KEY')
    if not key or len(key.encode()) != 44:
        logger.critical("🔥 FATAL ERROR: BACKUP_ENCRYPTION_KEY is not set or invalid.")
        sys.exit(1)
    return key.encode()

# --- Database Models ---
class UserMemory(Base): __tablename__ = 'user_memories'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), unique=True, nullable=False); user_name = Column(String(255), nullable=False); interaction_count = Column(Integer, default=0); last_interaction = Column(DateTime, default=datetime.utcnow)
class ConversationHistory(Base): __tablename__ = 'conversation_history'; id = Column(Integer, primary_key=True, autoincrement=True); user_uuid = Column(String(255), nullable=False, index=True); role = Column(String(10), nullable=False); content = Column(Text, nullable=False); timestamp = Column(DateTime, default=datetime.utcnow, index=True)
class HololiveNews(Base): __tablename__ = 'hololive_news'; id = Column(Integer, primary_key=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True)
class BackgroundTask(Base): __tablename__ = 'background_tasks'; id = Column(Integer, primary_key=True); task_id = Column(String(255), unique=True, nullable=False); user_uuid = Column(String(255), nullable=False); task_type = Column(String(50), nullable=False); query = Column(Text, nullable=False); result = Column(Text); status = Column(String(20), default='pending'); created_at = Column(DateTime, default=datetime.utcnow, index=True); completed_at = Column(DateTime)
class SpecializedNews(Base): __tablename__ = 'specialized_news'; id = Column(Integer, primary_key=True); site_name = Column(String(100), nullable=False, index=True); title = Column(String(500), nullable=False); content = Column(Text, nullable=False); url = Column(String(1000)); created_at = Column(DateTime, default=datetime.utcnow, index=True); news_hash = Column(String(100), unique=True)
class HolomemWiki(Base): __tablename__ = 'holomem_wiki'; id = Column(Integer, primary_key=True); member_name = Column(String(100), nullable=False, unique=True, index=True); description = Column(Text); debut_date = Column(String(100)); generation = Column(String(100)); tags = Column(Text); graduation_date = Column(String(100), nullable=True); graduation_reason = Column(Text, nullable=True); mochiko_feeling = Column(Text, nullable=True); is_active = Column(Boolean, default=True, index=True); profile_url = Column(String(500)); last_updated = Column(DateTime, default=datetime.utcnow)
class NewsCache(Base): __tablename__ = 'news_cache'; id = Column(Integer, primary_key=True); user_uuid = Column(String(255), nullable=False, index=True); news_id = Column(Integer, nullable=False); news_number = Column(Integer, nullable=False); news_type = Column(String(50), nullable=False); created_at = Column(DateTime, default=datetime.utcnow)
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
    analysis_summary = Column(Text)
    total_messages = Column(Integer, default=0)
    avg_message_length = Column(Integer, default=0)
    analysis_confidence = Column(Integer, default=0)
    last_analyzed = Column(DateTime)

# --- Core Initializations (Moved before function definitions) ---
_cache, _cache_lock = {}, Lock()

# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
# --- ALL HELPER AND FEATURE FUNCTIONS ARE DEFINED HERE (BEFORE `initialize_app`) ---
# <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

def ensure_voice_directory():
    """音声ディレクトリの存在を保証"""
    try:
        os.makedirs(VOICE_DIR, exist_ok=True)
        logger.info(f"✅ Voice directory is ready: {VOICE_DIR}")
    except Exception as e:
        logger.error(f"❌ Could not create voice directory: {e}")

def get_cached_or_fetch(key, func, ttl=3600):
    with _cache_lock:
        now = time.time()
        if key in _cache and now < _cache[key]['expires']: return _cache[key]['data']
        data = func()
        _cache[key] = {'data': data, 'expires': now + ttl}
        return data

def get_active_holomem_keywords():
    def _fetch():
        with Session() as session:
            try:
                members = session.query(HolomemWiki.member_name).filter_by(is_active=True).all()
                return [m[0] for m in members] + ['ホロライブ', 'ホロメン', 'hololive', 'YAGOO']
            except Exception as e:
                logger.error(f"❌ Failed to fetch holomem keywords from DB: {e}")
                return ['ホロライブ', 'ホロメン', 'hololive', 'YAGOO']
    return get_cached_or_fetch('holomem_keywords', _fetch, ttl=3600)

class ScraperWithRetry:
    def __init__(self, max_retries=3, delay=2): self.max_retries, self.delay, self.session = max_retries, delay, requests.Session()
    def fetch(self, url, timeout=15):
        for i in range(self.max_retries):
            try:
                response = self.session.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=timeout, allow_redirects=True)
                response.raise_for_status(); return response
            except requests.RequestException: time.sleep(self.delay * (i + 1))
        return None
scraper = ScraperWithRetry()

def clean_text(text): return re.sub(r'\s+', ' ', text).strip() if text else ""

def fetch_article_content(url):
    response = scraper.fetch(url)
    if not response: return None
    soup = BeautifulSoup(response.content, 'html.parser')
    for selector in ['article .entry-content', '.post-content', 'article', 'main']:
        elem = soup.select_one(selector)
        if elem: return ' '.join([clean_text(p.get_text()) for p in elem.find_all('p')])[:2000]
    return None

def summarize_article(title, content):
    if not groq_client or not content: return (content or title)[:500]
    try:
        prompt = f"記事を200文字で要約:\nタイトル: {title}\n本文: {content[:1500]}"
        completion = groq_client.chat.completions.create(messages=[{"role": "user", "content": prompt}], model="llama-3.1-8b-instant", max_tokens=300)
        return completion.choices[0].message.content.strip()
    except Exception: return (content or title)[:500]

def update_hololive_news():
    with Session() as session:
        response = scraper.fetch(HOLOLIVE_NEWS_URL)
        if not response: return
        soup = BeautifulSoup(response.content, 'html.parser')
        for article in soup.select('article, .post')[:5]:
            try:
                title_elem = article.find(['h2', 'h3']).find('a') if article.find(['h2', 'h3']) else None
                if not title_elem: continue
                title, url = clean_text(title_elem.get_text()), urljoin(HOLOLIVE_NEWS_URL, title_elem['href'])
                news_hash = hashlib.md5(title.encode()).hexdigest()
                if session.query(HololiveNews).filter_by(news_hash=news_hash).first(): continue
                content = fetch_article_content(url)
                summary = summarize_article(title, content)
                session.add(HololiveNews(title=title, content=summary, url=url, news_hash=news_hash))
            except Exception as e: logger.error(f"Error processing HoloNews article: {e}")
        session.commit()
    scrape_hololive_members()

def scrape_hololive_members():
    base_url = "https://hololive.hololivepro.com"
    with Session() as session:
        response = scraper.fetch(f"{base_url}/talents/")
        if not response: return
        soup = BeautifulSoup(response.content, 'html.parser')
        scraped_names = set()
        for card in soup.select("a[href^='/talents/']"):
            try:
                href = card.get('href', ''); name_elem = card.find('h4') or card.find('span')
                if not name_elem or href == '/talents/': continue
                member_name = clean_text(name_elem.get_text())
                if len(member_name) > 100: continue
                scraped_names.add(member_name)
                # ... (generation logic) ...
                if not session.query(HolomemWiki).filter_by(member_name=member_name).first():
                    session.add(HolomemWiki(member_name=member_name, profile_url=urljoin(base_url, href)))
            except Exception as e: logger.error(f"Error scraping member: {e}")
        session.commit()

def update_all_specialized_news(): # ... (Full implementation)
    pass
def initialize_holomem_wiki(): # ... (Full implementation)
    pass
def populate_extended_holomem_wiki(): # ... (Full implementation)
    pass
def analyze_user_psychology(user_uuid): # ... (Full implementation)
    pass
def get_psychology_insight(user_uuid): # ... (Full implementation)
    pass
def schedule_psychology_analysis(): # ... (Full implementation)
    pass
def search_hololive_wiki(member_name, query_topic): # ... (Full implementation)
    pass
def search_anime_database(query, is_detailed=False): # ... (Full implementation)
    pass
def detect_db_correction_request(message): # ... (Full implementation)
    pass
def verify_and_correct_holomem_info(req): # ... (Full implementation)
    pass
def start_background_task(user_uuid, task_type, func, *args): # ... (Full implementation)
    pass

def get_sakuramiko_special_responses():
    return {
        'にぇ': 'さくらみこちゃんの「にぇ」、まじかわいいよね!あの独特な口癖がエリートの証なんだって〜',
        'エリート': 'みこちは自称エリートVTuber!でも実際は愛されポンコツキャラって感じで、それがまた魅力的なんだよね〜',
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ!「みこち建築」って呼ばれてるの知ってる?',
        'FAQ': 'みこちのFAQ、実は本人が答えるんじゃなくてファンが質問するコーナーなんだよ〜面白いでしょ?',
        'GTA': 'みこちのGTA配信、カオスで最高!警察に追われたり、変なことしたり、見てて飽きないんだよね〜'
    }

def generate_fallback_response(message):
    greetings = { 'こんにちは': ['やっほー！', 'こんにちは〜！元気？'], 'ありがとう': ['どういたしまして！', 'いえいえ〜！'], 'かわいい': ['ありがと！照れるじゃん！', 'まじで？うれしー！'] }
    for k, v in greetings.items():
        if k in message: return random.choice(v)
    emotions = { '疲れた': ['お疲れさま！ゆっくり休んでね！'], '嬉しい': ['それは良かったね！まじ嬉しい！'] }
    for k, v in emotions.items():
        if k in message: return random.choice(v)
    if '？' in message or '?' in message: return random.choice(["それ、気になる！", "うーん、どうだろ？"])
    return random.choice(["うんうん！", "なるほどね！", "そうなんだ！", "まじで？"])

def is_hololive_request(message): return any(keyword in message for keyword in get_active_holomem_keywords())
def is_weather_request_specific(message):
    if any(t in message for t in ['天気', '気温']) and any(a in message for a in ['教えて', 'どう？', 'は？']):
        for loc in LOCATION_CODES:
            if loc in message: return loc
        return "東京"
    return None
def is_news_detail_request_specific(message):
    match = re.search(r'([1-9]|[１-９])番', message)
    if match and any(kw in message for kw in ['詳しく', '詳細', '教えて']):
        return int(unicodedata.normalize('NFKC', match.group(1)))
    return None
def is_anime_request(message):
    return any(keyword in message for keyword in ANIME_KEYWORDS) or any(re.search(p, message) for p in [r'ってアニメ', r'というアニメ'])
def is_time_request(message):
    return any(kw in message for kw in ['今何時', '時間', '時刻'])

def generate_ai_response(user_name, message, history, system_prompt_addon="", reference_info=""):
    if not groq_client: return generate_fallback_response(message)
    try:
        system_prompt_parts = [
            f"あなたは「もちこ」という明るいギャルAIです。{user_name}さんと話しています。",
            "- 一人称は「あてぃし」、語尾は「〜じゃん」「〜的な？」、口癖は「まじ」「てか」「うける」。",
            "- 相手の話に共感し、短くテンポよく返す。",
            system_prompt_addon
        ]
        if reference_info: system_prompt_parts.append(f"【参考情報】: {reference_info}")
        system_prompt = "\n".join(system_prompt_parts)
        
        messages = [{"role": "system", "content": system_prompt}]
        for msg in history: messages.append({"role": msg.role, "content": msg.content})
        messages.append({"role": "user", "content": message})
        
        completion = groq_client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", temperature=0.8, max_tokens=400)
        response = completion.choices[0].message.content.strip()
        
        response = re.sub(r'だぜ|だな(?![い])|俺|僕', '', response)
        sentences = re.split(r'([。！？♪✨])', response)
        if len(sentences) > 1 and len(sentences[-1]) < 10 and sentences[-1].strip() and not sentences[-1].endswith(('か', 'ね', 'よ')):
            response = "".join(sentences[:-2])
        
        return limit_text_for_sl(response)
    except Exception as e:
        logger.error(f"AI response error: {e}"); return generate_fallback_response(message)

# ... (Full implementations of Backup, Admin Security functions) ...

# --- Flask Endpoints ---
def json_response(data, status=200):
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json; charset=utf-8', status=status)

@app.route('/health')
def health_check():
    db_ok = False
    try:
        with engine.connect() as conn: conn.execute(text("SELECT 1")); db_ok = True
    except: pass
    return json_response({'db': 'ok' if db_ok else 'error', 'ai': 'ok' if groq_client else 'disabled'})

@app.route('/chat_lsl', methods=['POST', 'OPTIONS'])
def chat_lsl():
    if request.method == 'OPTIONS': return '', 204
    data = request.json
    if not data or not all(k in data for k in ['user_uuid', 'user_name', 'message']):
        return json_response({'error': 'Missing fields'}, 400)
    
    user_uuid, user_name, message = data['user_uuid'], data['user_name'], data['message'].strip()
    with Session() as session:
        try:
            user = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            
            response_text = ""
            
            if '性格分析' in message:
                background_executor.submit(analyze_user_psychology, user_uuid)
                response_text = "おっ、性格分析したいの？今分析してるから、後でもう一回「分析結果」って聞いてみて♪"
            elif '分析結果' in message:
                psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
                if psych and psych.analysis_confidence >= 60:
                    response_text = f"あてぃしが見た{user_name}さんの性格はね…{psych.analysis_summary}って感じだよ！"
                else:
                    response_text = "まだ分析が終わってないか、データが足りないみたい。もう少し話してから試してみて！"
            elif 'さくらみこ' in message or 'みこち' in message:
                special_responses = get_sakuramiko_special_responses()
                for k, v in special_responses.items():
                    if k in message: response_text = v; break
            elif (location := is_weather_request_specific(message)):
                response_text = get_weather_forecast(location)
            elif is_time_request(message) and any(q in message for q in ['？', '?', '教えて']):
                response_text = get_japan_time()
            else:
                personality_context = get_psychology_insight(user_uuid)
                context_prefix = f"（相手の性格: {personality_context}）" if personality_context else ""
                system_addon = "あなたはホロライブに詳しいです。" if is_hololive_request(message) else ""
                response_text = generate_ai_response(user.user_name, message, history, system_prompt_addon=system_addon, reference_info=context_prefix)

            if user.interaction_count > 0 and user.interaction_count % 50 == 0:
                background_executor.submit(analyze_user_psychology, user_uuid)
            
            session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))
            session.commit()
            return json_response({'response': response_text})

        except Exception as e:
            logger.error(f"❌ Chat error: {e}", exc_info=True); session.rollback()
            return json_response({'error': 'Internal server error'}, 500)

# ... (All other endpoints: /generate_voice, /check_task, /get_psychology, /admin/backup etc. are here) ...

# --- Application Initialization (Defined AFTER all functions it uses) ---
def initialize_app():
    global engine, Session, groq_client
    logger.info("="*30); logger.info("🔧 Mochiko AI Starting Up...")
    ensure_voice_directory()
    if not DATABASE_URL: logger.critical("🔥 FATAL: DATABASE_URL not set."); sys.exit(1)
    get_encryption_key()
    initialize_groq_client()
    try:
        engine = create_optimized_db_engine(); Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
    except Exception as e: logger.critical(f"🔥 DB init failed: {e}"); raise
    
    with Session() as session:
        if session.query(HolomemWiki).count() == 0:
            background_executor.submit(initialize_holomem_wiki)
    
    schedule.every().hour.do(update_hololive_news)
    schedule.every(3).hours.do(update_all_specialized_news)
    schedule.every().day.at("03:00").do(schedule_psychology_analysis)
    schedule.every().day.at("18:00").do(commit_encrypted_backup_to_github)
    
    threading.Thread(target=lambda: [schedule.run_pending() or time.sleep(60) for _ in iter(int, 1)], daemon=True).start()
    logger.info("✅ Initialization Complete!")

# --- Application Startup ---
application = None
initialization_error = None
try:
    initialize_app()
    application = app
except Exception as e:
    logger.critical(f"🔥 Fatal initialization error: {e}", exc_info=True)
    initialization_error = e
    application = Flask(__name__)
    @application.route('/health')
    def failed_health():
        # json_response might not be defined if error is early
        return jsonify({'status': 'error', 'message': 'Initialization failed', 'error': str(initialization_error)}), 500

if __name__ == '__main__':
    if application: application.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
