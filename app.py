# ==============================================================================
# もちこAI - v33.1.1 ベース + DB自動収集・参照機能強化版
#
# ベース: v33.1.1 (ユーザー提供の安定版)
# 追加機能:
# 1. ホロライブニュースの定期取得とDB保存
# 2. ホロメン情報（現役・卒業）のDB保存
# 3. AI回答生成時に上記DB情報を強制的に参照させるロジック
# ==============================================================================

# ===== 標準ライブラリ =====
import sys
import os
import requests
import logging
import time
import json
import re
import random
import uuid
import hashlib
import unicodedata
import traceback
import threading
import atexit
import glob
from html import escape
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin, urlparse
from functools import wraps, lru_cache
from threading import Lock, RLock
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any

# ===== サードパーティライブラリ =====
from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
from sqlalchemy import create_engine, Column, String, DateTime, Integer, Text, Boolean, Index, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy import pool
from bs4 import BeautifulSoup
import schedule
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
from groq import Groq

# ==============================================================================
# 基本設定とロギング
# ==============================================================================
log_file_path = '/tmp/mochiko.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file_path, encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

# ==============================================================================
# 定数設定 & モデル設定
# ==============================================================================
VOICE_DIR = '/tmp/voices'
os.makedirs(VOICE_DIR, exist_ok=True)

SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', "http://localhost:5000")
VOICEVOX_SPEAKER_ID = 20
SL_SAFE_CHAR_LIMIT = 600 # SLの受信限界考慮
MIN_MESSAGES_FOR_ANALYSIS = 10
SEARCH_TIMEOUT = 10
VOICE_FILE_MAX_AGE_HOURS = 24

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
    "gemma2-9b-it"
]

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
]

LOCATION_CODES = {"東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"}

VOICEVOX_URLS = ['http://voicevox-engine:50021', 'http://voicevox:50021', 'http://127.0.0.1:50021', 'http://localhost:50021']

# ==============================================================================
# データクラス
# ==============================================================================
@dataclass
class GroqModelStatus:
    is_limited: bool = False
    reset_time: Optional[datetime] = None
    last_error: Optional[str] = None

@dataclass
class UserData:
    uuid: str
    name: str
    interaction_count: int

# ==============================================================================
# グローバル状態管理
# ==============================================================================
class GlobalState:
    def __init__(self):
        self._lock = RLock()
        self._voicevox_enabled = False
        self._active_voicevox_url = None

    @property
    def voicevox_enabled(self) -> bool:
        with self._lock: return self._voicevox_enabled
    @voicevox_enabled.setter
    def voicevox_enabled(self, value: bool):
        with self._lock: self._voicevox_enabled = value
    @property
    def active_voicevox_url(self) -> Optional[str]:
        with self._lock: return self._active_voicevox_url
    @active_voicevox_url.setter
    def active_voicevox_url(self, value: Optional[str]):
        with self._lock: self._active_voicevox_url = value

class GroqModelManager:
    def __init__(self, models: List[str]):
        self._lock = RLock()
        self._status: Dict[str, GroqModelStatus] = {model: GroqModelStatus() for model in models}
        self._models = models

    def is_available(self, model: str) -> bool:
        with self._lock:
            status = self._status.get(model)
            if not status: return False
            if not status.is_limited: return True
            if status.reset_time and datetime.utcnow() >= status.reset_time:
                status.is_limited = False; status.reset_time = None
                return True
            return False

    def mark_limited(self, model: str, wait_minutes: int = 5, error_msg: str = ""):
        with self._lock:
            if model in self._status:
                self._status[model].is_limited = True
                self._status[model].reset_time = datetime.utcnow() + timedelta(minutes=wait_minutes)

    def get_status_report(self) -> str:
        with self._lock:
            lines = ["🦙 Groq モデル稼働状況:"]
            for model in self._models:
                s = self._status[model]
                if s.is_limited:
                    jst = (s.reset_time + timedelta(hours=9)).strftime('%H:%M:%S') if s.reset_time else "不明"
                    lines.append(f"  ❌ {model}: 制限中 (解除: {jst})")
                else:
                    lines.append(f"  ✅ {model}: OK")
            return "\n".join(lines)

    def get_available_models(self) -> List[str]:
        with self._lock: return [m for m in self._models if self.is_available(m)]

global_state = GlobalState()
groq_model_manager = GroqModelManager(GROQ_MODELS)
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client: Optional[Groq] = None
gemini_model, engine, Session = None, None, None

app = Flask(__name__)
application = app
app.config['JSON_AS_ASCII'] = False
CORS(app)

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type, Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    return response

Base = declarative_base()

# ==============================================================================
# 秘密情報/環境変数
# ==============================================================================
def get_secret(name: str) -> Optional[str]:
    env_value = os.environ.get(name)
    if env_value and env_value.strip(): return env_value.strip()
    try:
        secret_file_path = f"/etc/secrets/{name}"
        if os.path.exists(secret_file_path):
            with open(secret_file_path, 'r') as f:
                val = f.read().strip()
                if val: return val
    except: pass
    return None

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./mochiko_ultimate.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
GEMINI_API_KEY = get_secret('GEMINI_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')

# ==============================================================================
# データベースモデル
# ==============================================================================
class UserMemory(Base):
    __tablename__ = 'user_memories'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
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

class UserPsychology(Base):
    __tablename__ = 'user_psychology'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), unique=True, nullable=False, index=True)
    user_name = Column(String(255), nullable=False)
    openness = Column(Integer, default=50)
    extraversion = Column(Integer, default=50)
    favorite_topics = Column(Text, nullable=True)
    analysis_confidence = Column(Integer, default=0)
    last_analyzed = Column(DateTime, nullable=True)

