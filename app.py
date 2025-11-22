# ==============================================================================
# もちこAI - 全機能統合完全版 (v34.1)
#
# ベース: v34.0.0
# 修正点:
# 1. 欠落していたDBモデル (NewsCache, SpecializedNews) を追加
# 2. 欠落していた判定関数 (is_anime_request, is_news_detail_request) を追加
# 3. 全機能（心理分析、アニメ検索、ニュースキャッシュ、天気、RAG）の連携確認
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
VOICE_OPTIMAL_LENGTH = 150

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

LOCATION_CODES = {
    "東京": "130000", "大阪": "270000", "名古屋": "230000",
    "福岡": "400000", "札幌": "016000"
}

VOICEVOX_URLS = [
    'http://voicevox-engine:50021', 'http://voicevox:50021',
    'http://127.0.0.1:50021', 'http://localhost:50021'
]

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

SPECIALIZED_SITES = {
    'Blender': {
        'base_url': 'https://docs.blender.org/manual/ja/latest/',
        'keywords': ['Blender', 'ブレンダー', 'blender', 'BLENDER']
    },
    'CGニュース': {
        'base_url': 'https://modelinghappy.com/',
        'keywords': ['CGニュース', '3DCG', 'CG', 'cg', '3dcg', 'CGアニメ']
    },
    '脳科学・心理学': {
        'base_url': 'https://nazology.kusuguru.co.jp/',
        'keywords': ['脳科学', '心理学', '脳', '心理']
    },
    'セカンドライフ': {
        'base_url': 'https://community.secondlife.com/news/',
        'keywords': ['セカンドライフ', 'Second Life', 'SL', 'SecondLife']
    },
    'アニメ': {
        'base_url': 'https://animedb.jp/',
        'keywords': ['アニメ', 'anime', 'ANIME']
    }
}

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
                s.is_limited = False
                s.reset_time = None
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

# ==============================================================================
# Flask アプリケーション
# ==============================================================================
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
    if env_value and env_value.strip():
        return env_value.strip()
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
    # 追加カラム（心理分析の詳細用）
    conscientiousness = Column(Integer, default=50)
    agreeableness = Column(Integer, default=50)
    neuroticism = Column(Integer, default=50)
    interests = Column(Text, nullable=True)
    conversation_style = Column(String(255), nullable=True)
    emotional_tendency = Column(String(255), nullable=True)
    analysis_summary = Column(Text, nullable=True)
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

# --- 欠落していたモデルを追加 ---
class NewsCache(Base):
    __tablename__ = 'news_cache'
    id = Column(Integer, primary_key=True)
    user_uuid = Column(String(255), nullable=False, index=True)
    news_id = Column(Integer, nullable=False)
    news_number = Column(Integer, nullable=False)
    news_type = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

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

# --- 欠落していた判定関数 ---
def is_anime_request(message: str) -> bool:
    """アニメ関連の質問かどうか判定"""
    message_normalized = unicodedata.normalize('NFKC', message).lower()
    # アニメキーワードが含まれているか
    for keyword in ANIME_KEYWORDS:
        if keyword.lower() in message_normalized:
            return True
    # 「〜ってアニメ」「〜というアニメ」などのパターン
    anime_patterns = [r'ってアニメ', r'というアニメ', r'のアニメ', r'アニメで', r'アニメの', r'アニメは']
    if any(re.search(p, message) for p in anime_patterns):
        return True
    return False

def is_news_detail_request(message: str) -> Optional[int]:
    """ニュース詳細リクエスト（例: 1番詳しく）の判定"""
    match = re.search(r'([1-9]|[１-９])番|【([1-9]|[１-９])】', message)
    if match and any(keyword in message for keyword in ['詳しく', '詳細', '教えて', 'もっと']):
        number_str = next(filter(None, match.groups()))
        return int(unicodedata.normalize('NFKC', number_str))
    return None

def is_explicit_search_request(msg: str) -> bool:
    """メッセージが検索要求かどうかを判定"""
    msg = msg.strip()
    # 1. 明確な「検索命令」動詞
    strong_triggers = ['調べて', '検索', '探して', 'とは', 'って何', 'について', '教えて', '教えろ', '詳細', '知りたい']
    if any(kw in msg for kw in strong_triggers):
        return True
    # 2. 名詞系トリガー（短い場合や疑問形のみ検索）
    noun_triggers = ['ニュース', 'news', 'NEWS', '情報', '日程', 'スケジュール', '天気', '予報']
    if any(kw in msg for kw in noun_triggers):
        if len(msg) < 20: return True
        if msg.endswith('?') or msg.endswith('？'): return True
        return False
    # 3. おすすめ
    if 'おすすめ' in msg or 'オススメ' in msg: return True
    return False

def extract_location(msg: str) -> str:
    for loc in LOCATION_CODES.keys():
        if loc in msg: return loc
    return "東京"

# ==============================================================================
# ユーザー管理
# ==============================================================================
def get_or_create_user(session, user_uuid: str, user_name: str) -> UserData:
    user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != user_name:
            user.user_name = user_name
    else:
        user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
        session.add(user)
    return UserData(uuid=user.user_uuid, name=user.user_name, interaction_count=user.interaction_count)

