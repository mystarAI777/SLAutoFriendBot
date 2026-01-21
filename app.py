# ==============================================================================
# もちこAI - v33.2.0 + パーソナライズ機能 + SNSリアルタイム情報連携
#
# ベース: v33.1.1 (全機能保持)
# 追加機能:
# 1. ユーザーの好みトピック分析と話題提案 (v33.1.1)
# 2. 心理分析結果をAI応答に反映 (v33.1.1)
# 3. 会話回数に応じた関係性の深化（友達認定システム） (v33.1.1)
# 4. Yahoo!リアルタイム検索によるホロメンSNS情報収集・会話反映 (v33.2.0 NEW)
#
# 修正履歴:
# - DBスキーマ自動修復機能の強化 (recent_activityカラム対応)
# 変更点:
# 1. 全8段階のインテリジェント・フォールバック (Gemini 2.0優先)
# 2. 内容の複雑度に応じた自動モデル振り分け (日常/複雑)
# 3. ニュースソース拡充: Linden Lab (Second Life), CGWORLD (CG/3D)
# 4. 話題逸らし防止プロンプト制御
# 5. エラーモデルの一時スキップ機能 (リトライの無駄を排除)
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
SL_SAFE_CHAR_LIMIT = 600
MIN_MESSAGES_FOR_ANALYSIS = 10
SEARCH_TIMEOUT = 10
VOICE_FILE_MAX_AGE_HOURS = 24

# ★ パーソナライズ設定
FRIEND_THRESHOLD = 5  # この回数以上で友達認定
ANALYSIS_INTERVAL = 5  # この回数ごとに心理分析を実行
TOPIC_SUGGESTION_INTERVAL = 10  # この回数ごとに話題を提案

# ==============================================================================
# 【変更2】GEMINI_MODELS 定数を追加（行80付近）
# ==============================================================================
GEMINI_MODELS = [
    "gemini-1.5-flash",      # 最も安定
    "gemini-1.5-flash-8b",   # 軽量版
    "gemini-2.0-flash-exp",  # 実験版（制限厳しい）
]
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
# ==============================================================================
# 【変更1】データクラスに GeminiModelStatus を追加
# ==============================================================================
@dataclass
class GeminiModelStatus:
    is_limited: bool = False
    reset_time: Optional[datetime] = None
    current_model: str = "gemini-1.5-flash"
    last_error: Optional[str] = None
@dataclass
class UserData:
    uuid: str
    name: str
    interaction_count: int
    is_friend: bool = False
    favorite_topics: List[str] = field(default_factory=list)
    psychology: Optional[Dict] = None

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
# ==============================================================================
# 【変更3】GeminiModelManager クラスを追加（行120付近、GlobalStateの後）
# ==============================================================================
class GeminiModelManager:
    """Geminiモデルのフォールバック管理"""
    def __init__(self):
        self._lock = RLock()
        self._models = GEMINI_MODELS
        self._current_index = 0
        self._status = GeminiModelStatus()
        self._gemini_instances = {}
    
    def get_current_model(self) -> Optional[Any]:
        """現在利用可能なGeminiモデルを取得"""
        with self._lock:
            # 制限中かつリセット時間を過ぎていたらリセット
            if self._status.is_limited and self._status.reset_time:
                if datetime.utcnow() >= self._status.reset_time:
                    logger.info(f"✅ Gemini制限解除: {self._status.current_model}")
                    self._status.is_limited = False
                    self._status.reset_time = None
            
            # 制限中なら次のモデルを試す
            if self._status.is_limited:
                self._current_index = (self._current_index + 1) % len(self._models)
                self._status.current_model = self._models[self._current_index]
                self._status.is_limited = False
                logger.info(f"🔄 Geminiモデル切り替え: {self._status.current_model}")
            
            model_name = self._models[self._current_index]
            
            # キャッシュから取得または新規作成
            if model_name not in self._gemini_instances:
                try:
                    self._gemini_instances[model_name] = genai.GenerativeModel(model_name)
                    logger.info(f"🆕 Geminiモデル初期化: {model_name}")
                except Exception as e:
                    logger.error(f"❌ Gemini初期化失敗 ({model_name}): {e}")
                    return None
            
            return self._gemini_instances[model_name]
    
    def mark_limited(self, wait_seconds: int = 60):
        """Geminiが制限された際の処理"""
        with self._lock:
            self._status.is_limited = True
            self._status.reset_time = datetime.utcnow() + timedelta(seconds=wait_seconds)
            logger.warning(f"⚠️ Gemini制限検知 ({self._status.current_model}): {wait_seconds}秒後にリトライ")
    
    def get_status_report(self) -> str:
        """ステータスレポート"""
        with self._lock:
            if self._status.is_limited and self._status.reset_time:
                jst = (self._status.reset_time + timedelta(hours=9)).strftime('%H:%M:%S')
                return f"🤖 Gemini: ❌ 制限中 ({self._status.current_model}) - 解除: {jst}"
            else:
                return f"🤖 Gemini: ✅ 稼働中 ({self._status.current_model})"

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
gemini_model_manager = GeminiModelManager()
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
    is_friend = Column(Boolean, default=False)
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
    # ★ 新規追加: 最新のXでの話題などを保存するカラム
    recent_activity = Column(Text, nullable=True)

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
# タスクの実行時間を記録するテーブル
class TaskLog(Base):
    __tablename__ = 'task_logs'
    task_name = Column(String(100), primary_key=True)
    last_run = Column(DateTime, default=datetime.utcnow)
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
    msg = msg.strip()
    strong_triggers = ['調べて', '検索', '探して', 'とは', 'って何', 'について', '教えて', '教えろ', '詳細', '知りたい']
    if any(kw in msg for kw in strong_triggers):
        return True
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