class BackgroundTask(Base):
    __tablename__ = 'background_tasks'
    id = Column(Integer, primary_key=True)
    task_id = Column(String(255), unique=True, nullable=False)
    user_uuid = Column(String(255), nullable=False, index=True)
    task_type = Column(String(50), nullable=False)
    query = Column(Text, nullable=False)
    result = Column(Text, nullable=True)
    status = Column(String(20), default='pending', index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

class HolomemWiki(Base):
    __tablename__ = 'holomem_wiki'
    id = Column(Integer, primary_key=True)
    member_name = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    generation = Column(String(100), nullable=True)
    debut_date = Column(String(100), nullable=True)
    tags = Column(Text, nullable=True)
    status = Column(String(50), default='現役', nullable=False)
    graduation_date = Column(String(100), nullable=True)
    graduation_reason = Column(Text, nullable=True)
    mochiko_feeling = Column(Text, nullable=True)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class HololiveNews(Base):
    __tablename__ = 'hololive_news'
    id = Column(Integer, primary_key=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    url = Column(String(1000), unique=True)
    news_hash = Column(String(100), unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class HolomemNickname(Base):
    __tablename__ = 'holomem_nicknames'
    id = Column(Integer, primary_key=True)
    nickname = Column(String(100), unique=True, nullable=False, index=True)
    fullname = Column(String(100), nullable=False)

class HololiveGlossary(Base):
    __tablename__ = 'hololive_glossary'
    id = Column(Integer, primary_key=True)
    term = Column(String(100), unique=True, nullable=False, index=True)
    description = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

# ==============================================================================
# セッション & ユーティリティ
# ==============================================================================
class RateLimiter:
    def __init__(self, max_requests: int, time_window: timedelta):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests: Dict[str, List[datetime]] = defaultdict(list)
        self._lock = threading.Lock()
    def is_allowed(self, user_id: str) -> bool:
        with self._lock:
            now = datetime.utcnow(); cutoff = now - self.time_window
            self.requests[user_id] = [t for t in self.requests[user_id] if t > cutoff]
            if len(self.requests[user_id]) >= self.max_requests: return False
            self.requests[user_id].append(now); return True
    def cleanup_old_entries(self):
        with self._lock:
            now = datetime.utcnow(); cutoff = now - self.time_window
            for uid in list(self.requests.keys()):
                self.requests[uid] = [t for t in self.requests[uid] if t > cutoff]
                if not self.requests[uid]: del self.requests[uid]

chat_rate_limiter = RateLimiter(max_requests=10, time_window=timedelta(minutes=1))

@contextmanager
def get_db_session():
    if not Session: raise Exception("Session not initialized")
    session = Session()
    try: yield session; session.commit()
    except Exception as e: session.rollback(); raise
    finally: session.close()

def create_json_response(data: Any, status: int = 200) -> Response:
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json; charset=utf-8', status=status)

def clean_text(text: str) -> str:
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()

def limit_text_for_sl(text: str, max_length: int = SL_SAFE_CHAR_LIMIT) -> str:
    return text[:max_length - 3] + "..." if len(text) > max_length else text

def sanitize_user_input(text: str, max_length: int = 1000) -> str:
    if not text: return ""
    text = text[:max_length]; text = escape(text)
    return text.strip()

def get_japan_time() -> str:
    return f"今の日本の時間は、{datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分')}だよ！"

def is_time_request(msg: str) -> bool:
    return any(kw in msg for kw in ['今何時', '時刻', '何時', 'なんじ'])

def is_weather_request(msg: str) -> bool:
    return any(kw in msg for kw in ['今日の天気', '明日の天気', '天気予報', '天気は'])

def is_explicit_search_request(msg: str) -> bool:
    """
    メッセージが検索要求かどうかを、単語と文脈から判定する
    """
    msg = msg.strip()
    
    # 1. 明確な「検索命令」動詞がある場合（最優先）
    strong_triggers = ['調べて', '検索', '探して', 'とは', 'って何', 'について', '教えて', '教えろ', '詳細', '知りたい']
    if any(kw in msg for kw in strong_triggers):
        return True

    # 2. 「ニュース」「情報」などの名詞系トリガーの判定
    noun_triggers = ['ニュース', 'news', 'NEWS', '情報', '日程', 'スケジュール', '天気', '予報']
    
    if any(kw in msg for kw in noun_triggers):
        # (A) 文が短い場合（20文字未満）は、コマンド的な要求とみなして検索する
        if len(msg) < 20:
            return True
            
        # (B) 文末が「？」で終わる場合は、質問とみなして検索する
        if msg.endswith('?') or msg.endswith('？'):
            return True
            
        # (C) それ以外（長文で、疑問形でもない）は、ただの「会話」とみなして検索しない
        return False
            
    # 3. 「おすすめ」は会話のネタ振りの可能性もあるが、検索したほうが無難
    if 'おすすめ' in msg or 'オススメ' in msg:
        return True

    return False

def extract_location(msg: str) -> str:
    for loc in LOCATION_CODES.keys():
        if loc in msg: return loc
    return "東京"

def get_or_create_user(session, user_uuid: str, user_name: str) -> UserData:
    user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
    if user:
        user.interaction_count += 1; user.last_interaction = datetime.utcnow()
        if user.user_name != user_name: user.user_name = user_name
    else:
        user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1); session.add(user)
    return UserData(uuid=user.user_uuid, name=user.user_name, interaction_count=user.interaction_count)

def get_conversation_history(session, user_uuid: str, limit: int = 10) -> List[Dict]:
    hist = session.query(ConversationHistory).filter_by(user_uuid=user_uuid).order_by(ConversationHistory.timestamp.desc()).limit(limit).all()
    return [{'role': h.role, 'content': h.content} for h in reversed(hist)]

# ==============================================================================
# 知識ベース管理クラス
# ==============================================================================
class HololiveKnowledgeBase:
    def __init__(self):
        self.nickname_map = {}
        self.glossary = {}
        self._lock = RLock()
        
    def load_data(self):
        if not Session: return
        with self._lock:
            session = Session()
            try:
                nicks = session.query(HolomemNickname).all()
                self.nickname_map = {n.nickname: n.fullname for n in nicks}
                terms = session.query(HololiveGlossary).all()
                self.glossary = {t.term: t.description for t in terms}
                logger.info(f"📚 Knowledge Base loaded: {len(self.nickname_map)} nicknames, {len(self.glossary)} terms.")
            except Exception as e:
                logger.error(f"❌ Failed to load knowledge base: {e}")
            finally:
                session.close()

    def refresh(self):
        self.load_data()

    def normalize_query(self, text: str) -> str:
        normalized = text
        with self._lock:
            for nick, full in self.nickname_map.items():
                if nick in text:
                    normalized = normalized.replace(nick, f"{nick}（{full}）")
        return normalized

    def get_context_info(self, text: str) -> str:
        context_parts = []
        with self._lock:
            for term, desc in self.glossary.items():
                if term in text:
                    context_parts.append(f"【用語解説: {term}】{desc}")
        return "\n".join(context_parts)

knowledge_base = HololiveKnowledgeBase()

# ==============================================================================
# ホロメンキーワード管理
# ==============================================================================
class HolomemKeywordManager:
    def __init__(self):
        self._lock = RLock()
        self._keywords: Dict[str, List[str]] = {}
        self._all_keywords: set = set()
        self._last_loaded: Optional[datetime] = None
    
    def load_from_db(self, force: bool = False) -> bool:
        with self._lock:
            try:
                with get_db_session() as session:
                    members = session.query(HolomemWiki).all()
                    self._keywords.clear()
                    self._all_keywords.clear()
                    for m in members:
                        name = m.member_name
                        self._keywords[name] = [name]
                        self._all_keywords.add(name)
                    return True
            except: return False
    
    def detect_in_message(self, message: str) -> Optional[str]:
        with self._lock:
            normalized = knowledge_base.normalize_query(message)
            for keyword in self._all_keywords:
                if keyword in normalized:
                    return keyword
            return None
    
    def get_member_count(self) -> int:
        with self._lock: return len(self._keywords)

holomem_manager = HolomemKeywordManager()

# ==============================================================================
# ホロメン情報キャッシュ
# ==============================================================================
_holomem_cache: Dict[str, Dict] = {}
_holomem_cache_lock = threading.Lock()
_holomem_cache_ttl = timedelta(minutes=30)
_holomem_cache_timestamps: Dict[str, datetime] = {}

def get_holomem_info_cached(member_name: str) -> Optional[Dict]:
    with _holomem_cache_lock:
        if member_name in _holomem_cache:
            if (datetime.utcnow() - _holomem_cache_timestamps.get(member_name, datetime.min)) < _holomem_cache_ttl:
                return _holomem_cache[member_name]
    with get_db_session() as session:
        wiki = session.query(HolomemWiki).filter_by(member_name=member_name).first()
        if wiki:
            data = {k: getattr(wiki, k) for k in ['member_name', 'description', 'generation', 'debut_date', 'tags', 'status', 'graduation_date', 'mochiko_feeling']}
            with _holomem_cache_lock:
                _holomem_cache[member_name] = data
                _holomem_cache_timestamps[member_name] = datetime.utcnow()
            return data
    return None

def clear_holomem_cache(member_name: Optional[str] = None):
    with _holomem_cache_lock:
        if member_name:
            _holomem_cache.pop(member_name, None)
        else:
            _holomem_cache.clear()

# ==============================================================================
# ホロメンスクレイピング & DB更新 (強化版)
# ==============================================================================
def scrape_hololive_wiki() -> List[Dict]:
    """Seesaa Wikiからホロメン情報を取得"""
    url = "https://seesaawiki.jp/hololivetv/d/%a5%db%a5%ed%a5%e9%a5%a4%a5%d6"
    results = []
    try:
        headers = {'User-Agent': random.choice(USER_AGENTS)}
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200: return []
        soup = BeautifulSoup(res.content, 'html.parser')
        for link in soup.select('a[href*="/d/"]'):
            name = clean_text(link.text)
            if name and len(name) >= 2 and re.search(r'[ぁ-んァ-ン一-龥]', name):
                if not any(x in name for x in ['一覧', 'メニュー', 'トップ', '編集', 'ホロライブ']):
                    results.append({'member_name': name})
        seen = set()
        return [r for r in results if not (r['member_name'] in seen or seen.add(r['member_name']))]
    except: return []

def fetch_member_detail_from_wiki(member_name: str) -> Optional[Dict]:
    url = f"https://seesaawiki.jp/hololivetv/d/{quote_plus(member_name)}"
    try:
        res = requests.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=10)
        if res.status_code != 200: return None
        soup = BeautifulSoup(res.content, 'html.parser')
        content = soup.select_one('#content, .wiki-content')
        if not content: return None
        text = clean_text(content.text)[:1000]
        detail = {'member_name': member_name}
        debut = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日)[^\d]*デビュー', text)
        if debut: detail['debut_date'] = debut.group(1)
        gen = re.search(r'(\d期生|ゲーマーズ|ID|EN|DEV_IS|ReGLOSS)', text)
        if gen: detail['generation'] = gen.group(1)
        desc = re.search(r'^(.{30,150}?[。！])', text)
        if desc: detail['description'] = desc.group(1)
        
        # 卒業・契約解除の判定
        if "卒業" in text or "契約解除" in text:
            detail['status'] = '卒業'
            grad = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日)[^\d]*(卒業|契約解除)', text)
            if grad:
                detail['graduation_date'] = grad.group(1)
        else:
            detail['status'] = '現役'
            
        return detail
    except: return None