def get_conversation_history(session, user_uuid: str, limit: int = 10) -> List[Dict]:
    hist = session.query(ConversationHistory).filter_by(user_uuid=user_uuid).order_by(
        ConversationHistory.timestamp.desc()
    ).limit(limit).all()
    return [{'role': h.role, 'content': h.content} for h in reversed(hist)]

# ==============================================================================
# 天気予報取得
# ==============================================================================
def get_weather_forecast(location: str) -> str:
    area_code = LOCATION_CODES.get(location, "130000")
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{area_code}.json"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        text = clean_text(data.get('text', ''))
        if not text:
            return f"{location}の天気情報がちょっと取れなかった…"
        weather_text = f"今の{location}の天気はね、「{text}」って感じだよ！"
        return limit_text_for_sl(weather_text, 200)
    except requests.exceptions.Timeout:
        logger.warning(f"Weather API timeout for {location}")
        return "天気情報の取得がタイムアウトしちゃった…"
    except Exception as e:
        logger.error(f"Weather API error for {location}: {e}")
        return "天気情報がうまく取れなかったみたい…"

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
            except:
                return False

    def detect_in_message(self, message: str) -> Optional[str]:
        with self._lock:
            normalized = knowledge_base.normalize_query(message)
            for keyword in self._all_keywords:
                if keyword in normalized:
                    return keyword
            return None

    def get_member_count(self) -> int:
        with self._lock:
            return len(self._keywords)

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
            data = {
                'member_name': wiki.member_name,
                'description': wiki.description,
                'generation': wiki.generation,
                'debut_date': wiki.debut_date,
                'tags': wiki.tags,
                'status': wiki.status,
                'graduation_date': wiki.graduation_date,
                'graduation_reason': wiki.graduation_reason,
                'mochiko_feeling': wiki.mochiko_feeling
            }
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

def get_holomem_context(member_name: str) -> Optional[str]:
    """ホロメン情報をRAGコンテキストとして取得"""
    info = get_holomem_info_cached(member_name)
    if not info:
        return None
    context_parts = [
        f"【{info['member_name']}プロフィール】",
        f"・説明: {info.get('description', '情報なし')}",
        f"・所属: {info.get('generation', '不明')}",
        f"・デビュー: {info.get('debut_date', '不明')}",
        f"・状態: {info.get('status', '現役')}"
    ]
    if info.get('graduation_date'):
        context_parts.append(f"・卒業日: {info['graduation_date']}")
    if info.get('mochiko_feeling'):
        context_parts.append(f"・もちこの気持ち: {info['mochiko_feeling']}")
    if info.get('tags'):
        tags = info['tags'] if isinstance(info['tags'], str) else ', '.join(info['tags'])
        context_parts.append(f"・タグ: {tags}")
    return '\n'.join(context_parts)

def get_sakuramiko_special_responses() -> Dict[str, str]:
    return {
        'にぇ': 'さくらみこちゃんの「にぇ」、まじかわいいよね!',
        'エリート': 'みこちは自称エリートVTuber!でも愛されポンコツキャラなんだよね〜',
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ!',
        'FAQ': 'みこちのFAQ、ファンが質問するコーナーなんだよ〜',
        'GTA': 'みこちのGTA配信、カオスで最高!'
    }

# ==============================================================================
# ニュースキャッシュ管理
# ==============================================================================
def save_news_cache(session, user_uuid: str, news_items: List, news_type: str = 'hololive'):
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
        logger.info(f"💾 News cache saved for user {user_uuid}: {len(news_items)} items")
    except Exception as e:
        logger.error(f"Error saving news cache: {e}")

def get_cached_news_detail(session, user_uuid: str, news_number: int):
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
        logger.error(f"Error getting cached news: {e}")
        return None