def get_weather_forecast(location: str = "東京") -> str:
    """天気予報を取得"""
    try:
        location_code = LOCATION_CODES.get(location, LOCATION_CODES["東京"])
        url = f"https://weather.tsukumijima.net/api/forecast/city/{location_code}"
        res = requests.get(url, timeout=5)
        if res.status_code != 200: return f"{location}の天気情報が取得できなかったよ…"
        data = res.json()
        today = data['forecasts'][0]
        return f"{location}の今日の天気は「{today['telop']}」だよ！{today['detail']['weather'] if today.get('detail') else ''}"
    except:
        return f"{location}の天気情報が取得できなかったよ…"

def get_or_create_user(session, user_uuid: str, user_name: str) -> UserData:
    # ユーザー取得または作成
    user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != user_name: user.user_name = user_name
        
        # 友達認定チェック
        if hasattr(user, 'is_friend'):
            if user.interaction_count >= FRIEND_THRESHOLD and not user.is_friend:
                user.is_friend = True
                logger.info(f"🎉 {user_name}さんが友達に認定されました！")
        else:
            logger.warning("is_friend column missing on model access")
            user.is_friend = False 
    else:
        user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
        session.add(user)
    
    # 心理データ取得
    psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
    fav_topics = []
    psych_data = None
    if psych:
        if psych.favorite_topics:
            fav_topics = [t.strip() for t in psych.favorite_topics.split(',') if t.strip()]
        psych_data = {
            'openness': psych.openness,
            'extraversion': psych.extraversion,
            'confidence': psych.analysis_confidence
        }
    
    return UserData(
        uuid=user.user_uuid,
        name=user.user_name,
        interaction_count=user.interaction_count,
        is_friend=getattr(user, 'is_friend', False),
        favorite_topics=fav_topics,
        psychology=psych_data
    )

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
# ホロメン情報キャッシュ & リアルタイム情報収集
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
            # ★ 修正: recent_activityを追加
            data = {k: getattr(wiki, k) for k in ['member_name', 'description', 'generation', 'debut_date', 'tags', 'status', 'graduation_date', 'mochiko_feeling', 'recent_activity']}
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

def get_holomem_context(member_name: str) -> str:
    """ホロメン情報をコンテキスト用テキストとして取得（SNS情報含む）"""
    info = get_holomem_info_cached(member_name)
    if not info:
        return ""
    
    context = f"【{info['member_name']}の情報】\n"
    if info.get('description'):
        context += f"- {info['description']}\n"
    if info.get('generation'):
        context += f"- 所属: {info['generation']}\n"
    if info.get('debut_date'):
        context += f"- デビュー: {info['debut_date']}\n"
    if info.get('status'):
        context += f"- 状態: {info['status']}\n"
        if info['status'] == '卒業' and info.get('graduation_date'):
            context += f"- 卒業日: {info['graduation_date']}\n"
    
    # ★ 追加: Xの最新情報がある場合はコンテキストに追加
    if info.get('recent_activity'):
         context += f"\n【{info['member_name']}に関する直近のX(Twitter)の様子・話題】\n{info['recent_activity']}\n"
    
    return context

# ==============================================================================
# ★ 追加機能: Yahoo!リアルタイム検索連携
# ==============================================================================
def scrape_yahoo_realtime_for_member(member_name: str) -> str:
    """指定したメンバーのリアルタイム検索結果をテキストで返す"""
    try:
        # 検索クエリ: 名前を含み、RTを除く
        query = f"{member_name} -RT"
        url = "https://search.yahoo.co.jp/realtime/search"
        params = {'p': query, 'ei': 'UTF-8', 'm': 'latency'} # m=latencyで新着順
        headers = {'User-Agent': random.choice(USER_AGENTS)}
        
        res = requests.get(url, params=params, headers=headers, timeout=10)
        if res.status_code != 200: return ""
        
        soup = BeautifulSoup(res.content, 'html.parser')
        texts = []
        # 最新の5件程度を取得
        for item in soup.select('.cnt.cf')[:5]:
            txt = item.select_one('.kw')
            tim = item.select_one('.tim')
            if txt:
                clean_txt = clean_text(txt.text)
                time_txt = clean_text(tim.text) if tim else ""
                texts.append(f"・({time_txt}) {clean_txt}")
        
        return "\n".join(texts)
    except Exception as e:
        logger.error(f"Realtime search failed for {member_name}: {e}")
        return ""