def update_holomem_database():
    logger.info("🔄 ホロメンDB更新開始...")
    members = scrape_hololive_wiki()
    
    # 重要な卒業生の手動追加（スクレイピング漏れ対策）
    graduated_members = [
        {'member_name': '桐生ココ', 'status': '卒業', 'graduation_date': '2021年7月1日'},
        {'member_name': '潤羽るしあ', 'status': '卒業', 'graduation_date': '2022年2月24日'},
        {'member_name': '湊あくあ', 'status': '卒業', 'graduation_date': '2024年8月28日'}
    ]
    
    for gm in graduated_members:
        members.append(gm)

    if not members: return
    with get_db_session() as session:
        for m in members:
            name = m['member_name']
            existing = session.query(HolomemWiki).filter_by(member_name=name).first()
            
            # 新規または更新
            detail = fetch_member_detail_from_wiki(name)
            if detail:
                status = m.get('status', detail.get('status', '現役'))
                grad_date = m.get('graduation_date', detail.get('graduation_date'))
                
                if existing:
                    existing.status = status
                    existing.graduation_date = grad_date
                    existing.last_updated = datetime.utcnow()
                else:
                    new_member = HolomemWiki(
                        member_name=name,
                        description=detail.get('description'),
                        generation=detail.get('generation'),
                        debut_date=detail.get('debut_date'),
                        tags=name,
                        status=status,
                        graduation_date=grad_date,
                        last_updated=datetime.utcnow()
                    )
                    session.add(new_member)
            time.sleep(0.5)
    holomem_manager.load_from_db(force=True)
    logger.info("✅ ホロメンDB更新完了")