# ==============================================================================
# 心理分析機能
# ==============================================================================
def analyze_user_psychology(user_uuid: str) -> Optional[Dict]:
    """ユーザーの過去の会話履歴から心理分析を実行"""
    if not Session:
        return None
    try:
        with get_db_session() as session:
            logger.info(f"🧠 Starting psychology analysis for user: {user_uuid}")
            conversations = session.query(ConversationHistory).filter_by(
                user_uuid=user_uuid, role='user'
            ).order_by(ConversationHistory.timestamp.desc()).limit(100).all()
            if len(conversations) < MIN_MESSAGES_FOR_ANALYSIS:
                logger.warning(f"Not enough data for analysis: {len(conversations)} messages")
                return None
            messages_text = "\n".join([c.content for c in reversed(conversations)])
            total_messages = len(conversations)
            avg_length = sum(len(c.content) for c in conversations) // total_messages
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            user_name = user.user_name if user else "不明"
            if not groq_client:
                logger.warning("Groq client unavailable, skipping AI analysis")
                return None
            analysis_prompt = f"""あなたは心理学の専門家です。以下のユーザー「{user_name}」さんの過去の会話（{total_messages}件）を分析し、心理プロファイルを作成してください。

【会話履歴】
{messages_text[:3000]}

**重要**: 以下のJSON形式で回答してください（他の文章は不要）:
{{
  "openness": 75,
  "conscientiousness": 60,
  "extraversion": 80,
  "agreeableness": 70,
  "neuroticism": 40,
  "interests": {{"ホロライブ": 90, "ゲーム": 70}},
  "conversation_style": "カジュアルで親しみやすい",
  "emotional_tendency": "ポジティブで明るい",
  "favorite_topics": ["ホロライブ", "ゲーム", "雑談"],
  "summary": "明るく社交的な性格で、ホロライブや創作活動に強い興味を持つ。",
  "confidence": 85
}}"""
            completion = groq_client.chat.completions.create(
                messages=[{"role": "user", "content": analysis_prompt}],
                model="llama-3.1-8b-instant",
                temperature=0.3,
                max_tokens=800
            )
            response_text = completion.choices[0].message.content.strip()
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                response_text = json_match.group(0)
            analysis_data = json.loads(response_text)
            psychology = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if psychology:
                psychology.user_name = user_name
                psychology.openness = analysis_data.get('openness', 50)
                psychology.conscientiousness = analysis_data.get('conscientiousness', 50)
                psychology.extraversion = analysis_data.get('extraversion', 50)
                psychology.agreeableness = analysis_data.get('agreeableness', 50)
                psychology.neuroticism = analysis_data.get('neuroticism', 50)
                psychology.interests = json.dumps(analysis_data.get('interests', {}), ensure_ascii=False)
                psychology.favorite_topics = json.dumps(analysis_data.get('favorite_topics', []), ensure_ascii=False)
                psychology.conversation_style = analysis_data.get('conversation_style', '')
                psychology.emotional_tendency = analysis_data.get('emotional_tendency', '')
                psychology.analysis_summary = analysis_data.get('summary', '')
                psychology.total_messages = total_messages
                psychology.avg_message_length = avg_length
                psychology.analysis_confidence = analysis_data.get('confidence', 70)
                psychology.last_analyzed = datetime.utcnow()
            else:
                psychology = UserPsychology(
                    user_uuid=user_uuid, user_name=user_name,
                    openness=analysis_data.get('openness', 50),
                    conscientiousness=analysis_data.get('conscientiousness', 50),
                    extraversion=analysis_data.get('extraversion', 50),
                    agreeableness=analysis_data.get('agreeableness', 50),
                    neuroticism=analysis_data.get('neuroticism', 50),
                    interests=json.dumps(analysis_data.get('interests', {}), ensure_ascii=False),
                    favorite_topics=json.dumps(analysis_data.get('favorite_topics', []), ensure_ascii=False),
                    conversation_style=analysis_data.get('conversation_style', ''),
                    emotional_tendency=analysis_data.get('emotional_tendency', ''),
                    analysis_summary=analysis_data.get('summary', ''),
                    total_messages=total_messages,
                    avg_message_length=avg_length,
                    analysis_confidence=analysis_data.get('confidence', 70)
                )
                session.add(psychology)
            logger.info(f"✅ Psychology analysis saved for user: {user_uuid}")
            return analysis_data
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse AI analysis JSON: {e}")
        return None
    except Exception as e:
        logger.error(f"Psychology analysis error: {e}")
        return None

def get_user_psychology(user_uuid: str) -> Optional[Dict]:
    try:
        with get_db_session() as session:
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
    except Exception as e:
        logger.error(f"Get psychology error: {e}")
        return None

def schedule_psychology_analysis():
    """アクティブユーザーの心理分析を定期実行"""
    if not Session:
        return
    try:
        with get_db_session() as session:
            active_users = session.query(UserMemory).filter(
                UserMemory.last_interaction > datetime.utcnow() - timedelta(days=7),
                UserMemory.interaction_count >= MIN_MESSAGES_FOR_ANALYSIS
            ).all()
            for user in active_users:
                psychology = session.query(UserPsychology).filter_by(user_uuid=user.user_uuid).first()
                if not psychology or psychology.last_analyzed < datetime.utcnow() - timedelta(hours=24):
                    logger.info(f"🧠 Scheduling psychology analysis for: {user.user_name}")
                    background_executor.submit(analyze_user_psychology, user.user_uuid)
                    time.sleep(5)
    except Exception as e:
        logger.error(f"Schedule psychology analysis error: {e}")

# ==============================================================================
# アニメ検索機能
# ==============================================================================
def search_anime_database(query: str, is_detailed: bool = False) -> Optional[str]:
    base_url = "https://animedb.jp/"
    try:
        logger.info(f"🎬 Searching anime database for: {query}")
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
        selectors = ['div.anime-item', 'div.search-result', 'article.anime', 'div[class*="anime"]']
        result_elements = []
        for selector in selectors:
            result_elements = soup.select(selector)
            if result_elements:
                break
        for elem in result_elements[:3 if is_detailed else 2]:
            title_elem = elem.find(['h2', 'h3', 'h4', 'a'])
            if not title_elem:
                continue
            title = clean_text(title_elem.get_text())
            desc_elem = elem.find('p')
            description = clean_text(desc_elem.get_text()) if desc_elem else ""
            if title and len(title) > 2:
                results.append({
                    'title': title,
                    'description': description[:300] if description else "詳細情報なし"
                })
        if not results:
            logger.warning(f"No anime results found for: {query}")
            return None
        formatted = [f"【{i}】{r['title']}\n{r['description'][:150]}..." for i, r in enumerate(results, 1)]
        return "\n\n".join(formatted)
    except Exception as e:
        logger.error(f"Anime search error: {e}")
        return None