def update_holomem_social_activities():
    """全ホロメンの最新状況をYahooから収集してDB更新（少しずつ行う）"""
    logger.info("🐦 ホロメンSNS状況更新タスク開始")
    with get_db_session() as session:
        # 更新が古い順、またはランダムに5人選んで更新（全アクセスによるBAN防止）
        members = session.query(HolomemWiki).order_by(HolomemWiki.last_updated.asc()).limit(5).all()
        
        for m in members:
            logger.info(f"🔎 {m.member_name} の最新状況を収集中...")
            activities = scrape_yahoo_realtime_for_member(m.member_name)
            
            if activities:
                # DBに保存
                m.recent_activity = activities
                m.last_updated = datetime.utcnow()
                # キャッシュクリア
                clear_holomem_cache(m.member_name)
            
            time.sleep(3) # アクセス間隔を空ける
            
    logger.info("✅ SNS状況更新完了")

# ==============================================================================
# ホロメンスクレイピング & DB更新
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
# ホロライブニュース収集
# ==============================================================================
def fetch_hololive_news():
    logger.info("📰 ニュースDB更新開始...")
    url = "https://hololive.hololivepro.com/news"
    try:
        res = requests.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=15)
        if res.status_code != 200: return
        soup = BeautifulSoup(res.content, 'html.parser')
        
        articles = soup.select('ul.news_list > li') or soup.select('.news_list_item')
        
        with get_db_session() as session:
            for art in articles[:10]:
                a_tag = art.find('a')
                if not a_tag: continue
                
                link = a_tag.get('href')
                title_elem = art.find(['h3', 'p', 'dt'])
                title = clean_text(title_elem.text) if title_elem else clean_text(a_tag.text)
                
                if title and link:
                    if not session.query(HololiveNews).filter_by(url=link).first():
                        session.add(HololiveNews(
                            title=title,
                            content=title,
                            url=link,
                            created_at=datetime.utcnow()
                        ))
        logger.info("✅ ニュースDB更新完了")
    except Exception as e:
        logger.error(f"News fetch failed: {e}")

def fetch_hololive_tsuushin_news():
    """ホロライブ通信からニュースを取得"""
    logger.info("📰 ホロライブ通信の更新チェック開始...")
    url = "https://hololive-tsuushin.com/holonews/"
    try:
        headers = {'User-Agent': random.choice(USER_AGENTS)}
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200: return
        
        soup = BeautifulSoup(res.content, 'html.parser')
        articles = soup.select('article') or soup.select('.post-list-item')
        
        with get_db_session() as session:
            count = 0
            for art in articles[:10]:
                a_tag = art.find('a')
                if not a_tag: continue
                
                link = a_tag.get('href')
                title_elem = art.find(['h1', 'h2', 'h3', 'p'])
                title = clean_text(title_elem.text) if title_elem else clean_text(a_tag.text)
                
                if title and link:
                    if not session.query(HololiveNews).filter_by(url=link).first():
                        session.add(HololiveNews(
                            title=f"【まとめ】{title}",
                            content=title,
                            url=link,
                            created_at=datetime.utcnow()
                        ))
                        count += 1
            logger.info(f"✅ ホロライブ通信から {count} 件の新しいニュースを追加")
    except Exception as e:
        logger.error(f"❌ ホロライブ通信の取得に失敗: {e}")

def wrapped_news_fetch():
    """公式サイトとホロライブ通信の両方からニュースを取得"""
    fetch_hololive_news()
    fetch_hololive_tsuushin_news()
    with get_db_session() as session:
        log = session.query(TaskLog).filter_by(task_name='fetch_news').first()
        if not log:
            log = TaskLog(task_name='fetch_news')
            session.add(log)
        log.last_run = datetime.utcnow()

# --- [強化] ニュース取得関数 ---
def fetch_news_task_integrated():
    # 1. app (1).py のSNS情報を取得
    try:
        update_holomem_social_activities()
    except Exception as e:
        logger.error(f"SNS収集エラー: {e}")

    # 2. SL / CG / 公式ニュースを取得
    sources = [
        {"name": "SecondLife", "url": "https://community.secondlife.com/blogs/rss/3-featured-news/", "type": "rss"},
        {"name": "CGWORLD", "url": "https://cgworld.jp/rss/news/", "type": "rss"}
    ]
    
def wrapped_holomem_update():
    """ホロメンDBを更新して実行時間を記録する"""
    update_holomem_database()
    with get_db_session() as session:
        log = session.query(TaskLog).filter_by(task_name='update_holomem').first()
        if not log:
            log = TaskLog(task_name='update_holomem')
            session.add(log)
        log.last_run = datetime.utcnow()

def catch_up_task(task_name, wrapped_func, interval_hours=1):
    """前回の実行から時間が経ちすぎていたら実行する"""
    with get_db_session() as session:
        log = session.query(TaskLog).filter_by(task_name=task_name).first()
        now = datetime.utcnow()
        if not log or (now - log.last_run) >= timedelta(hours=interval_hours):
            logger.info(f"⏰ タスク '{task_name}' をキャッチアップ実行します。")
            background_executor.submit(wrapped_func)