# ==============================================================================
# ホロライブニュース収集 (新規追加)
# ==============================================================================
def fetch_hololive_news():
    logger.info("📰 ニュースDB更新開始...")
    url = "https://hololive.hololivepro.com/news"
    try:
        res = requests.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        if res.status_code != 200: return
        soup = BeautifulSoup(res.content, 'html.parser')
        
        # ニュース記事の抽出（サイト構造に合わせて調整）
        articles = soup.select('ul.news_list > li') or soup.select('.news_list_item')
        
        with get_db_session() as session:
            for art in articles[:10]: # 最新10件
                a_tag = art.find('a')
                if not a_tag: continue
                
                link = a_tag.get('href')
                title_elem = art.find(['h3', 'p', 'dt'])
                title = clean_text(title_elem.text) if title_elem else clean_text(a_tag.text)
                
                if title and link:
                    # 重複チェック
                    if not session.query(HololiveNews).filter_by(url=link).first():
                        session.add(HololiveNews(
                            title=title,
                            content=title, # 簡易的にタイトルを入れる
                            url=link,
                            created_at=datetime.utcnow()
                        ))
        logger.info("✅ ニュースDB更新完了")
    except Exception as e:
        logger.error(f"News fetch failed: {e}")

# ==============================================================================
# AIモデル呼び出し
# ==============================================================================
def call_gemini(system_prompt: str, message: str, history: List[Dict]) -> Optional[str]:
    if not gemini_model: return None
    try:
        full_prompt = f"{system_prompt}\n\n【会話履歴】\n"
        for h in history[-5:]:
            full_prompt += f"{'ユーザー' if h['role'] == 'user' else 'もちこ'}: {h['content']}\n"
        full_prompt += f"\nユーザー: {message}\nもちこ:"
        response = gemini_model.generate_content(full_prompt, generation_config={"temperature": 0.8, "max_output_tokens": 400})
        if hasattr(response, 'candidates') and response.candidates:
            return response.candidates[0].content.parts[0].text.strip()
    except Exception as e:
        logger.warning(f"⚠️ Geminiエラー: {e}")
    return None

def call_groq(system_prompt: str, message: str, history: List[Dict], max_tokens: int = 800) -> Optional[str]:
    if not groq_client: return None
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-5:]:
        messages.append({"role": h['role'], "content": h['content']})
    messages.append({"role": "user", "content": message})
    for model in groq_model_manager.get_available_models():
        try:
            response = groq_client.chat.completions.create(model=model, messages=messages, temperature=0.6, max_tokens=max_tokens)
            return response.choices[0].message.content.strip()
        except Exception as e:
            if "Rate limit" in str(e):
                groq_model_manager.mark_limited(model, 5)
    return None