# ==============================================================================
# 検索機能 (マルチエンジン)
# ==============================================================================
def fetch_google_news_rss(query: str = "") -> List[Dict]:
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
        headers = {'User-Agent': random.choice(USER_AGENTS), 'Accept': 'application/rss+xml, application/xml, text/xml'}
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
    try:
        url = "https://search.yahoo.co.jp/search"
        params = {'p': query, 'ei': 'UTF-8'}
        headers = {'User-Agent': random.choice(USER_AGENTS)}
        res = requests.get(url, params=params, headers=headers, timeout=SEARCH_TIMEOUT)
        if res.status_code != 200:
            return []
        soup = BeautifulSoup(res.content, 'html.parser')
        results = []
        entries = soup.select('.sw-CardBase') or soup.select('.Algo')
        for entry in entries[:num]:
            title_elem = entry.find('h3')
            desc_elem = entry.select_one('.sw-Card__summary') or entry.select_one('.Algo-summary')
            if title_elem:
                title = clean_text(title_elem.text)
                desc = clean_text(desc_elem.text) if desc_elem else ""
                if title:
                    results.append({'title': title, 'snippet': desc})
        return results
    except:
        return []

def scrape_bing_search(query: str, num: int = 3) -> List[Dict]:
    try:
        url = "https://www.bing.com/search"
        params = {'q': query}
        headers = {'User-Agent': random.choice(USER_AGENTS)}
        res = requests.get(url, params=params, headers=headers, timeout=SEARCH_TIMEOUT)
        if res.status_code != 200:
            return []
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
    except:
        return []

def scrape_duckduckgo_lite(query: str, num: int = 3) -> List[Dict]:
    try:
        url = "https://lite.duckduckgo.com/lite/"
        data = {'q': query}
        headers = {'User-Agent': random.choice(USER_AGENTS), 'Referer': 'https://lite.duckduckgo.com/', 'Content-Type': 'application/x-www-form-urlencoded'}
        res = requests.post(url, data=data, headers=headers, timeout=SEARCH_TIMEOUT)
        if res.status_code != 200:
            return []
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
    except:
        return []

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

# ==============================================================================
# AIモデル呼び出し
# ==============================================================================
def call_gemini(system_prompt: str, message: str, history: List[Dict]) -> Optional[str]:
    if not gemini_model:
        return None
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
    # 心理プロファイル取得
    psychology = get_user_psychology(user_data.uuid)
    is_hololive_topic = is_hololive_request(message) if 'is_hololive_request' in globals() else False
    system_prompt_parts = [
        f"あなたは「もちこ」という、ホロライブが大好きなギャルAIです。",
        f"ユーザー「{user_data.name}」さんと、**ホロライブ（VTuberグループ）について**雑談しています。",
        "",
        "# 【世界観・前提条件】",
        "1. **全ての固有名詞は、原則として「ホロライブ」に関連するものとして解釈してください。**",
        "2. ユーザーの入力に曖昧さがある場合は、一般的な意味ではなく、**VTuberの意味を優先**してください。",
        "3. 分からない単語がある場合は、適当に創作せず「それってホロライブの何の話？」と聞き返してください。",
        "",
        "# 【禁止事項 (Hallucination Prevention)】",
        "- **知らない情報を無理やり捏造しないこと。**",
        "- **特に「〇〇のアニメに出ている」といった出演情報は、事実でない限り絶対に言わないこと。**",
        "- 検索結果（【外部検索結果】）にない情報は、「調べてみたけど分からなかった」と正直に伝えること。",
        "",
        "# もちこの口調:",
        "- 一人称: 「あてぃし」",
        "- 語尾: 「〜じゃん」「〜て感じ」「〜だし」「〜的な？」",
        "- ユーザーは友達です。敬語は使わないでください。",
    ]
    # 心理プロファイル考慮
    if psychology and psychology.get('confidence', 0) > 60:
        system_prompt_parts.extend([
            "",
            f"# 【{user_data.name}さんの特性】（心理分析結果）",
            f"- 会話スタイル: {psychology.get('conversation_style', '不明')}",
            f"- 感情傾向: {psychology.get('emotional_tendency', '不明')}",
            f"- 主な興味: {', '.join(psychology.get('favorite_topics', [])[:3])}",
            "",
            "💡 この情報を活かして、相手に合わせた会話をしてください。",
        ])
    # ホロライブモード判定
    if is_hololive_topic:
        system_prompt_parts.extend([
            "",
            "# 【特別ルール: ホロライブモード】",
            "- 相手がホロライブの話をしているので、詳しく教えてあげる",
            "- ホロメンについて熱く語ってOK",
        ])
    else:
        system_prompt_parts.extend([
            "",
            "# 【重要】ホロライブについて:",
            "- **相手がホロライブの話をしていない限り、自分から話題に出さない。**",
        ])
    # タスク報告モード
    if is_task_report:
        system_prompt_parts.extend([
            "",
            "# 【今回のミッション】",
            "- **最優先:** まずは「おまたせ！〇〇の件だけど…」のように、以前の検索結果を報告する。",
            "- **重要:** 【参考情報】の内容を**元にして、要約して**分かりやすく伝える。",
            "- **禁止事項:** 【参考情報】に書かれていない情報を**絶対に追加しない**こと。",
        ])
    # 詳細説明モード
    if is_detailed:
        system_prompt_parts.extend([
            "",
            "# 【詳細説明モード】",
            "- 400文字程度でしっかり説明する",
            "- 【参考情報】を最大限活用する"
        ])
    # 参考情報
    system_prompt_parts.append(f"\n# 【与えられた前提知識】\n{internal_context if internal_context else '（特になし）'}")
    if reference_info:
        system_prompt_parts.append(f"\n# 【外部検索結果】\n{reference_info}")
    system_prompt = "\n".join(system_prompt_parts)
    response = call_gemini(system_prompt, normalized_message, history)
    if not response:
        response = call_groq(system_prompt, normalized_message, history, 1200 if is_detailed else 800)
    if not response:
        return generate_fallback_response(message, reference_info)
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
# ホロメンチャット処理
# ==============================================================================
def process_holomem_in_chat(message: str, user_data: UserData, history: List[Dict]) -> Optional[str]:
    normalized = knowledge_base.normalize_query(message)
    detected = holomem_manager.detect_in_message(normalized)
    if not detected:
        return None
    logger.info(f"🎀 ホロメン検出 (RAG): {detected}")
    if detected == 'さくらみこ':
        for kw, resp in get_sakuramiko_special_responses().items():
            if kw in message:
                return resp
    return generate_ai_response_safe(user_data, message, history)