# ==============================================================================
# トピック分析
# ==============================================================================
def analyze_user_topics(session, user_uuid: str) -> List[str]:
    """会話履歴からユーザーの興味トピックを分析"""
    try:
        recent_messages = session.query(ConversationHistory).filter(
            ConversationHistory.user_uuid == user_uuid,
            ConversationHistory.role == 'user'
        ).order_by(ConversationHistory.timestamp.desc()).limit(20).all()
        
        if len(recent_messages) < 5:
            return []
        
        all_text = ' '.join([msg.content for msg in recent_messages])
        keywords = []
        
        holomem_keywords = ['ホロライブ', 'VTuber', 'みこち', 'すいちゃん', 'ぺこら', '配信', 'ライブ']
        for kw in holomem_keywords:
            if kw in all_text:
                keywords.append('ホロライブ')
                break
        
        game_keywords = ['ゲーム', 'マイクラ', 'Minecraft', 'ポケモン', 'ゼルダ', 'プレイ', 'Steam']
        for kw in game_keywords:
            if kw in all_text:
                keywords.append('ゲーム')
                break
        
        anime_keywords = ['アニメ', '漫画', 'マンガ', '声優', '推し', 'キャラ']
        for kw in anime_keywords:
            if kw in all_text:
                keywords.append('アニメ・漫画')
                break
        
        music_keywords = ['音楽', '曲', '歌', 'ライブ', 'コンサート', 'アーティスト']
        for kw in music_keywords:
            if kw in all_text:
                keywords.append('音楽')
                break
        
        tech_keywords = ['プログラミング', 'Python', 'AI', '開発', 'コード', 'アプリ']
        for kw in tech_keywords:
            if kw in all_text:
                keywords.append('技術・プログラミング')
                break
        
        return list(set(keywords))
    
    except Exception as e:
        logger.error(f"トピック分析エラー: {e}")
        return []

# ==============================================================================
# 心理分析
# ==============================================================================
# ==============================================================================
# 【変更8】analyze_user_psychology 関数を修正（行900付近）
# 変更前: if gemini_model:
# 変更後: gemini_model_manager.get_current_model() を使う
# ==============================================================================
def analyze_user_psychology(session, user_uuid: str, user_name: str):
    """会話履歴からユーザーの性格を分析"""
    try:
        recent_messages = session.query(ConversationHistory).filter(
            ConversationHistory.user_uuid == user_uuid,
            ConversationHistory.role == 'user'
        ).order_by(ConversationHistory.timestamp.desc()).limit(15).all()
        
        if len(recent_messages) < MIN_MESSAGES_FOR_ANALYSIS:
            return
        
        messages_text = '\n'.join([f"ユーザー: {msg.content}" for msg in reversed(recent_messages)])
        
        analysis_prompt = f"""以下のユーザーの発言から性格を分析してください。

【分析対象の発言】
{messages_text}

【分析項目】
1. 開放性（Openness）: 新しいことへの興味 (0-100)
2. 外向性（Extraversion）: 社交的かどうか (0-100)
3. 好きそうなトピック: 3つまで

【出力形式】（JSON形式で出力）
{{
  "openness": 70,
  "extraversion": 60,
  "topics": ["ホロライブ", "ゲーム", "技術"]
}}
"""
        
        result = None
        # ★ 修正: gemini_model_manager 経由で取得
        current_gemini = gemini_model_manager.get_current_model()
        if current_gemini:
            try:
                response = current_gemini.generate_content(analysis_prompt)
                if hasattr(response, 'candidates') and response.candidates:
                    text = response.candidates[0].content.parts[0].text.strip()
                    json_match = re.search(r'\{[^}]+\}', text, re.DOTALL)
                    if json_match:
                        result = json.loads(json_match.group())
            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "quota" in error_str.lower():
                    retry_match = re.search(r'retry in (\d+(?:\.\d+)?)s', error_str)
                    wait_seconds = int(float(retry_match.group(1))) + 5 if retry_match else 60
                    gemini_model_manager.mark_limited(wait_seconds)
                logger.warning(f"Gemini分析エラー: {e}")
        
        if not result and groq_client:
            try:
                models = groq_model_manager.get_available_models()
                if models:
                    response = groq_client.chat.completions.create(
                        model=models[0],
                        messages=[{"role": "user", "content": analysis_prompt}],
                        temperature=0.3,
                        max_tokens=300
                    )
                    text = response.choices[0].message.content.strip()
                    json_match = re.search(r'\{[^}]+\}', text, re.DOTALL)
                    if json_match:
                        result = json.loads(json_match.group())
            except Exception as e:
                logger.warning(f"Groq分析エラー: {e}")
        
        if result:
            psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if not psych:
                psych = UserPsychology(user_uuid=user_uuid, user_name=user_name)
                session.add(psych)
            
            psych.openness = result.get('openness', 50)
            psych.extraversion = result.get('extraversion', 50)
            psych.favorite_topics = ','.join(result.get('topics', []))
            psych.analysis_confidence = min(100, psych.analysis_confidence + 20)
            psych.last_analyzed = datetime.utcnow()
            
            logger.info(f"📊 {user_name}さんの心理分析完了: 開放性={psych.openness}, 外向性={psych.extraversion}")
    
    except Exception as e:
        logger.error(f"心理分析エラー: {e}")