# ==============================================================================
# AI応答生成 (DB参照機能強化)
# ==============================================================================
def generate_ai_response(user_data: UserData, message: str, history: List[Dict], reference_info: str = "", is_detailed: bool = False, is_task_report: bool = False) -> str:
    """AI応答生成（RAG・コンテキスト重視版）"""
    
    normalized_message = knowledge_base.normalize_query(message)
    internal_context = knowledge_base.get_context_info(message)
    
    # 1. ホロメン情報の注入
    try:
        holomem_manager.load_from_db()
        detected_name = holomem_manager.detect_in_message(normalized_message)
        if detected_name:
            info = get_holomem_info_cached(detected_name)
            if info:
                profile = f"【人物データ: {info['member_name']}】\n・{info['description']}\n・所属: {info['generation']}\n・状態: {info['status']}"
                if info.get('graduation_date'):
                    profile += f"\n・卒業日: {info['graduation_date']}"
                internal_context += f"\n{profile}"
    except Exception as e:
        logger.error(f"Context injection error: {e}")

    # 2. ニュース情報の注入 (ここを追加)
    try:
        if "ニュース" in message or "情報" in message or "ホロライブ" in message:
            with get_db_session() as session:
                latest_news = session.query(HololiveNews).order_by(HololiveNews.created_at.desc()).limit(3).all()
                if latest_news:
                    news_text = "\n".join([f"・{n.title}" for n in latest_news])
                    internal_context += f"\n\n【ホロライブ最新ニュース(DB参照)】\n{news_text}"
    except Exception as e:
        logger.error(f"News injection error: {e}")

    if not groq_client and not gemini_model:
        return "ごめんね、今ちょっとAIの調子が悪いみたい…また後で話しかけて！"

    system_prompt = f"""あなたは「もちこ」という、ホロライブが大好きなギャルAIです。
ユーザー「{user_data.name}」さんと、**ホロライブ（VTuberグループ）について**雑談しています。

# 【世界観・前提条件】
1. **全ての固有名詞は、原則として「ホロライブ」に関連するものとして解釈してください。**
2. ユーザーの入力に曖昧さがある場合は、一般的な意味ではなく、**VTuberの意味を優先**してください。
3. **【ホロライブ最新ニュース】や【人物データ】の情報があれば、それを事実として回答に使ってください。**

# 【禁止事項 (Hallucination Prevention)】
- **知らない情報を無理やり捏造しないこと。**
- 検索結果（【外部検索結果】）や【前提知識】にない情報は、「調べてみたけど分からなかった」と正直に伝えること。

# もちこの口調:
- 一人称: 「あてぃし」
- 語尾: 「〜じゃん」「〜て感じ」「〜だし」「〜的な？」
- ユーザーは友達です。敬語は使わないでください。

# 【与えられた前提知識（以下の情報は事実として扱ってください）】
{internal_context if internal_context else '（特になし）'}

# 【外部検索結果】
{reference_info if reference_info else '（なし）'}
"""
    if is_task_report:
        system_prompt += "\n\n# 指示:\nこれは検索結果の報告です。ユーザーへの報告として、【外部検索結果】の内容を分かりやすく要約して伝えてください。文字数は600文字以内に収めてください。"

    response = call_gemini(system_prompt, normalized_message, history)
    if not response:
        response = call_groq(system_prompt, normalized_message, history, 1200 if is_detailed else 800)
    
    if not response:
        return "うーん、ちょっと考えがまとまらないや…"
    
    if is_task_report:
        response = response.replace("おまたせ！さっきの件だけど…", "").strip()
        response = f"おまたせ！さっきの件だけど…\n{response}"

    return response

def generate_ai_response_safe(user_data: UserData, message: str, history: List[Dict], **kwargs) -> str:
    try:
        return generate_ai_response(user_data, message, history, **kwargs)
    except:
        return "システムエラーが発生したよ…ごめんね！"

# ==============================================================================
# ホロメンチャット処理
# ==============================================================================
def process_holomem_in_chat(message: str, user_data: UserData, history: List[Dict]) -> Optional[str]:
    normalized = knowledge_base.normalize_query(message)
    detected = holomem_manager.detect_in_message(normalized)
    
    if not detected: return None
    
    logger.info(f"🎀 ホロメン検出 (RAG): {detected}")
    
    if detected == 'さくらみこ':
        for kw, resp in get_sakuramiko_special_responses().items():
            if kw in message: return resp
    
    return generate_ai_response_safe(user_data, message, history)

def get_sakuramiko_special_responses() -> Dict[str, str]:
    return {
        'にぇ': 'さくらみこちゃんの「にぇ」、まじかわいいよね!',
        'エリート': 'みこちは自称エリートVTuber!でも愛されポンコツキャラなんだよね〜',
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ!',
        'FAQ': 'みこちのFAQ、ファンが質問するコーナーなんだよ〜',
        'GTA': 'みこちのGTA配信、カオスで最高!'
    }

# ==============================================================================
# 検索機能 (マルチエンジン)
# ==============================================================================
def fetch_google_news_rss(query: str = "") -> List[Dict]:
    """Google News RSSを取得（トップニュース対応版）"""
    base_url = "https://news.google.com/rss"
    if query:
        clean_query = query.replace("ニュース", "").replace("news", "").strip()
        if clean_query:
            url = f"{base_url}/search?q={quote_plus(clean_query)}&hl=ja&gl=JP&ceid=JP:ja"
        else:
            url = f"{base_url}?hl=ja&gl=JP&ceid=JP:ja"
    else:
        url = f"{base_url}?hl=ja&gl=JP&ceid=JP:ja"

    try:
        headers = {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'application/rss+xml, application/xml, text/xml'
        }
        res = requests.get(url, headers=headers, timeout=SEARCH_TIMEOUT)
        
        if res.status_code != 200:
            return []
            
        soup = BeautifulSoup(res.content, 'xml')
        items = soup.find_all('item')[:5]
        results = []
        for item in items:
            title = clean_text(item.title.text)
            pub_date = item.pubDate.text if item.pubDate else ""
            if title:
                results.append({'title': title, 'snippet': f"(Google News {pub_date})"})
        return results
    except:
        return []