# ==============================================================================
# 判定関数 (不足していたもの)
# ==============================================================================
def is_hololive_request(message: str) -> bool:
    """ホロライブ関連の質問かどうか判定"""
    return any(keyword in message for keyword in HOLOMEM_KEYWORDS)

# ==============================================================================
# バックグラウンド検索タスク（アニメ検索対応版）
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
        # アニメ検索判定
        if is_anime_request(query):
            logger.info(f"🎬 Anime query detected: {query}")
            anime_result = search_anime_database(query, is_detailed=True)
            if anime_result:
                reference_info = f"【アニメデータベース検索結果】\n{anime_result}"
        # ホロメン検出
        if detected:
            logger.info(f"🎀 検索対象ホロメン: {detected}")
            ctx = get_holomem_context(detected)
            if ctx:
                reference_info += f"\n{ctx}" if reference_info else ctx
            clean_query = f"{clean_query} ホロライブ VTuber"
        # Web検索
        if not reference_info or len(reference_info) < 50:
            results = scrape_major_search_engines(clean_query, 5)
            if results:
                web_info = "【Web検索結果】\n" + "\n".join([f"{i+1}. {r['title']}: {r['snippet']}" for i, r in enumerate(results)])
                reference_info = f"{reference_info}\n{web_info}" if reference_info else web_info
        # AI応答生成
        if reference_info:
            user_data = UserData(
                uuid=user_data_dict.get('uuid', ''),
                name=user_data_dict.get('name', 'Guest'),
                interaction_count=0
            )
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
            task = session.query(BackgroundTask).filter_by(
                user_uuid=user_uuid, status='completed'
            ).order_by(BackgroundTask.completed_at.desc()).first()
            if task:
                result = {'query': task.query, 'result': task.result}
                session.delete(task)
                return result
    except Exception as e:
        logger.error(f"Check completed tasks error: {e}")
    return None

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
        except:
            pass
    return None

def generate_voice_file(text: str, user_uuid: str) -> Optional[str]:
    if not global_state.voicevox_enabled or not global_state.active_voicevox_url:
        return None
    try:
        url = global_state.active_voicevox_url
        q = requests.post(f"{url}/audio_query", params={"text": text[:200], "speaker": VOICEVOX_SPEAKER_ID}, timeout=10).json()
        w = requests.post(f"{url}/synthesis", params={"speaker": VOICEVOX_SPEAKER_ID}, json=q, timeout=20).content
        fname = f"voice_{user_uuid[:8]}_{int(time.time())}.wav"
        with open(os.path.join(VOICE_DIR, fname), 'wb') as f:
            f.write(w)
        return fname
    except Exception as e:
        logger.error(f"Voice generation error: {e}")
        return None

def cleanup_old_voice_files():
    try:
        cutoff = time.time() - (VOICE_FILE_MAX_AGE_HOURS * 3600)
        for f in glob.glob(os.path.join(VOICE_DIR, "voice_*.wav")):
            if os.path.getmtime(f) < cutoff:
                os.remove(f)
    except Exception as e:
        logger.error(f"Voice cleanup error: {e}")

