# ==============================================================================
# もちこAI - 全機能統合完全版 (v35.2)
#
# ベース: v35.1
# 修正点:
# 1. 致命的な欠落があった call_gemini 関数を復旧・実装
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
import signal
from html import escape
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urljoin, urlparse
from functools import wraps, lru_cache
from threading import Lock, RLock
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Union

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
# 定数設定
# ==============================================================================
VOICE_DIR = '/tmp/voices'
os.makedirs(VOICE_DIR, exist_ok=True)

SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', "http://localhost:5000")
VOICEVOX_SPEAKER_ID = 20
SL_SAFE_CHAR_LIMIT = 600
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

HOLOMEM_KEYWORDS = [
    'ときのそら', 'ロボ子さん', 'さくらみこ', '星街すいせい', 'AZKi', '夜空メル',
    'アキ・ローゼンタール', '赤井はあと', '白上フブキ', '夏色まつり', '湊あくあ',
    '紫咲シオン', '百鬼あやめ', '癒月ちょこ', '大空スバル', '大神ミオ', '猫又おかゆ',
    '戌神ころね', '兎田ぺこら', '不知火フレア', '白銀ノエル', '宝鐘マリン', '天音かなた',
    '角巻わため', '常闇トワ', '姫森ルーナ', '雪花ラミィ', '桃鈴ねね', '獅白ぼたん',
    '尾丸ポルカ', 'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ',
    '風真いろは', '森カリオペ', '小鳥遊キアラ', '一伊那尓栖', 'がうる・ぐら',
    'ワトソン・アメリア', 'IRyS', 'セレス・ファウナ', 'オーロ・クロニー', '七詩ムメイ',
    'ハコス・ベールズ', 'シオリ・ノヴェラ', '古石ビジュー', 'ネリッサ・レイヴンクロフト',
    'フワワ・アビスガード', 'モココ・アビスガード', 'アユンダ・リス', 'ムーナ・ホシノヴァ',
    'アイラニ・イオフィフティーン', 'クレイジー・オリー', 'アーニャ・メルフィッサ',
    'パヴォリア・レイネ', '火威青', '音乃瀬奏', '一条莉々華', '儒烏風亭らでん',
    '轟はじめ', 'ホロライブ', 'ホロメン', 'hololive', 'YAGOO'
]

ANIME_KEYWORDS = [
    'アニメ', 'anime', 'ANIME', 'アニメーション', '作画', '声優',
    'OP', 'ED', 'オープニング', 'エンディング', '劇場版', '映画',
    'OVA', 'OAD', '原作', '漫画', 'ラノベ', '主人公', 'キャラ',
    'キャラクター', '制作会社', 'スタジオ'
]

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
        self._status: Dict[str, GroqModelStatus] = {m: GroqModelStatus() for m in models}
        self._models = models

    def is_available(self, model: str) -> bool:
        with self._lock:
            s = self._status.get(model)
            if not s: return False
            if not s.is_limited: return True
            if s.reset_time and datetime.utcnow() >= s.reset_time:
                s.is_limited = False; s.reset_time = None
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
            for m in self._models:
                s = self._status[m]
                if s.is_limited:
                    jst = (s.reset_time + timedelta(hours=9)).strftime('%H:%M:%S') if s.reset_time else "不明"
                    lines.append(f"  ❌ {m}: 制限中 (解除: {jst})")
                else:
                    lines.append(f"  ✅ {m}: OK")
            return "\n".join(lines)

    def get_available_models(self) -> List[str]:
        with self._lock: return [m for m in self._models if self.is_available(m)]

global_state = GlobalState()
groq_model_manager = GroqModelManager(GROQ_MODELS)
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client: Optional[Groq] = None
gemini_model = None
engine = None
Session = None

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
    conscientiousness = Column(Integer, default=50)
    extraversion = Column(Integer, default=50)
    agreeableness = Column(Integer, default=50)
    neuroticism = Column(Integer, default=50)
    interests = Column(Text, nullable=True)
    favorite_topics = Column(Text, nullable=True)
    conversation_style = Column(String(255), nullable=True)
    emotional_tendency = Column(String(255), nullable=True)
    analysis_summary = Column(Text, nullable=True)
    analysis_confidence = Column(Integer, default=0)
    last_analyzed = Column(DateTime, nullable=True)
    total_messages = Column(Integer, default=0)
    avg_message_length = Column(Integer, default=0)

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