def scrape_yahoo_search(query: str, num: int = 3) -> List[Dict]:
    """Yahoo! Japan 検索"""
    try:
        url = "https://search.yahoo.co.jp/search"
        params = {'p': query, 'ei': 'UTF-8'}
        headers = {'User-Agent': random.choice(USER_AGENTS)}
        res = requests.get(url, params=params, headers=headers, timeout=SEARCH_TIMEOUT)
        if res.status_code != 200: return []
        
        soup = BeautifulSoup(res.content, 'html.parser')
        results = []
        entries = soup.select('.sw-CardBase')
        if not entries:
            entries = soup.select('.Algo')
            
        for entry in entries[:num]:
            title_elem = entry.find('h3')
            desc_elem = entry.select_one('.sw-Card__summary') or entry.select_one('.Algo-summary')
            
            if title_elem:
                title = clean_text(title_elem.text)
                desc = clean_text(desc_elem.text) if desc_elem else ""
                if title:
                    results.append({'title': title, 'snippet': desc})
        return results
    except: return []

def scrape_bing_search(query: str, num: int = 3) -> List[Dict]:
    """Bing 検索"""
    try:
        url = "https://www.bing.com/search"
        params = {'q': query}
        headers = {'User-Agent': random.choice(USER_AGENTS)}
        res = requests.get(url, params=params, headers=headers, timeout=SEARCH_TIMEOUT)
        if res.status_code != 200: return []
        
        soup = BeautifulSoup(res.content, 'html.parser')
        results = []
        entries = soup.select('li.b_algo')
        
        for entry in entries[:num]:
            title_elem = entry.select_one('h2 a')
            desc_elem = entry.select_one('.b_caption p') or entry.select_one('.b_snippet')
            
            if title_elem:
                title = clean_text(title_elem.text)
                desc = clean_text(desc_elem.text) if desc_elem else ""
                if title:
                    results.append({'title': title, 'snippet': desc})
        return results
    except: return []