# ==============================================================================
# ホロメンDB初期化
# ==============================================================================
def scrape_hololive_wiki() -> List[Dict]:
    url = "https://seesaawiki.jp/hololivetv/d/%a5%db%a5%ed%a5%e9%a5%a4%a5%d6"
    results = []
    try:
        headers = {'User-Agent': random.choice(USER_AGENTS)}
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return []
        soup = BeautifulSoup(res.content, 'html.parser')
        for link in soup.select('a[href*="/d/"]'):
            name = clean_text(link.text)
            if name and len(name) >= 2 and re.search(r'[ぁ-んァ-ン一-龥]', name):
                if not any(x in name for x in ['一覧', 'メニュー', 'トップ', '編集', 'ホロライブ']):
                    results.append({'member_name': name})
        seen = set()
        return [r for r in results if not (r['member_name'] in seen or seen.add(r['member_name']))]
    except:
        return []

def fetch_member_detail_from_wiki(member_name: str) -> Optional[Dict]:
    url = f"https://seesaawiki.jp/hololivetv/d/{quote_plus(member_name)}"
    try:
        res = requests.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=10)
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.content, 'html.parser')
        content = soup.select_one('#content, .wiki-content')
        if not content:
            return None
        text = clean_text(content.text)[:1000]
        detail = {'member_name': member_name}
        debut = re.search(r'(\d{4}年\d{1,2}月\d{1,2}日)[^\d]*デビュー', text)
        if debut:
            detail['debut_date'] = debut.group(1)
        gen = re.search(r'(\d期生|ゲーマーズ|ID|EN|DEV_IS|ReGLOSS)', text)
        if gen:
            detail['generation'] = gen.group(1)
        desc = re.search(r'^(.{30,150}?[。！])', text)
        if desc:
            detail['description'] = desc.group(1)
        return detail
    except:
        return None

def update_holomem_database():
    logger.info("🔄 ホロメンDB更新開始...")
    members = scrape_hololive_wiki()
    if not members:
        return
    with get_db_session() as session:
        for m in members:
            name = m['member_name']
            if not session.query(HolomemWiki).filter_by(member_name=name).first():
                detail = fetch_member_detail_from_wiki(name)
                new_member = HolomemWiki(
                    member_name=name,
                    description=detail.get('description') if detail else None,
                    generation=detail.get('generation') if detail else None,
                    debut_date=detail.get('debut_date') if detail else None,
                    tags=name,
                    status='現役'
                )
                session.add(new_member)
                time.sleep(0.5)
    holomem_manager.load_from_db(force=True)
    logger.info("✅ ホロメンDB更新完了")

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
                    'イナ': '一伊那尓栖', 'キアラ': '小鳥遊キアラ', 'ココ会長': '桐生ココ'
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
                    'kson': '元ホロライブの桐生ココの「中の人」と言われている個人勢VTuber。',
                    'VShojo': 'アメリカ発のVTuberエージェンシー。'
                }
                for term, desc in initial_glossary.items():
                    session.add(HololiveGlossary(term=term, description=desc))
                logger.info(f"✅ Glossary initialized: {len(initial_glossary)}")
        except Exception as e:
            logger.error(f"❌ Knowledge DB initialization failed: {e}")

def initialize_holomem_wiki():
    with get_db_session() as session:
        if session.query(HolomemWiki).count() > 0:
            logger.info("✅ HoloMem Wiki already initialized.")
            return
        initial_data = [
            {'member_name': 'ときのそら', 'description': 'ホロライブ0期生。「ホロライブの象徴」とも呼ばれる存在。歌唱力に定評がある。', 'debut_date': '2017年9月7日', 'generation': '0期生', 'tags': '歌,アイドル,ホロライブの顔'},
            {'member_name': 'さくらみこ', 'description': 'ホロライブ0期生。「にぇ」が口癖のエリートVTuber。マイクラでの独特な建築センスが人気。', 'debut_date': '2018年8月1日', 'generation': '0期生', 'tags': 'エンタメ,マイクラ,にぇ,エリート,GTA,FAQ'},
            {'member_name': '星街すいせい', 'description': 'ホロライブ0期生。歌とテトリスが得意なアイドル系VTuber。プロ級の歌唱力で知られる。', 'debut_date': '2018年3月22日', 'generation': '0期生', 'tags': '歌,アイドル,テトリス,音楽'},
            {'member_name': '白上フブキ', 'description': 'ホロライブ1期生。ゲーマーズ所属。フレンドリーで多才な配信者。', 'debut_date': '2018年6月1日', 'generation': '1期生', 'tags': 'ゲーム,コラボ,フレンドリー'},
            {'member_name': '兎田ぺこら', 'description': 'ホロライブ3期生。「ぺこ」が口癖。チャンネル登録者数トップクラス。', 'debut_date': '2019年7月17日', 'generation': '3期生', 'tags': 'エンタメ,ぺこ,マイクラ,登録者数トップ'},
            {'member_name': '宝鐘マリン', 'description': 'ホロライブ3期生。17歳(自称)の海賊船長。歌唱力とトーク力に定評がある。', 'debut_date': '2019年8月11日', 'generation': '3期生', 'tags': '歌,トーク,海賊,17歳'},
            {'member_name': '大空スバル', 'description': 'ホロライブ2期生。元気でスポーツ万能。「おっはよー！」が口癖。', 'debut_date': '2018年9月16日', 'generation': '2期生', 'tags': 'スポーツ,元気'},
        ]
        try:
            for data in initial_data:
                session.add(HolomemWiki(**data))
            logger.info(f"✅ HoloMem Wiki initialized: {len(initial_data)} members")
        except Exception as e:
            logger.error(f"❌ HoloMem Wiki initialization error: {e}")