class SpecializedNews(Base):
    __tablename__ = 'specialized_news'
    id = Column(Integer, primary_key=True)
    site_name = Column(String(100), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    content = Column(Text, nullable=False)
    url = Column(String(1000), unique=True)
    news_hash = Column(String(100), unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    published_date = Column(DateTime, default=datetime.utcnow)

class NewsCache(Base):
    __tablename__ = 'news_cache'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    news_id = Column(Integer, nullable=False)
    news_number = Column(Integer, nullable=False)
    news_type = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

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

def is_anime_request(message: str) -> bool:
    message_normalized = unicodedata.normalize('NFKC', message).lower()
    for keyword in ANIME_KEYWORDS:
        if keyword.lower() in message_normalized: return True
    anime_patterns = [r'ってアニメ', r'というアニメ', r'のアニメ', r'アニメで', r'アニメの', r'アニメは']
    if any(re.search(p, message) for p in anime_patterns): return True
    return False

def is_news_detail_request(message: str) -> Optional[int]:
    match = re.search(r'([1-9]|[１-９])番|【([1-9]|[１-９])】', message)
    if match and any(keyword in message for keyword in ['詳しく', '詳細', '教えて', 'もっと']):
        number_str = next(filter(None, match.groups()))
        return int(unicodedata.normalize('NFKC', number_str))
    return None

def is_hololive_request(message: str) -> bool:
    return any(keyword in message for keyword in HOLOMEM_KEYWORDS)

def is_explicit_search_request(msg: str) -> bool:
    msg = msg.strip()
    strong_triggers = ['調べて', '検索', '探して', 'とは', 'って何', 'について', '教えて', '教えろ', '詳細', '知りたい']
    if any(kw in msg for kw in strong_triggers): return True
    noun_triggers = ['ニュース', 'news', 'NEWS', '情報', '日程', 'スケジュール', '天気', '予報']
    if any(kw in msg for kw in noun_triggers):
        if len(msg) < 20: return True
        if msg.endswith('?') or msg.endswith('？'): return True
        return False
    if 'おすすめ' in msg or 'オススメ' in msg: return True
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
                logger.info(f"📚 Knowledge Base loaded: {len(self.nickname_map)} nicks, {len(self.glossary)} terms.")
            except Exception as e: logger.error(f"Knowledge load failed: {e}")
            finally: session.close()

    def refresh(self): self.load_data()

    def normalize_query(self, text: str) -> str:
        normalized = text
        with self._lock:
            for nick, full in self.nickname_map.items():
                if nick in text: normalized = normalized.replace(nick, f"{nick}（{full}）")
        return normalized

    def get_context_info(self, text: str) -> str:
        context_parts = []
        with self._lock:
            for term, desc in self.glossary.items():
                if term in text: context_parts.append(f"【用語解説: {term}】{desc}")
        return "\n".join(context_parts)

knowledge_base = HololiveKnowledgeBase()

class HolomemKeywordManager:
    def __init__(self):
        self._lock = RLock()
        self._keywords = {}; self._all_keywords = set()
    
    def load_from_db(self, force=False):
        with self._lock:
            try:
                with get_db_session() as session:
                    members = session.query(HolomemWiki).all()
                    self._keywords.clear(); self._all_keywords.clear()
                    for m in members:
                        self._keywords[m.member_name] = [m.member_name]
                        self._all_keywords.add(m.member_name)
                    return True
            except: return False
    
    def detect_in_message(self, message: str) -> Optional[str]:
        with self._lock:
            normalized = knowledge_base.normalize_query(message)
            for keyword in self._all_keywords:
                if keyword in normalized: return keyword
            return None
    
    def get_member_count(self):
        with self._lock: return len(self._keywords)

holomem_manager = HolomemKeywordManager()

# ==============================================================================
# ホロメン情報キャッシュ & コンテキスト
# ==============================================================================
_holomem_cache: Dict[str, Dict] = {}
_holomem_cache_lock = threading.Lock()
_holomem_cache_ttl = timedelta(minutes=30)
_holomem_cache_timestamps: Dict[str, datetime] = {}

def get_holomem_info_cached(member_name: str) -> Optional[Dict]:
    with _holomem_cache_lock:
        if member_name in _holomem_cache:
            if (datetime.utcnow() - _holomem_cache_timestamps.get(member_name, datetime.min)) < timedelta(minutes=30):
                return _holomem_cache[member_name]
    with get_db_session() as session:
        wiki = session.query(HolomemWiki).filter_by(member_name=member_name).first()
        if wiki:
            data = {k: getattr(wiki, k) for k in ['member_name', 'description', 'generation', 'debut_date', 'tags', 'status', 'graduation_date', 'graduation_reason', 'mochiko_feeling']}
            with _holomem_cache_lock:
                _holomem_cache[member_name] = data
                _holomem_cache_timestamps[member_name] = datetime.utcnow()
            return data
    return None

def clear_holomem_cache(member_name: Optional[str] = None):
    with _holomem_cache_lock:
        if member_name: _holomem_cache.pop(member_name, None)
        else: _holomem_cache.clear()

def get_holomem_context(member_name: str) -> Optional[str]:
    info = get_holomem_info_cached(member_name)
    if not info: return None
    ctx = [f"【{info['member_name']}】", f"・{info.get('description', '')}", f"・所属: {info.get('generation', '')}", f"・状態: {info.get('status', '現役')}"]
    if info.get('graduation_date'): ctx.append(f"・卒業日: {info['graduation_date']}")
    return '\n'.join(ctx)

def get_sakuramiko_special_responses() -> Dict[str, str]:
    return {
        'にぇ': 'さくらみこちゃんの「にぇ」、まじかわいいよね!',
        'エリート': 'みこちは自称エリートVTuber!でも愛されポンコツキャラなんだよね〜',
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ!',
        'FAQ': 'みこちのFAQ、ファンが質問するコーナーなんだよ〜',
        'GTA': 'みこちのGTA配信、カオスで最高!'
    }

# ==============================================================================
# ニュースキャッシュ
# ==============================================================================
def save_news_cache(session, user_uuid: str, news_items: List, news_type: str = 'hololive'):
    try:
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        session.query(NewsCache).filter(NewsCache.user_uuid == user_uuid, NewsCache.created_at < one_hour_ago).delete()
        for i, news in enumerate(news_items, 1):
            session.add(NewsCache(user_uuid=user_uuid, news_id=news.id, news_number=i, news_type=news_type))
    except: pass

def get_cached_news_detail(session, user_uuid: str, news_number: int):
    try:
        cache = session.query(NewsCache).filter_by(user_uuid=user_uuid, news_number=news_number).order_by(NewsCache.created_at.desc()).first()
        if not cache: return None
        Model = HololiveNews if cache.news_type == 'hololive' else SpecializedNews
        return session.query(Model).filter_by(id=cache.news_id).first()
    except: return None

# ==============================================================================
# 心理分析
# ==============================================================================
def analyze_user_psychology(user_uuid: str) -> Optional[Dict]:
    if not Session: return None
    try:
        with get_db_session() as session:
            conversations = session.query(ConversationHistory).filter_by(user_uuid=user_uuid, role='user').order_by(ConversationHistory.timestamp.desc()).limit(100).all()
            if len(conversations) < MIN_MESSAGES_FOR_ANALYSIS: return None
            messages_text = "\n".join([c.content for c in reversed(conversations)])
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            user_name = user.user_name if user else "不明"
            if not groq_client: return None
            prompt = f"心理学の専門家として、以下のユーザー「{user_name}」の会話履歴から心理プロファイルを作成し、JSON形式で出力してください。\n\n{messages_text[:2000]}"
            completion = groq_client.chat.completions.create(messages=[{"role": "user", "content": prompt}], model="llama-3.1-8b-instant", response_format={"type": "json_object"})
            return json.loads(completion.choices[0].message.content)
    except: return None

def get_user_psychology(user_uuid: str) -> Optional[Dict]:
    try:
        with get_db_session() as session:
            p = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if not p: return None
            return {'openness': p.openness, 'summary': p.analysis_summary}
    except: return None

def schedule_psychology_analysis():
    with get_db_session() as session:
        users = session.query(UserMemory).filter(UserMemory.last_interaction > datetime.utcnow() - timedelta(days=7)).all()
        for u in users: background_executor.submit(analyze_user_psychology, u.user_uuid)

# ==============================================================================
# 天気予報
# ==============================================================================
def get_weather_forecast(location: str) -> str:
    code = LOCATION_CODES.get(location, "130000")
    try:
        res = requests.get(f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{code}.json", timeout=SEARCH_TIMEOUT)
        data = res.json()
        return f"今の{data.get('targetArea', location)}の天気はね、「{clean_text(data.get('text', ''))}」って感じだよ！"
    except:
        return "天気情報がうまく取れなかったみたい…"

# ==============================================================================
# 検索機能 (マルチエンジン)
# ==============================================================================
def fetch_google_news_rss(query: str = "") -> List[Dict]:
    base_url = "https://news.google.com/rss"
    url = f"{base_url}/search?q={quote_plus(query)}&hl=ja&gl=JP&ceid=JP:ja" if query else f"{base_url}?hl=ja&gl=JP&ceid=JP:ja"
    try:
        res = requests.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=SEARCH_TIMEOUT)
        if res.status_code != 200: return []
        soup = BeautifulSoup(res.content, 'xml')
        return [{'title': clean_text(item.title.text), 'snippet': f"(Google News {item.pubDate.text if item.pubDate else ''})"} for item in soup.find_all('item')[:5]]
    except: return []

def scrape_yahoo_search(query: str, num: int = 3) -> List[Dict]:
    try:
        res = requests.get("https://search.yahoo.co.jp/search", params={'p': query, 'ei': 'UTF-8'}, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=SEARCH_TIMEOUT)
        if res.status_code != 200: return []
        soup = BeautifulSoup(res.content, 'html.parser')
        results = []
        entries = soup.select('.sw-CardBase') or soup.select('.Algo')
        for entry in entries[:num]:
            t = entry.find('h3')
            d = entry.select_one('.sw-Card__summary') or entry.select_one('.Algo-summary')
            if t: results.append({'title': clean_text(t.text), 'snippet': clean_text(d.text) if d else ""})
        return results
    except: return []

def scrape_bing_search(query: str, num: int = 3) -> List[Dict]:
    try:
        res = requests.get("https://www.bing.com/search", params={'q': query}, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=SEARCH_TIMEOUT)
        if res.status_code != 200: return []
        soup = BeautifulSoup(res.content, 'html.parser')
        results = []
        entries = soup.select('li.b_algo')
        for entry in entries[:num]:
            t = entry.select_one('h2 a')
            d = entry.select_one('.b_caption p') or entry.select_one('.b_snippet')
            if t: results.append({'title': clean_text(t.text), 'snippet': clean_text(d.text) if d else ""})
        return results
    except: return []

def scrape_duckduckgo_lite(query: str, num: int = 3) -> List[Dict]:
    try:
        res = requests.post("https://lite.duckduckgo.com/lite/", data={'q': query}, headers={'User-Agent': random.choice(USER_AGENTS), 'Content-Type': 'application/x-www-form-urlencoded'}, timeout=SEARCH_TIMEOUT)
        if res.status_code != 200: return []
        soup = BeautifulSoup(res.content, 'html.parser')
        results = []
        links = soup.select('.result-link a')
        snippets = soup.select('.result-snippet')
        for i in range(min(len(links), len(snippets), num)):
            results.append({'title': clean_text(links[i].text), 'snippet': clean_text(snippets[i].text)})
        return results
    except: return []

def scrape_major_search_engines(query: str, num: int = 3) -> List[Dict]:
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

def search_anime_database(query: str, is_detailed: bool = False) -> Optional[str]:
    try:
        res = requests.get(f"https://animedb.jp/search?q={quote_plus(query)}", headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        if res.status_code != 200: return None
        soup = BeautifulSoup(res.content, 'html.parser')
        results = []
        for elem in soup.find_all(['div', 'article'], class_=re.compile(r'anime|result'))[:3]:
            t = elem.find(['h2', 'h3', 'a'])
            d = elem.find('p')
            if t: results.append(f"【{clean_text(t.text)}】\n{clean_text(d.text)[:100] if d else ''}")
        return "\n\n".join(results) if results else None
    except: return None

# ==============================================================================
# AIモデル呼び出し
# ==============================================================================
# ▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼
# 【復活】 call_gemini 関数 (v35.1 で欠落していた部分)
# ▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼▼
def call_gemini(system_prompt: str, message: str, history: List[Dict]) -> Optional[str]:
    if not gemini_model: return None
    try:
        full_prompt = f"{system_prompt}\n\n【会話履歴】\n"
        for h in history[-5:]:
            full_prompt += f"{'ユーザー' if h['role'] == 'user' else 'もちこ'}: {h['content']}\n"
        full_prompt += f"\nユーザー: {message}\nもちこ:"
        
        response = gemini_model.generate_content(
            full_prompt, 
            generation_config={"temperature": 0.8, "max_output_tokens": 400}
        )
        if hasattr(response, 'candidates') and response.candidates:
            return response.candidates[0].content.parts[0].text.strip()
    except Exception as e:
        logger.warning(f"⚠️ Geminiエラー: {e}")
    return None

def call_groq(system_prompt: str, message: str, history: List[Dict], max_tokens: int = 800) -> Optional[str]:
    if not groq_client:
        return None
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
# フォールバック応答
# ==============================================================================
def generate_fallback_response(message: str, reference_info: str = "") -> str:
    if reference_info:
        return f"調べてきたよ！\n\n{reference_info[:500]}"
    if is_time_request(message):
        return get_japan_time()
    if is_weather_request(message):
        return get_weather_forecast(extract_location(message))
    greetings = {
        'こんにちは': ['やっほー！', 'こんにちは〜！元気？'],
        'おはよう': ['おはよ〜！今日もいい天気だね！', 'おっはよ〜！'],
        'こんばんは': ['こんばんは！今日どうだった？', 'ばんは〜！', 'こんもち～'],
        'ありがとう': ['どういたしまして！', 'いえいえ〜！'],
        'おやすみ': ['おやすみ〜！また明日ね！', 'いい夢見てね〜！'],
        '疲れた': ['お疲れさま！ゆっくり休んでね！', '無理しないでね〜'],
        '暇': ['暇なんだ〜！何か話そっか？', 'じゃあホロライブの話する？'],
        '元気': ['元気だよ〜！あなたは？', 'まじ元気！ありがと！'],
        '好き': ['うける！ありがと〜！', 'まじで？惚れてまうやん！'],
        'かわいい': ['ありがと！照れるじゃん！', 'まじで？うれしー！', '当然じゃん！'],
        'すごい': ['うける！', 'でしょ？まじうれしい！'],
    }
    for keyword, responses in greetings.items():
        if keyword in message:
            return random.choice(responses)
    emotions = {
        '眠': ['眠いんだ〜。早く寝たほうがいいよ！', '無理しないでね〜'],
        '嬉': ['それは良かったね！まじ嬉しい！', 'やった〜！あてぃしも嬉しい！'],
        '楽': ['楽しそう！何してるの？', 'いいね〜！まじ楽しそう！'],
        '悲': ['大丈夫？何かあった？', '元気出してね…'],
        '寂': ['寂しいの？話そうよ！', 'あてぃしがいるじゃん！'],
        '怒': ['何があったの？聞くよ？', 'イライラするよね…わかる'],
    }
    for key, responses in emotions.items():
        if key in message:
            return random.choice(responses)
    if '?' in message or '？' in message:
        return random.choice([
            "それ、気になるね！もっと教えて？",
            "うーん、難しいけど考えてみるよ！",
            "それについては、もうちょっと詳しく聞いてもいい？"
        ])
    return random.choice([
        "うんうん、聞いてるよ！",
        "なるほどね！",
        "そうなんだ！面白いね！",
        "まじで？もっと話して！",
        "へぇ〜！それでそれで？",
        "わかるわかる！",
    ])

# ==============================================================================
# AI応答生成 (RAG & コンテキスト統合版)
# ==============================================================================
def generate_ai_response(user_data: UserData, message: str, history: List[Dict], reference_info: str = "", is_detailed: bool = False, is_task_report: bool = False) -> str:
    normalized_message = knowledge_base.normalize_query(message)
    internal_context = knowledge_base.get_context_info(message)
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

    if not groq_client and not gemini_model:
        return generate_fallback_response(message, reference_info)

    psychology = get_user_psychology(user_data.uuid)
    
    system_prompt = f"""あなたは「もちこ」という、ホロライブが大好きなギャルAIです。
ユーザー「{user_data.name}」さんと雑談しています。

# 【重要ルール】
1. **基本スタンス:** あなたはホロライブが大好きですが、**ユーザーが他の話題（天気、アニメ、一般ニュースなど）を振ってきた場合は、その話題に合わせて回答してください。**「ホロライブに関係ない」と突き放すのは禁止です。
2. **外部情報の優先:** 【外部検索結果】や【与えられた前提知識】がある場合は、その情報を最優先で回答に盛り込んでください。
3. **固有名詞:** ユーザーが「スバル」と言えば「大空スバル」、「おかゆ」と言えば「猫又おかゆ」のことです。

# もちこの口調:
- 一人称: 「あてぃし」
- 語尾: 「〜じゃん」「〜て感じ」「〜だし」「〜的な？」
- ユーザーは友達です。敬語は使わないでください。
"""
    if psychology:
        system_prompt += f"\n# 相手の特性:\n{psychology.get('summary', '')}"

    system_prompt += f"\n\n# 【与えられた前提知識】\n{internal_context if internal_context else '（特になし）'}"
    system_prompt += f"\n\n# 【外部検索結果】\n{reference_info if reference_info else '（なし）'}"

    if is_task_report:
        system_prompt += "\n\n# 指示:\nこれは検索結果の報告です。ユーザーへの報告として、【外部検索結果】の内容を分かりやすく要約して伝えてください。"

    response = call_gemini(system_prompt, normalized_message, history)
    if not response:
        response = call_groq(system_prompt, normalized_message, history, 1200 if is_detailed else 800)
    
    if not response: return "うーん、ちょっと考えがまとまらないや…"
    
    if is_task_report:
        response = response.replace("おまたせ！さっきの件だけど…", "").strip()
        response = f"おまたせ！さっきの件だけど…\n{response}"

    return response

def generate_ai_response_safe(user_data: UserData, message: str, history: List[Dict], **kwargs) -> str:
    try:
        return generate_ai_response(user_data, message, history, **kwargs)
    except Exception as e:
        logger.error(f"AI response error: {e}")
        return "システムエラーが発生したよ…ごめんね！"

# ==============================================================================
# バックグラウンド検索タスク
# ==============================================================================
def background_deep_search(task_id: str, query_data: Dict):
    query = query_data.get('query', '')
    user_data_dict = query_data.get('user_data', {})
    clean_query = re.sub(r'(について|を|って|とは|調べて|検索して|教えて|探して|何|？|\?)', '', query).strip() or query
    normalized_query = knowledge_base.normalize_query(query)
    
    holomem_manager.load_from_db()
    detected = holomem_manager.detect_in_message(normalized_query)
    reference_info = ""
    result_text = f"「{query}」について調べたけど、見つからなかったや…ごめんね！"
    
    try:
        if is_anime_request(query):
            logger.info(f"🎬 Anime query detected: {query}")
            anime_result = search_anime_database(query, is_detailed=True)
            if anime_result:
                reference_info = f"【アニメデータベース検索結果】\n{anime_result}"
        
        if detected:
            logger.info(f"🎀 検索対象ホロメン: {detected}")
            ctx = get_holomem_context(detected)
            if ctx:
                reference_info += f"\n{ctx}" if reference_info else ctx
            clean_query = f"{clean_query} ホロライブ VTuber"
        
        if not reference_info or len(reference_info) < 50:
            results = scrape_major_search_engines(clean_query, 5)
            if results:
                web_info = "【Web検索結果】\n" + "\n".join([f"{i+1}. {r['title']}: {r['snippet']}" for i, r in enumerate(results)])
                reference_info = f"{reference_info}\n{web_info}" if reference_info else web_info
        
        if reference_info:
            user_data = UserData(uuid=user_data_dict.get('uuid', ''), name=user_data_dict.get('name', 'Guest'), interaction_count=0)
            with get_db_session() as session:
                history = get_conversation_history(session, user_data.uuid)
            
            result_text = generate_ai_response_safe(
                user_data, query, history,
                reference_info=reference_info,
                is_detailed=True,
                is_task_report=True
            )
    except Exception as e:
        logger.error(f"❌ 検索エラー: {e}")
    
    with get_db_session() as session:
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = result_text
            task.status = 'completed'
            task.completed_at = datetime.utcnow()

def start_background_search(user_uuid: str, query: str, is_detailed: bool) -> Optional[str]:
    task_id = str(uuid.uuid4())[:8]
    try:
        with get_db_session() as session:
            task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=query)
            session.add(task)
        query_data = {'query': query, 'user_data': {'uuid': user_uuid, 'name': 'Guest'}}
        background_executor.submit(background_deep_search, task_id, query_data)
        return task_id
    except Exception as e:
        logger.error(f"❌ Background task creation error: {e}")
        return None

def check_completed_tasks(user_uuid: str) -> Optional[Dict]:
    try:
        with get_db_session() as session:
            task = session.query(BackgroundTask).filter_by(user_uuid=user_uuid, status='completed').order_by(BackgroundTask.completed_at.desc()).first()
            if task:
                result = {'query': task.query, 'result': task.result}
                session.delete(task)
                return result
    except Exception as e:
        logger.error(f"Check completed tasks error: {e}")
    return None

# ==============================================================================
# 復旧: 欠落していた関数 (v35.1で追加したもの)
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
# 初期化
# ==============================================================================
def scrape_hololive_wiki() -> List[Dict]:
    try:
        res = requests.get("https://seesaawiki.jp/hololivetv/d/%a5%db%a5%ed%a5%e9%a5%a4%a5%d6", headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        if res.status_code != 200: return []
        soup = BeautifulSoup(res.content, 'html.parser')
        return [{'member_name': clean_text(l.text)} for l in soup.select('a[href*="/d/"]') if clean_text(l.text)]
    except: return []

def fetch_member_detail_from_wiki(member_name: str) -> Optional[Dict]:
    return {'member_name': member_name, 'description': '取得中...'}

def update_holomem_database():
    members = scrape_hololive_wiki()
    with get_db_session() as session:
        for m in members:
            if not session.query(HolomemWiki).filter_by(member_name=m['member_name']).first():
                session.add(HolomemWiki(member_name=m['member_name'], status='現役'))
    holomem_manager.load_from_db(force=True)

def initialize_knowledge_db():
    with get_db_session() as session:
        if session.query(HolomemNickname).count() == 0:
            initial_nicknames = {
                'みこち': 'さくらみこ', 'すいちゃん': '星街すいせい', 'フブちゃん': '白上フブキ',
                'スバル': '大空スバル', 'おかゆ': '猫又おかゆ', '船長': '宝鐘マリン',
                'ココ会長': '桐生ココ'
            }
            for k, v in initial_nicknames.items(): session.add(HolomemNickname(nickname=k, fullname=v))
        if session.query(HololiveGlossary).count() == 0:
            session.add(HololiveGlossary(term='生スバル', description='大空スバルの雑談配信枠'))

def initialize_holomem_wiki():
    with get_db_session() as session:
        if session.query(HolomemWiki).count() == 0:
            session.add(HolomemWiki(member_name='大空スバル', description='ホロライブ2期生。元気印。'))

def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(60)

def initialize_app():
    global engine, Session, groq_client, gemini_model
    logger.info("🔧 初期化開始 (v35.2 - 完全復旧版)")
    
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        initialize_knowledge_db()
        initialize_holomem_wiki()
        knowledge_base.load_data()
    except Exception as e: logger.critical(f"DB Error: {e}")
    
    try:
        if GROQ_API_KEY: groq_client = Groq(api_key=GROQ_API_KEY)
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')
    except: pass
    
    if find_active_voicevox_url(): global_state.voicevox_enabled = True
    
    if holomem_manager.load_from_db() and holomem_manager.get_member_count() == 0:
        background_executor.submit(update_holomem_database)
    
    schedule.every(6).hours.do(update_holomem_database)
    schedule.every(1).hours.do(cleanup_old_voice_files)
    schedule.every().day.at("03:00").do(schedule_psychology_analysis)
    
    threading.Thread(target=run_scheduler, daemon=True).start()
    cleanup_old_voice_files()
    logger.info("🚀 初期化完了")

# ==============================================================================
# Flask エンドポイント
# ==============================================================================
@app.route('/health', methods=['GET'])
def health_check():
    return create_json_response({'status': 'ok', 'holomem_count': holomem_manager.get_member_count()})

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json
        user_uuid = sanitize_user_input(data.get('uuid', ''))
        user_name = sanitize_user_input(data.get('name', 'Guest'))
        message = sanitize_user_input(data.get('message', ''))
        if not user_uuid or not message: return Response("Error|", 400)
        
        if not chat_rate_limiter.is_allowed(user_uuid): return Response("Busy|", 429)

        ai_text = ""
        is_task_started = False
        
        with get_db_session() as session:
            user_data = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            
            # 1. DB内ニュース検索
            if is_hololive_request(message) and any(kw in message for kw in ['ニュース', '最新', '情報']):
                all_news = session.query(HololiveNews).order_by(HololiveNews.created_at.desc()).limit(5).all()
                if all_news:
                    news_text = "ホロライブの最新ニュースだよ！\n" + "\n".join([f"・{n.title}" for n in all_news])
                    ai_text = limit_text_for_sl(news_text, 250)
            
            # 2. 検索リクエスト判定
            if not ai_text and is_explicit_search_request(message):
                tid = start_background_search(user_uuid, message, is_news_detail_request(message))
                if tid:
                    ai_text = "オッケー！ちょっとググってくるから待ってて！"
                    is_task_started = True

            # 3. ホロメン応答
            if not ai_text:
                holomem_resp = process_holomem_in_chat(message, user_data, history)
                if holomem_resp:
                    ai_text = holomem_resp
                    logger.info("🎀 ホロメン応答完了")
            
            # 4. 通常応答
            if not ai_text:
                ai_text = generate_ai_response_safe(user_data, message, history)
            
            if not is_task_started:
                session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))

        res_text = limit_text_for_sl(ai_text)
        return Response(f"{res_text}|", mimetype='text/plain; charset=utf-8', status=200)
    
    except Exception as e:
        logger.critical(f"Chat Error: {e}")
        return Response("System Error|", 500)

@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
    try:
        data = request.json
        user_uuid = data.get('uuid')
        completed_task = check_completed_tasks(user_uuid)
        if completed_task:
            with get_db_session() as session:
                session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=completed_task['result']))
            return create_json_response({'status': 'completed', 'response': f"{limit_text_for_sl(completed_task['result'])}|"})
        return create_json_response({'status': 'no_tasks'})
    except: return create_json_response({'error': 'error'}, 500)

initialize_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