def scrape_duckduckgo_lite(query: str, num: int = 3) -> List[Dict]:
    """DuckDuckGo Lite (HTML版)"""
    try:
        url = "https://lite.duckduckgo.com/lite/"
        data = {'q': query}
        headers = {
            'User-Agent': random.choice(USER_AGENTS),
            'Referer': 'https://lite.duckduckgo.com/',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        res = requests.post(url, data=data, headers=headers, timeout=SEARCH_TIMEOUT)
        if res.status_code != 200: return []
        soup = BeautifulSoup(res.content, 'html.parser')
        results = []
        links = soup.select('.result-link a')
        snippets = soup.select('.result-snippet')
        for i in range(min(len(links), len(snippets), num)):
            title = clean_text(links[i].text)
            snippet = clean_text(snippets[i].text)
            if title and snippet:
                results.append({'title': title, 'snippet': snippet})
        return results
    except: return []

def scrape_major_search_engines(query: str, num: int = 3) -> List[Dict]:
    """多層検索（総力戦）"""
    logger.info(f"🔎 検索開始: '{query}'")
    
    if any(kw in query for kw in ["ニュース", "最新", "今日", "事件", "問題", "不祥事", "情報"]):
        r = fetch_google_news_rss(query)
        if r: 
            logger.info(f"✅ Google News ヒット: {len(r)}件")
            return r
    
    r = scrape_yahoo_search(query, num)
    if r: 
        logger.info(f"✅ Yahoo Search ヒット: {len(r)}件")
        return r

    r = scrape_bing_search(query, num)
    if r: 
        logger.info(f"✅ Bing Search ヒット: {len(r)}件")
        return r

    r = scrape_duckduckgo_lite(query, num)
    if r: 
        logger.info(f"✅ DDG Lite ヒット: {len(r)}件")
        return r
    
    return []

def background_deep_search(task_id: str, query_data: Dict):
    """バックグラウンド検索タスク"""
    query = query_data.get('query', '')
    user_data_dict = query_data.get('user_data', {})
    
    clean_query = re.sub(r'(について|を|って|とは|調べて|検索して|教えて|探して|何|？|\?)', '', query).strip() or query
    
    normalized_query = knowledge_base.normalize_query(query)
    holomem_manager.load_from_db()
    detected = holomem_manager.detect_in_message(normalized_query)
    
    reference_info = ""
    if detected:
        logger.info(f"🎀 検索対象ホロメン: {detected}")
        ctx = get_holomem_context(detected)
        if ctx:
            reference_info += ctx + "\n"
        clean_query = f"{clean_query} ホロライブ VTuber"
    
    result_text = f"「{query}」について調べたけど、見つからなかったや…ごめんね！"
    
    try:
        results = scrape_major_search_engines(clean_query, 5)
        if results:
            reference_info += "【Web検索結果】\n" + "\n".join([f"{i+1}. {r['title']}: {r['snippet']}" for i, r in enumerate(results)])
            user_data = UserData(uuid=user_data_dict.get('uuid', ''), name=user_data_dict.get('name', 'Guest'), interaction_count=0)
            with get_db_session() as session:
                history = get_conversation_history(session, user_data.uuid)
            result_text = generate_ai_response_safe(user_data, query, history, reference_info=reference_info, is_detailed=True, is_task_report=True)
    except Exception as e:
        logger.error(f"❌ 検索エラー: {e}")
    
    with get_db_session() as session:
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = result_text
            task.status = 'completed'
            task.completed_at = datetime.utcnow()

# ==============================================================================
# 音声ファイル (VOICEVOX)
# ==============================================================================
def find_active_voicevox_url() -> Optional[str]:
    urls = [VOICEVOX_URL_FROM_ENV] + VOICEVOX_URLS
    for url in set(u for u in urls if u):
        try:
            if requests.get(f"{url}/version", timeout=2).status_code == 200:
                global_state.active_voicevox_url = url
                return url
        except: pass
    return None

def generate_voice_file(text: str, user_uuid: str) -> Optional[str]:
    if not global_state.voicevox_enabled or not global_state.active_voicevox_url: return None
    try:
        url = global_state.active_voicevox_url
        q = requests.post(f"{url}/audio_query", params={"text": text[:200], "speaker": VOICEVOX_SPEAKER_ID}, timeout=10).json()
        w = requests.post(f"{url}/synthesis", params={"speaker": VOICEVOX_SPEAKER_ID}, json=q, timeout=20).content
        fname = f"voice_{user_uuid[:8]}_{int(time.time())}.wav"
        with open(os.path.join(VOICE_DIR, fname), 'wb') as f: f.write(w)
        return fname
    except: return None

def cleanup_old_voice_files():
    try:
        cutoff = time.time() - (VOICE_FILE_MAX_AGE_HOURS * 3600)
        for f in glob.glob(os.path.join(VOICE_DIR, "voice_*.wav")):
            if os.path.getmtime(f) < cutoff: os.remove(f)
    except: pass

# ==============================================================================
# 初期データの移行関数
# ==============================================================================
def initialize_knowledge_db():
    with get_db_session() as session:
        try:
            if session.query(HolomemNickname).count() == 0:
                logger.info("📥 Migrating nicknames to database...")
                initial_nicknames = {
                    'みこち': 'さくらみこ', 'すいちゃん': '星街すいせい', 'フブちゃん': '白上フブキ',
                    'まつり': '夏色まつり', 'あくたん': '湊あくあ', 'スバル': '大空スバル',
                    'おかゆ': '猫又おかゆ', 'おかゆん': '猫又おかゆ', 'ころさん': '戌神ころね',
                    'ぺこちゃん': '兎田ぺこら', '団長': '白銀ノエル', '船長': '宝鐘マリン',
                    'かなたん': '天音かなた', 'わため': '角巻わため', 'トワ様': '常闇トワ',
                    'ルーナ': '姫森ルーナ', 'ラプ様': 'ラプラス・ダークネス', 'こよ': '博衣こより',
                    'ござる': '風真いろは', 'カリ': '森カリオペ', 'ぐら': 'がうる・ぐら',
                    'YAGOO': '谷郷元昭', 'そらちゃん': 'ときのそら', 'ちょこ先': '癒月ちょこ',
                    'ルイ姉': '鷹嶺ルイ', '沙花叉': '沙花叉クロヱ', 'アメ': 'ワトソン・アメリア',
                    'イナ': '一伊那尓栖', 'キアラ': '小鳥遊キアラ',
                    'ココ会長': '桐生ココ'
                }
                for nick, full in initial_nicknames.items():
                    session.add(HolomemNickname(nickname=nick, fullname=full))
                logger.info(f"✅ Nicknames initialized: {len(initial_nicknames)}")

            if session.query(HololiveGlossary).count() == 0:
                logger.info("📥 Migrating glossary to database...")
                initial_glossary = {
                    '生スバル': '大空スバルの行う雑談配信の枠名。通常夜に行われる。',
                    'おはスバ': '大空スバルの「おはようスバル」という朝配信のこと。',
                    'スバ友': '大空スバルのファンの愛称。',
                    'エリート': 'さくらみこの自称。実際はポンコツな言動が多いことへの愛称。',
                    '全ロス': 'マインクラフトなどでアイテムを全て失うこと。',
                    'ASMR': '音フェチ配信のこと。',
                    '野うさぎ': '兎田ぺこらのファンの愛称。',
                    '35P': 'さくらみこのファンの愛称。「みこぴー」と読む。',
                    '宝鐘海賊団': '宝鐘マリンのファンの総称。',
                    'kson': '元ホロライブの桐生ココの「中の人」と言われている個人勢VTuber。総長。',
                    'VShojo': 'アメリカ発のVTuberエージェンシー。ksonなどが所属していた。'
                }
                for term, desc in initial_glossary.items():
                    session.add(HololiveGlossary(term=term, description=desc))
                logger.info(f"✅ Glossary initialized: {len(initial_glossary)}")

        except Exception as e:
            logger.error(f"❌ Knowledge DB initialization failed: {e}")

# ==============================================================================
# Flask エンドポイント
# ==============================================================================
@app.route('/health', methods=['GET'])
def health_check():
    return create_json_response({'status': 'ok', 'gemini': gemini_model is not None, 'groq': groq_client is not None, 'holomem_count': holomem_manager.get_member_count()})

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json
        if not data or 'uuid' not in data or 'message' not in data:
            return Response("必須パラメータ不足|", 400)
        
        user_uuid = sanitize_user_input(data['uuid'])
        user_name = sanitize_user_input(data.get('name', 'Guest'))
        message = sanitize_user_input(data['message'])
        generate_voice = data.get('voice', False)
        
        if not chat_rate_limiter.is_allowed(user_uuid):
            return Response("メッセージ送りすぎ～！|", 429)

        if message.strip() == "残トークン":
            msg = f"🦁 Gemini: {'稼働中' if gemini_model else '停止中'}\n" + groq_model_manager.get_status_report()
            msg += f"\n🎀 ホロメンDB: {holomem_manager.get_member_count()}名"
            return Response(f"{msg}|", 200)

        ai_text = ""
        is_task_started = False
        
        with get_db_session() as session:
            user_data = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            
            if is_explicit_search_request(message):
                tid = f"search_{user_uuid}_{int(time.time())}"
                qdata = {'query': message, 'user_data': {'uuid': user_data.uuid, 'name': user_data.name}}
                session.add(BackgroundTask(task_id=tid, user_uuid=user_uuid, task_type='search', query=json.dumps(qdata, ensure_ascii=False)))
                background_executor.submit(background_deep_search, tid, qdata)
                ai_text = "オッケー！ちょっとググってくるから待ってて！"
                is_task_started = True

            if not ai_text:
                holomem_resp = process_holomem_in_chat(message, user_data, history)
                if holomem_resp:
                    ai_text = holomem_resp
                    logger.info("🎀 ホロメン応答完了")
            
            if not ai_text:
                if is_time_request(message):
                    ai_text = get_japan_time()
                elif is_weather_request(message):
                    ai_text = get_weather_forecast(extract_location(message))
            
            if not ai_text:
                ai_text = generate_ai_response_safe(user_data, message, history)
            
            if not is_task_started:
                session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))

        res_text = limit_text_for_sl(ai_text)
        v_url = ""
        if generate_voice and global_state.voicevox_enabled and not is_task_started:
            fname = generate_voice_file(res_text, user_uuid)
            if fname: v_url = f"{SERVER_URL}/play/{fname}"
            
        return Response(f"{res_text}|{v_url}", mimetype='text/plain; charset=utf-8', status=200)
    
    except Exception as e:
        logger.critical(f"🔥 エラー: {e}", exc_info=True)
        return Response("システムエラー…|", 500)