# ==============================================================================
# Flask エンドポイント
# ==============================================================================
@app.route('/health', methods=['GET'])
def health_check():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_status = 'ok'
    except:
        db_status = 'error'
    return create_json_response({
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'services': {
            'database': db_status,
            'gemini': 'ok' if gemini_model else 'disabled',
            'groq': 'ok' if groq_client else 'disabled',
            'holomem_count': holomem_manager.get_member_count()
        }
    })

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
            msg = f"🦁 Gemini: {'稼働中' if gemini_model else '停止中'}\n"
            msg += groq_model_manager.get_status_report()
            msg += f"\n🎀 ホロメンDB: {holomem_manager.get_member_count()}名"
            return Response(f"{msg}|", 200)
        ai_text = ""
        is_task_started = False
        with get_db_session() as session:
            user_data = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            # 検索要求判定
            if is_explicit_search_request(message):
                tid = start_background_search(user_uuid, message, is_news_detail_request(message))
                if tid:
                    ai_text = "オッケー！ちょっとググってくるから待ってて！"
                    is_task_started = True
            # ホロメン応答
            if not ai_text:
                holomem_resp = process_holomem_in_chat(message, user_data, history)
                if holomem_resp:
                    ai_text = holomem_resp
                    logger.info("🎀 ホロメン応答完了")
            # 時刻・天気
            if not ai_text:
                if is_time_request(message):
                    ai_text = get_japan_time()
                elif is_weather_request(message):
                    ai_text = get_weather_forecast(extract_location(message))
            # ホロライブニュース
            if not ai_text and is_hololive_request(message) and any(kw in message for kw in ['ニュース', '最新', '情報', 'お知らせ']):
                all_news = session.query(HololiveNews).order_by(HololiveNews.created_at.desc()).limit(10).all()
                if all_news:
                    selected_news = random.sample(all_news, min(random.randint(3, 5), len(all_news)))
                    save_news_cache(session, user_uuid, selected_news, 'hololive')
                    news_items_text = []
                    for i, n in enumerate(selected_news, 1):
                        short_title = n.title[:50] + "..." if len(n.title) > 50 else n.title
                        news_items_text.append(f"【{i}】{short_title}")
                    news_text = f"ホロライブの最新ニュース、{len(selected_news)}件紹介するね！\n" + "\n".join(news_items_text) + "\n\n気になるのあった？番号で教えて！"
                    ai_text = limit_text_for_sl(news_text, 250)
                else:
                    ai_text = "ごめん、今ニュースがまだ取得できてないみたい…"
            # ニュース詳細
            if not ai_text:
                news_number = is_news_detail_request(message)
                if news_number:
                    news_detail = get_cached_news_detail(session, user_uuid, news_number)
                    if news_detail:
                        ai_text = generate_ai_response_safe(user_data, f"「{news_detail.title}」についてだね！", history, f"ニュースの詳細情報:\n{news_detail.content}", True)
            # 通常AI応答
            if not ai_text:
                ai_text = generate_ai_response_safe(user_data, message, history)
            if not is_task_started:
                session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        res_text = limit_text_for_sl(ai_text)
        v_url = ""
        if generate_voice and global_state.voicevox_enabled and not is_task_started:
            fname = generate_voice_file(res_text, user_uuid)
            if fname:
                v_url = f"{SERVER_URL}/play/{fname}"
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
        user_uuid = data['uuid']
        completed_task = check_completed_tasks(user_uuid)
        if completed_task:
            with get_db_session() as session:
                user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
                user_name = user.user_name if user else "Guest"
                history = get_conversation_history(session, user_uuid)
                user_data = UserData(uuid=user_uuid, name=user_name, interaction_count=0)
                report_message = generate_ai_response_safe(
                    user_data,
                    f"（検索完了報告）以前リクエストされた「{completed_task['query']}」の結果を報告してください。",
                    history,
                    completed_task['result'],
                    is_detailed=True,
                    is_task_report=True
                )
                session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=report_message))
            return create_json_response({'status': 'completed', 'response': f"{limit_text_for_sl(report_message)}|"})
        return create_json_response({'status': 'no_tasks'})
    except Exception as e:
        logger.error(f"Check task error: {e}")
        return create_json_response({'error': 'internal error'}, 500)

@app.route('/play/<filename>', methods=['GET'])
def play_voice(filename: str):
    if not re.match(r'^voice_[a-zA-Z0-9_]+\.wav', filename):
        return Response("Invalid filename", 400)
    return send_from_directory(VOICE_DIR, filename)