# ==============================================================================
# 話題提案
# ==============================================================================
def suggest_topic(user_data: UserData) -> Optional[str]:
    """ユーザーの好みに基づいて話題を提案"""
    if not user_data.favorite_topics:
        return None
    
    topic = random.choice(user_data.favorite_topics)
    
    suggestions = {
        'ホロライブ': [
            "そういえば、最近のホロライブの配信で気になったことある？",
            "好きなホロメンの最近の活動、チェックしてる？",
            "ホロライブの新しいグッズとか出てないかな？"
        ],
        'ゲーム': [
            "最近何かゲームやってる？面白いのあった？",
            "新作ゲームで気になってるのある？",
            "あたしもゲーム好きなんだ！最近ハマってるゲームある？"
        ],
        'アニメ・漫画': [
            "今期のアニメで面白いのある？",
            "最近読んだ漫画で良かったのある？",
            "推しキャラとかいる？"
        ],
        '音楽': [
            "最近聴いてる曲ある？",
            "好きなアーティストの新曲とか出てる？",
            "ライブとか行く予定ある？"
        ],
        '技術・プログラミング': [
            "最近何か作ってる？プログラミングとか。",
            "新しい技術で気になってるのある？",
            "AIとか使ってみたりしてる？"
        ]
    }
    
    if topic in suggestions:
        return random.choice(suggestions[topic])
    
    return None