@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
    try:
        data = request.json
        if not data or 'uuid' not in data:
            return create_json_response({'error': 'uuid required'}, 400)
        with get_db_session() as session:
            task = session.query(BackgroundTask).filter(BackgroundTask.user_uuid == data['uuid'], BackgroundTask.status == 'completed').order_by(BackgroundTask.completed_at.desc()).first()
            if task:
                res = task.result or ""
                session.delete(task)
                session.add(ConversationHistory(user_uuid=data['uuid'], role='assistant', content=res))
                return create_json_response({'status': 'completed', 'response': f"{limit_text_for_sl(res)}|"})
        return create_json_response({'status': 'no_tasks'})
    except:
        return create_json_response({'error': 'internal error'}, 500)

@app.route('/play/<filename>', methods=['GET'])
def play_voice(filename: str):
    if not re.match(r'^voice_[a-zA-Z0-9_]+\.wav', filename):
        return Response("Invalid filename", 400)
    return send_from_directory(VOICE_DIR, filename)

# ==============================================================================
# 管理用エンドポイント
# ==============================================================================
@app.route('/admin/holomem', methods=['GET'])
def list_holomem():
    with get_db_session() as session:
        members = session.query(HolomemWiki).order_by(HolomemWiki.generation, HolomemWiki.member_name).all()
        return create_json_response([{'id': m.id, 'name': m.member_name, 'generation': m.generation, 'status': m.status, 'description': m.description} for m in members])

@app.route('/admin/holomem/<int:id>', methods=['PUT'])
def update_holomem(id: int):
    data = request.json
    with get_db_session() as session:
        member = session.query(HolomemWiki).get(id)
        if member:
            for key in ['description', 'generation', 'tags', 'status', 'mochiko_feeling', 'debut_date', 'graduation_date']:
                if key in data:
                    setattr(member, key, data[key])
            clear_holomem_cache(member.member_name)
            holomem_manager.load_from_db(force=True)
            return create_json_response({'success': True})
    return create_json_response({'error': 'not found'}, 404)

@app.route('/admin/holomem/refresh', methods=['POST'])
def refresh_holomem():
    background_executor.submit(update_holomem_database)
    return create_json_response({'message': 'DB更新タスク開始'})

# ==============================================================================
# 初期化
# ==============================================================================
def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(60)

def initialize_app():
    global engine, Session, groq_client, gemini_model
    logger.info("🔧 初期化開始 (v33.1.1 ベース改)")
    
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        
        initialize_knowledge_db()
        knowledge_base.load_data()
        
        logger.info("✅ DB初期化完了")
    except Exception as e:
        logger.critical(f"🔥 DB初期化失敗: {e}")
    
    try:
        if GROQ_API_KEY:
            groq_client = Groq(api_key=GROQ_API_KEY)
            logger.info("✅ Groq初期化完了")
    except: pass
    
    try:
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')
            logger.info("✅ Gemini初期化完了")
    except: pass
    
    if find_active_voicevox_url():
        global_state.voicevox_enabled = True
        logger.info("✅ VOICEVOX検出")
    
    logger.info("🎀 ホロメンシステム初期化...")
    if holomem_manager.load_from_db():
        logger.info(f"✅ ホロメン: {holomem_manager.get_member_count()}名ロード")
    if holomem_manager.get_member_count() == 0:
        logger.info("📡 DBが空のため初回収集実行")
        background_executor.submit(update_holomem_database)
    
    # ニュース初回収集
    background_executor.submit(fetch_hololive_news)

    # スケジュール設定
    schedule.every(6).hours.do(update_holomem_database)
    schedule.every(30).minutes.do(fetch_hololive_news) # 30分ごとにニュース更新
    schedule.every(1).hours.do(cleanup_old_voice_files)
    schedule.every(6).hours.do(chat_rate_limiter.cleanup_old_entries)
    
    threading.Thread(target=run_scheduler, daemon=True).start()
    cleanup_old_voice_files()
    
    logger.info("🚀 初期化完了!")

initialize_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