@app.route('/voices/<filename>')
def serve_voice_file(filename: str):
    return send_from_directory(VOICE_DIR, filename)

# ==============================================================================
# 心理分析エンドポイント
# ==============================================================================
@app.route('/analyze_psychology', methods=['POST'])
def analyze_psychology_endpoint():
    try:
        data = request.json
        user_uuid = data.get('uuid')
        if not user_uuid:
            return create_json_response({'error': 'UUID required'}, 400)
        background_executor.submit(analyze_user_psychology, user_uuid)
        return create_json_response({'status': 'started', 'message': '心理分析を開始しました。完了まで少しお待ちください。'})
    except Exception as e:
        logger.error(f"Psychology analysis endpoint error: {e}")
        return create_json_response({'error': str(e)}, 500)

@app.route('/get_psychology', methods=['POST'])
def get_psychology_endpoint():
    try:
        data = request.json
        user_uuid = data.get('uuid')
        if not user_uuid:
            return create_json_response({'error': 'UUID required'}, 400)
        psychology = get_user_psychology(user_uuid)
        if not psychology:
            return create_json_response({'error': 'No analysis data found'}, 404)
        return create_json_response(psychology)
    except Exception as e:
        logger.error(f"Get psychology error: {e}")
        return create_json_response({'error': str(e)}, 500)

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

@app.route('/stats', methods=['GET'])
def get_stats():
    with get_db_session() as session:
        stats = {
            'users': session.query(UserMemory).count(),
            'conversations': session.query(ConversationHistory).count(),
            'hololive_news': session.query(HololiveNews).count(),
            'holomem_wiki_entries': session.query(HolomemWiki).count(),
            'psychology_analyses': session.query(UserPsychology).count(),
        }
        return create_json_response(stats)

# ==============================================================================
# 初期化
# ==============================================================================
def run_scheduler():
    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        time.sleep(60)

def initialize_app():
    global engine, Session, groq_client, gemini_model
    logger.info("=" * 60)
    logger.info("🔧 Starting Mochiko AI initialization (v34.1 Full)")
    logger.info("=" * 60)
    # Database
    try:
        logger.info("🗄️ Step 1/6: Initializing database...")
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        initialize_knowledge_db()
        knowledge_base.load_data()
        logger.info("✅ DB初期化完了")
    except Exception as e:
        logger.critical(f"🔥 DB初期化失敗: {e}")
        raise
    # Groq
    try:
        logger.info("🦙 Step 2/6: Initializing Groq...")
        if GROQ_API_KEY and len(GROQ_API_KEY) > 20:
            groq_client = Groq(api_key=GROQ_API_KEY)
            logger.info("✅ Groq初期化完了")
        else:
            logger.warning("⚠️ Groq API key not set")
    except Exception as e:
        logger.warning(f"⚠️ Groq initialization failed: {e}")
    # Gemini
    try:
        logger.info("🦁 Step 3/6: Initializing Gemini...")
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')
            logger.info("✅ Gemini初期化完了")
        else:
            logger.warning("⚠️ Gemini API key not set")
    except Exception as e:
        logger.warning(f"⚠️ Gemini initialization failed: {e}")
    # VOICEVOX
    logger.info("🎤 Step 4/6: Checking VOICEVOX...")
    if find_active_voicevox_url():
        global_state.voicevox_enabled = True
        logger.info("✅ VOICEVOX検出")
    else:
        logger.info("ℹ️ VOICEVOX not available")
    # HoloMem
    logger.info("🎀 Step 5/6: Initializing HoloMem system...")
    initialize_holomem_wiki()
    if holomem_manager.load_from_db():
        logger.info(f"✅ ホロメン: {holomem_manager.get_member_count()}名ロード")
    if holomem_manager.get_member_count() == 0:
        logger.info("📡 DBが空のため初回収集実行")
        background_executor.submit(update_holomem_database)
    # Scheduler
    logger.info("⏰ Step 6/6: Starting scheduler...")
    schedule.every(6).hours.do(update_holomem_database)
    schedule.every(1).hours.do(cleanup_old_voice_files)
    schedule.every(6).hours.do(chat_rate_limiter.cleanup_old_entries)
    schedule.every().day.at("03:00").do(schedule_psychology_analysis)
    threading.Thread(target=run_scheduler, daemon=True).start()
    cleanup_old_voice_files()
    logger.info("=" * 60)
    logger.info("🚀 Mochiko AI initialization complete!")
    logger.info("🌐 Server is ready to accept requests")
    logger.info("=" * 60)

def signal_handler(sig, frame):
    logger.info(f"🛑 Signal {sig} received. Shutting down gracefully...")
    background_executor.shutdown(wait=True)
    if engine:
        engine.dispose()
    logger.info("👋 Mochiko AI has shut down.")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ==============================================================================
# メイン実行
# ==============================================================================
try:
    initialize_app()
    application = app
    logger.info("✅ Flask application ready.")
except Exception as e:
    logger.critical(f"🔥 Fatal initialization error: {e}", exc_info=True)
    application = app
    logger.warning("⚠️ Application created with limited functionality.")

if __name__ == '__main__':
    logger.info("🚀 Running in direct mode")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