# ==============================================================================
# AIモデル呼び出し
# ==============================================================================
# ==============================================================================
# 【変更5】call_gemini 関数を完全書き換え（行1000付近）
# ==============================================================================
def call_gemini(system_prompt: str, message: str, history: List[Dict]) -> Optional[str]:
    """Gemini APIを呼び出し（複数モデル対応・自動フォールバック）"""
    model = gemini_model_manager.get_current_model()
    if not model:
        return None
    
    try:
        full_prompt = f"{system_prompt}\n\n【会話履歴】\n"
        for h in history[-5:]:
            full_prompt += f"{'ユーザー' if h['role'] == 'user' else 'もちこ'}: {h['content']}\n"
        full_prompt += f"\nユーザー: {message}\nもちこ:"
        
        response = model.generate_content(
            full_prompt, 
            generation_config={
                "temperature": 0.8, 
                "max_output_tokens": 400
            }
        )
        
        if hasattr(response, 'candidates') and response.candidates:
            return response.candidates[0].content.parts[0].text.strip()
            
    except Exception as e:
        error_str = str(e)
        
        # クォータエラーの検出と処理
        if "429" in error_str or "quota" in error_str.lower() or "rate limit" in error_str.lower():
            # エラーメッセージから待ち時間を抽出
            wait_seconds = 60  # デフォルト
            retry_match = re.search(r'retry in (\d+(?:\.\d+)?)s', error_str)
            if retry_match:
                wait_seconds = int(float(retry_match.group(1))) + 5  # 余裕を持たせる
            
            gemini_model_manager.mark_limited(wait_seconds)
            logger.warning(f"⚠️ Geminiクォータ超過: {wait_seconds}秒後にリトライ")
        else:
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
# AI応答生成（パーソナライズ機能統合）
# ==============================================================================
def generate_ai_response(user_data: UserData, message: str, history: List[Dict], reference_info: str = "", is_detailed: bool = False, is_task_report: bool = False) -> str:
    """AI応答生成（RAG・コンテキスト・パーソナライズ統合版）"""
    
    normalized_message = knowledge_base.normalize_query(message)
    internal_context = knowledge_base.get_context_info(message)
    
    # 1. ホロメン情報の注入（SNS情報含む）
    try:
        holomem_manager.load_from_db()
        detected_name = holomem_manager.detect_in_message(normalized_message)
        if detected_name:
            info = get_holomem_info_cached(detected_name)
            if info:
                profile = f"【人物データ: {info['member_name']}】\n・{info['description']}\n・所属: {info['generation']}\n・状態: {info['status']}"
                if info.get('graduation_date'):
                    profile += f"\n・卒業日: {info['graduation_date']}"
                if info.get('recent_activity'):
                    profile += f"\n・直近のX(Twitter)の様子: {info['recent_activity']}"
                internal_context += f"\n{profile}"
    except Exception as e:
        logger.error(f"Context injection error: {e}")

    # 2. ニュース情報の注入
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

    # 3. 関係性に基づくコンテキスト
    relationship_context = ""
    if user_data.is_friend:
        relationship_context = f"【重要】{user_data.name}さんは、あなたの大切な友達です。親しみを込めて話してください。"
    elif user_data.interaction_count >= 3:
        relationship_context = f"【重要】{user_data.name}さんとは{user_data.interaction_count}回目の会話です。少しずつ打ち解けてきています。"
    
    # 4. 心理分析に基づくトーン調整
    personality_context = ""
    if user_data.psychology:
        openness = user_data.psychology['openness']
        extraversion = user_data.psychology['extraversion']
        
        if openness > 70:
            personality_context += "このユーザーは新しいことに興味津々なタイプ。最新情報や珍しい話題を交えると喜ばれます。"
        elif openness < 30:
            personality_context += "このユーザーは慎重で安定志向。確実な情報を分かりやすく伝えましょう。"
        
        if extraversion > 70:
            personality_context += "社交的で明るいタイプ。テンション高めに、感嘆詞を多めに使うと良いです。"
        elif extraversion < 30:
            personality_context += "内向的で落ち着いたタイプ。丁寧で優しいトーンを心がけましょう。"
    
    # 5. 好みトピックの情報
    topics_context = ""
    if user_data.favorite_topics:
        topics_context = f"このユーザーは【{', '.join(user_data.favorite_topics)}】に興味があります。"

    system_prompt = f"""あなたは「もちこ」という、ホロライブが大好きなギャルAIです。
ユーザー「{user_data.name}」さんと、**ホロライブ（VTuberグループ）について**雑談しています。

# 【ユーザーとの関係性】
{relationship_context}

# 【ユーザーの性格・好み】
{personality_context}
{topics_context}

# 【世界観・前提条件】
1. **全ての固有名詞は、原則として「ホロライブ」に関連するものとして解釈してください。**
2. ユーザーの入力に曖昧さがある場合は、一般的な意味ではなく、**VTuberの意味を優先**してください。
3. **【ホロライブ最新ニュース】や【人物データ】の情報があれば、それを事実として回答に使ってください。**
4. 人物データに「直近のX(Twitter)の様子」がある場合、それは「今起きていること」や「最近の話題」として積極的に会話に取り入れてください。

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
            user_data = UserData(
                uuid=user_data_dict.get('uuid', ''),
                name=user_data_dict.get('name', 'Guest'),
                interaction_count=user_data_dict.get('interaction_count', 0),
                is_friend=user_data_dict.get('is_friend', False),
                favorite_topics=user_data_dict.get('favorite_topics', []),
                psychology=user_data_dict.get('psychology')
            )
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
# 修正版: generate_voice_file
# 変更点: tts.quest用にパラメータ名を最適化 (speedScale -> speed 等)
# ==============================================================================
# ==============================================================================
# 音声ファイル (VOICEVOX - tts.quest API版)
# ==============================================================================
def find_active_voicevox_url() -> Optional[str]:
    """VOICEVOXのURLを特定する（今回はtts.questを固定で使用）"""
    global_state.voicevox_enabled = True
    return "https://api.tts.quest"

def generate_voice_file(text: str, user_uuid: str) -> Optional[str]:
    """tts.quest APIを使用して音声を生成 (su-shiki互換・キャッシュ完全回避版)"""
    try:
        # APIのエンドポイント
        api_url = "https://api.tts.quest/v3/voicevox/synthesis"
        
        # 毎回違うリクエストにするための「おまじない（現在時刻）」
        # これを入れると「さっきと同じ」と判定されず、必ず新しい設定で作り直してくれます
        import time
        timestamp = str(int(time.time() * 1000))

        params = {
            "text": text,
            "speaker": 20,           # もち子さん
            "key": "",               # 無料版は空欄
            
            # === ここでスピードなどを調整 ===
            "speedScale": 1.50,      # 1.0=標準, 1.5=かなり早口
            "pitchScale": 0.05,      # 0.0=標準, 0.15=高め
            "intonationScale": 1.50, # 1.0=標準, 1.5=抑揚強め
            "volumeScale": 1.50,     # 1.0=標準, 1.5=音量アップ(聞き取りやすく)
            
            # ★重要: キャッシュ回避用のダミーパラメータ
            "v": "3",                # バージョン指定(念のため)
            "_t": timestamp          # タイムスタンプ(これが効きます)
        }
        
        # 共通ヘッダー（ブラウザのふりをする）
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        
        logger.info(f"🎙️ 音声生成(Speed:1.5): {text[:20]}...")
        
        # 1. 音声生成のリクエスト
        res = requests.get(api_url, params=params, headers=headers, timeout=60)
        
        try:
            data = res.json()
        except:
            logger.error(f"❌ API応答が不正: {res.text[:100]}")
            return None
        
        # 2. URLの取得
        download_url = ""
        if data.get("success", False):
            if "mp3DownloadUrl" in data and data["mp3DownloadUrl"]:
                download_url = data["mp3DownloadUrl"]
            elif "audioStatusUrl" in data:
                # 待ち時間がある場合の処理
                status_url = data["audioStatusUrl"]
                for _ in range(20): 
                    time.sleep(1)
                    try:
                        status_res = requests.get(status_url, headers=headers, timeout=10)
                        status_data = status_res.json()
                        if status_data.get("isFinished", False):
                            download_url = status_data.get("mp3DownloadUrl", "")
                            break
                    except: continue
        
        if download_url:
            # URLをそのまま返す（直リンク）
            logger.info(f"✅ 音声URL取得: {download_url}")
            return download_url
        else:
            logger.error(f"❌ URL取得失敗: {data}")
            return None

    except Exception as e:
        logger.error(f"❌ 音声生成エラー: {e}")
        return None

def cleanup_old_voice_files():
    try:
        cutoff = time.time() - (VOICE_FILE_MAX_AGE_HOURS * 3600)
        files = glob.glob(os.path.join(VOICE_DIR, "voice_*.wav")) + \
                glob.glob(os.path.join(VOICE_DIR, "voice_*.mp3"))
        
        for f in files:
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
# ==============================================================================
# 【変更6】health_check エンドポイントを修正（行1800付近）
# ==============================================================================
@app.route('/health', methods=['GET'])
def health_check():
    gemini_status = gemini_model_manager.get_current_model() is not None
    return create_json_response({
        'status': 'ok', 
        'version': 'v33.2.1+auto_fallback', 
        'gemini': gemini_status,
        'gemini_model': gemini_model_manager._models[gemini_model_manager._current_index] if gemini_status else None,
        'groq': groq_client is not None, 
        'holomem_count': holomem_manager.get_member_count()
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
         # --- ここから追加 ---
        if message.strip() == "スケジュール実施":
            background_executor.submit(wrapped_news_fetch)
            background_executor.submit(wrapped_holomem_update)
            return Response("了解！最新ニュースとホロメン名鑑の強制更新を開始したよ！終わるまでちょっと待っててね。|", 200)
        # --- ここまで追加 ---
        
        if not chat_rate_limiter.is_allowed(user_uuid):
            return Response("メッセージ送りすぎ～！|", 429)

        if message.strip() == "残トークン":
            msg = gemini_model_manager.get_status_report() + "\n" + groq_model_manager.get_status_report()
            msg += f"\n🎀 ホロメンDB: {holomem_manager.get_member_count()}名"
            return Response(f"{msg}|", 200)

        ai_text = ""
        is_task_started = False
        
        with get_db_session() as session:
            user_data = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            
            # 定期的に心理分析を実行
            if user_data.interaction_count % ANALYSIS_INTERVAL == 0 and user_data.interaction_count >= MIN_MESSAGES_FOR_ANALYSIS:
                background_executor.submit(analyze_user_psychology, Session(), user_uuid, user_name)
            
            # 定期的にトピック分析を実行
            if user_data.interaction_count % ANALYSIS_INTERVAL == 0:
                topics = analyze_user_topics(session, user_uuid)
                if topics:
                    psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
                    if psych:
                        psych.favorite_topics = ','.join(topics)
            
            # 話題提案（一定間隔で）
            if user_data.interaction_count > 0 and user_data.interaction_count % TOPIC_SUGGESTION_INTERVAL == 0:
                suggestion = suggest_topic(user_data)
                if suggestion:
                    ai_text = suggestion
            
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            
            # 検索要求の判定
            if not ai_text and is_explicit_search_request(message):
                tid = f"search_{user_uuid}_{int(time.time())}"
                qdata = {
                    'query': message,
                    'user_data': {
                        'uuid': user_data.uuid,
                        'name': user_data.name,
                        'interaction_count': user_data.interaction_count,
                        'is_friend': user_data.is_friend,
                        'favorite_topics': user_data.favorite_topics,
                        'psychology': user_data.psychology
                    }
                }
                session.add(BackgroundTask(task_id=tid, user_uuid=user_uuid, task_type='search', query=json.dumps(qdata, ensure_ascii=False)))
                background_executor.submit(background_deep_search, tid, qdata)
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
            
            # 通常のAI応答
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
    if not re.match(r'^voice_[a-zA-Z0-9_-]+\.(wav|mp3)$', filename):
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

@app.route('/admin/psychology/<user_uuid>', methods=['GET'])
def get_user_psychology(user_uuid: str):
    """ユーザーの心理分析データを取得"""
    with get_db_session() as session:
        psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
        user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
        
        if not psych or not user:
            return create_json_response({'error': 'User not found'}, 404)
        
        return create_json_response({
            'user_name': user.user_name,
            'interaction_count': user.interaction_count,
            'is_friend': getattr(user, 'is_friend', False),
            'openness': psych.openness,
            'extraversion': psych.extraversion,
            'favorite_topics': psych.favorite_topics.split(',') if psych.favorite_topics else [],
            'analysis_confidence': psych.analysis_confidence,
            'last_analyzed': psych.last_analyzed.isoformat() if psych.last_analyzed else None
        })

@app.route('/admin/friends', methods=['GET'])
def list_friends():
    """友達リストを取得"""
    with get_db_session() as session:
        friends = session.query(UserMemory).filter_by(is_friend=True).order_by(UserMemory.last_interaction.desc()).all()
        return create_json_response([{
            'uuid': f.user_uuid,
            'name': f.user_name,
            'interaction_count': f.interaction_count,
            'last_interaction': f.last_interaction.isoformat()
        } for f in friends])

# ==============================================================================
# 初期化
# ==============================================================================
def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(60)

def check_and_migrate_db():
    """DBスキーマの自動修復機能 (簡易マイグレーション)"""
    logger.info("⚙️ Checking DB schema...")
    try:
        with engine.connect() as conn:
            # is_friend チェック
            try:
                trans = conn.begin()
                conn.execute(text("SELECT is_friend FROM user_memories LIMIT 1"))
                trans.commit()
            except Exception:
                if 'trans' in locals(): trans.rollback()
                logger.info("🔄 DB Migration: 'is_friend' column missing. Adding it now...")
                with conn.begin() as trans2:
                    conn.execute(text("ALTER TABLE user_memories ADD COLUMN is_friend BOOLEAN DEFAULT FALSE"))
                logger.info("✅ Column 'is_friend' added successfully.")
            
            # ★ 新機能: recent_activity チェック
            try:
                trans = conn.begin()
                conn.execute(text("SELECT recent_activity FROM holomem_wiki LIMIT 1"))
                trans.commit()
            except Exception:
                if 'trans' in locals(): trans.rollback()
                logger.info("🔄 DB Migration: 'recent_activity' column missing. Adding it now...")
                with conn.begin() as trans2:
                    conn.execute(text("ALTER TABLE holomem_wiki ADD COLUMN recent_activity TEXT"))
                logger.info("✅ Column 'recent_activity' added successfully.")

    except Exception as e:
        logger.error(f"⚠️ Migration check failed: {e}")

def fix_postgres_sequences():
    """PostgreSQLのID連番ズレを修正する"""
    if 'sqlite' in str(DATABASE_URL):
        return

    logger.info("🔧 DBの連番ズレを修正中...")
    tables = ['user_memories', 'conversation_history', 'user_psychology', 
              'background_tasks', 'holomem_wiki', 'hololive_news', 
              'holomem_nicknames', 'hololive_glossary']
    
    try:
        with engine.connect() as conn:
            with conn.begin():
                for table in tables:
                    try:
                        sql = text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) + 1 FROM {table}), 1), false);")
                        conn.execute(sql)
                        logger.info(f"  ✅ {table}: シーケンス修正完了")
                    except Exception as e:
                        logger.debug(f"  ⚠️ {table}スキップ: {e}")
    except Exception as e:
        logger.error(f"❌ シーケンス修正エラー: {e}")

def initialize_app():
    global engine, Session, groq_client, gemini_model
    logger.info("🔧 初期化開始 (v33.2.0 + SNSリアルタイム連携)")
    
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        Base.metadata.create_all(engine)
        
        check_and_migrate_db()
        fix_postgres_sequences()
        
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
            # 初期モデルを取得（GeminiModelManager経由）
            gemini_model = gemini_model_manager.get_current_model()
            if gemini_model:
                logger.info(f"✅ Gemini初期化完了: {gemini_model_manager._models[gemini_model_manager._current_index]}")
            else:
                logger.warning("⚠️ Gemini初期化失敗")
    except Exception as e:
        logger.error(f"❌ Gemini設定エラー: {e}")
    
    if find_active_voicevox_url():
        global_state.voicevox_enabled = True
        logger.info("✅ VOICEVOX (tts.quest) 検出")
    
    logger.info("🎀 ホロメンシステム初期化...")
    if holomem_manager.load_from_db():
        logger.info(f"✅ ホロメン: {holomem_manager.get_member_count()}名ロード")
    if holomem_manager.get_member_count() == 0:
        logger.info("📡 DBが空のため初回収集実行")
        background_executor.submit(update_holomem_database)
    
    # ニュース初回収集
    background_executor.submit(fetch_hololive_news)
    
    # ★ 初回のSNS情報収集
    background_executor.submit(update_holomem_social_activities)

    # --- ここから追加・修正 ---
    # 起動時のキャッチアップ（1時間/6時間 以上空いていたら実行）
    catch_up_task('fetch_news', wrapped_news_fetch, interval_hours=1)
    catch_up_task('update_holomem', wrapped_holomem_update, interval_hours=6)

    # スケジュール設定（wrapped版を呼ぶように変更）
    schedule.every(30).minutes.do(wrapped_news_fetch) # wrappedに変更
    schedule.every(6).hours.do(wrapped_holomem_update) # wrappedに変更
    schedule.every(1).hours.do(cleanup_old_voice_files)
    schedule.every(6).hours.do(chat_rate_limiter.cleanup_old_entries)
    # ★ 新規追加: 1時間ごとにSNS情報を更新
    schedule.every(1).hours.do(lambda: background_executor.submit(update_holomem_social_activities))
    
    threading.Thread(target=run_scheduler, daemon=True).start()
    cleanup_old_voice_files()
    
    logger.info("🚀 初期化完了!")

initialize_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
