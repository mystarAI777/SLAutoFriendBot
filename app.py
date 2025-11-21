# ==============================================================================
# もちこAI - 全機能統合版 (v31.0 - Bug Fix & Improvement Edition)
#
# 変更点 (v30.0 -> v31.0):
# 1. 心理分析結果のDB保存処理を実装
# 2. キャッシュ関数のセッション問題を修正（辞書で返すように変更）
# 3. バックグラウンドタスクのセッション管理を改善
# 4. Groqモデル状態管理をスレッドセーフに改善
# 5. 音声ファイル自動クリーンアップ機能を追加
# 6. 例外処理の強化とエラーログの改善
# 7. 会話履歴取得の効率化
# 8. レート制限のリセットロジックを改善
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
SL_SAFE_CHAR_LIMIT = 250
MIN_MESSAGES_FOR_ANALYSIS = 10
SEARCH_TIMEOUT = 15
VOICE_FILE_MAX_AGE_HOURS = 24  # 音声ファイル保持時間

# Groqで使用するモデルリスト（優先度順）
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
    "gemma2-9b-it"
]

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
]

LOCATION_CODES = {
    "東京": "130000", "大阪": "270000", "名古屋": "230000",
    "福岡": "400000", "札幌": "016000"
}

SPECIALIZED_SITES = {
    'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー']},
    'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG', 'CG業界']},
    '脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学', '認知科学']},
    'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']},
    'アニメ': {'base_url': 'https://animedb.jp/', 'keywords': ['アニメ', 'anime', 'アニメーション', '声優']}
}

HOLOMEM_KEYWORDS = [
    'ときのそら', 'ロボ子さん', 'さくらみこ', 'みこち', '星街すいせい', 'すいちゃん',
    'AZKi', '夜空メル', 'アキ・ローゼンタール', '赤井はあと', '白上フブキ', '夏色まつり',
    '湊あくあ', '紫咲シオン', '百鬼あやめ', '癒月ちょこ', '大空スバル', '大神ミオ',
    '猫又おかゆ', 'おかゆん', '戌神ころね', 'ころさん', '兎田ぺこら', 'ぺこーら',
    '不知火フレア', '白銀ノエル', '宝鐘マリン', '船長', '天音かなた', '角巻わため',
    '常闘トワ', '姫森ルーナ', '雪花ラミィ', '桃鈴ねね', '獅白ぼたん', '尾丸ポルカ',
    'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより', '沙花叉クロヱ', '風真いろは',
    '森カリオペ', '小鳥遊キアラ', '一伊那尓栖', 'がうる・ぐら', 'サメちゃん',
    'ワトソン・アメリア', 'IRyS', 'セレス・ファウナ', 'オーロ・クロニー', '七詩ムメイ',
    'ハコス・ベールズ', 'シオリ・ノヴェラ', '古石ビジュー', 'ネリッサ・レイヴンクロフト',
    'フワワ・アビスガード', 'モココ・アビスガード', 'アユンダ・リス', 'ムーナ・ホシノヴァ',
    'アイラニ・イオフィフティーン', 'クレイジー・オリー', 'アーニャ・メルフィッサ',
    'パヴォリア・レイネ', '火威青', '音乃瀬奏', '一条莉々華', '儒烏風亭らでん',
    '轟はじめ', 'ホロライブ', 'ホロメン', 'hololive', 'YAGOO', '桐生ココ',
    '潤羽るしあ', '魔乃アロエ', '九十九佐命'
]

ANIME_KEYWORDS = ['アニメ', 'anime', 'アニメーション', '作画', '声優', 'OP', 'ED', '劇場版', '映画', '原作', '漫画', 'ラノベ']

VOICEVOX_URLS = [
    'http://voicevox-engine:50021', 'http://voicevox:50021',
    'http://127.0.0.1:50021', 'http://localhost:50021'
]

# ==============================================================================
# データクラス（型安全性向上）
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
# グローバル状態管理（スレッドセーフ改善版）
# ==============================================================================
class GlobalState:
    def __init__(self):
        self._lock = RLock()
        self._voicevox_enabled = False
        self._active_voicevox_url = None

    @property
    def voicevox_enabled(self) -> bool:
        with self._lock:
            return self._voicevox_enabled

    @voicevox_enabled.setter
    def voicevox_enabled(self, value: bool):
        with self._lock:
            self._voicevox_enabled = value

    @property
    def active_voicevox_url(self) -> Optional[str]:
        with self._lock:
            return self._active_voicevox_url

    @active_voicevox_url.setter
    def active_voicevox_url(self, value: Optional[str]):
        with self._lock:
            self._active_voicevox_url = value


class GroqModelManager:
    """Groqモデルの状態をスレッドセーフに管理"""
    
    def __init__(self, models: List[str]):
        self._lock = RLock()
        self._status: Dict[str, GroqModelStatus] = {
            model: GroqModelStatus() for model in models
        }
        self._models = models

    def is_available(self, model: str) -> bool:
        with self._lock:
            status = self._status.get(model)
            if not status:
                return False
            if not status.is_limited:
                return True
            if status.reset_time and datetime.utcnow() >= status.reset_time:
                status.is_limited = False
                status.reset_time = None
                status.last_error = None
                logger.info(f"✅ {model} の制限が解除されました")
                return True
            return False

    def mark_limited(self, model: str, wait_minutes: int = 5, error_msg: str = ""):
        with self._lock:
            if model in self._status:
                self._status[model].is_limited = True
                self._status[model].reset_time = datetime.utcnow() + timedelta(minutes=wait_minutes)
                self._status[model].last_error = error_msg
                logger.warning(f"⚠️ {model} を{wait_minutes}分間制限")

    def get_status_report(self) -> str:
        with self._lock:
            lines = ["🦙 Groq モデル稼働状況:"]
            for model in self._models:
                status = self._status[model]
                if status.is_limited:
                    reset = status.reset_time
                    if reset:
                        jst = (reset + timedelta(hours=9)).strftime('%H:%M:%S')
                        lines.append(f"  ❌ {model}: 制限中 (解除予定: {jst})")
                    else:
                        lines.append(f"  ❌ {model}: 制限中")
                else:
                    lines.append(f"  ✅ {model}: OK")
            return "\n".join(lines)

    def get_available_models(self) -> List[str]:
        with self._lock:
            return [m for m in self._models if self.is_available(m)]


# グローバルインスタンス
global_state = GlobalState()
groq_model_manager = GroqModelManager(GROQ_MODELS)
background_executor = ThreadPoolExecutor(max_workers=5)

# AI クライアント
groq_client: Optional[Groq] = None
gemini_model = None
engine = None
Session = None

# Flask アプリケーション
app = Flask(__name__)
application = app
app.config['JSON_AS_ASCII'] = False
CORS(app)
Base = declarative_base()

# ==============================================================================
# 秘密情報/環境変数 読み込み
# ==============================================================================
def get_secret(name: str) -> Optional[str]:
    env_value = os.environ.get(name)
    if env_value and env_value.strip():
        return env_value.strip()
    try:
        secret_file_path = f"/etc/secrets/{name}"
        if os.path.exists(secret_file_path):
            with open(secret_file_path, 'r') as f:
                file_value = f.read().strip()
                if file_value:
                    return file_value
    except Exception:
        pass
    return None

DATABASE_URL = get_secret('DATABASE_URL') or 'sqlite:///./mochiko_ultimate.db'
GROQ_API_KEY = get_secret('GROQ_API_KEY')
GEMINI_API_KEY = get_secret('GEMINI_API_KEY')
VOICEVOX_URL_FROM_ENV = get_secret('VOICEVOX_URL')
WEATHER_API_KEY = get_secret('WEATHER_API_KEY')

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
    __table_args__ = (
        Index('idx_user_timestamp', 'user_uuid', 'timestamp'),
    )


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
    conversation_style = Column(String(100), nullable=True)
    emotional_tendency = Column(String(100), nullable=True)
    analysis_summary = Column(Text, nullable=True)
    total_messages = Column(Integer, default=0)
    avg_message_length = Column(Integer, default=0)
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

# ==============================================================================
# 例外クラス
# ==============================================================================
class MochikoException(Exception):
    """もちこAI基底例外"""
    pass

class AIModelException(MochikoException):
    """AIモデル関連の例外"""
    pass

class DatabaseException(MochikoException):
    """データベース関連の例外"""
    pass

class RateLimitException(MochikoException):
    """レート制限例外"""
    pass

# ==============================================================================
# セキュリティ & レート制限
# ==============================================================================
class RateLimiter:
    def __init__(self, max_requests: int, time_window: timedelta):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests: Dict[str, List[datetime]] = defaultdict(list)
        self._lock = threading.Lock()

    def is_allowed(self, user_id: str) -> bool:
        with self._lock:
            now = datetime.utcnow()
            cutoff = now - self.time_window
            self.requests[user_id] = [
                req_time for req_time in self.requests[user_id]
                if req_time > cutoff
            ]
            if len(self.requests[user_id]) >= self.max_requests:
                return False
            self.requests[user_id].append(now)
            return True

    def cleanup_old_entries(self):
        """古いエントリをクリーンアップ"""
        with self._lock:
            now = datetime.utcnow()
            cutoff = now - self.time_window
            empty_users = []
            for user_id, times in self.requests.items():
                self.requests[user_id] = [t for t in times if t > cutoff]
                if not self.requests[user_id]:
                    empty_users.append(user_id)
            for user_id in empty_users:
                del self.requests[user_id]


chat_rate_limiter = RateLimiter(max_requests=10, time_window=timedelta(minutes=1))


def sanitize_user_input(text: str, max_length: int = 1000) -> str:
    if not text:
        return ""
    text = text[:max_length]
    text = escape(text)
    dangerous_patterns = [
        r'<script[^>]*>.*?</script>',
        r'javascript:',
        r'on\w+\s*=',
    ]
    for pattern in dangerous_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL)
    return text.strip()


def mask_uuid(uuid_str: str) -> str:
    if len(uuid_str) > 8:
        return f"{uuid_str[:4]}****{uuid_str[-4:]}"
    return "****"

# ==============================================================================
# セッション管理
# ==============================================================================
@contextmanager
def get_db_session():
    if not Session:
        logger.error("❌ データベースSessionが初期化されていません")
        raise DatabaseException("Database Session is not initialized.")
    session = Session()
    try:
        yield session
        session.commit()
    except Exception as e:
        logger.error(f"❌ DBエラー: {type(e).__name__}: {e}")
        session.rollback()
        raise DatabaseException(f"DB operation failed: {e}")
    finally:
        session.close()

# ==============================================================================
# ユーティリティ関数
# ==============================================================================
def create_json_response(data: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(data, ensure_ascii=False),
        mimetype='application/json; charset=utf-8',
        status=status
    )


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def limit_text_for_sl(text: str, max_length: int = SL_SAFE_CHAR_LIMIT) -> str:
    if len(text) > max_length:
        return text[:max_length - 3] + "..."
    return text


def get_japan_time() -> str:
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    return f"今の日本の時間は、{now.strftime('%Y年%m月%d日 %H時%M分')}だよ！"


def is_time_request(message: str) -> bool:
    keywords = ['今何時', '時刻', '何時', 'なんじ']
    return any(kw in message for kw in keywords)


def is_weather_request(message: str) -> bool:
    keywords = ['今日の天気', '明日の天気', '天気予報', '天気は']
    return any(kw in message for kw in keywords)


def is_hololive_request(message: str) -> bool:
    return any(kw in message for kw in HOLOMEM_KEYWORDS)


def is_anime_request(message: str) -> bool:
    return any(kw in message for kw in ANIME_KEYWORDS)


def detect_specialized_topic(message: str) -> Optional[str]:
    for topic, config in SPECIALIZED_SITES.items():
        if any(kw in message for kw in config['keywords']):
            return topic
    return None


def is_explicit_search_request(message: str) -> bool:
    keywords = ['調べて', '検索して', '探して', 'とは', 'って何', 'について', '教えて', 'おすすめ']
    return any(kw in message for kw in keywords)


def is_short_response(message: str) -> bool:
    normalized = message.strip().lower()
    short_responses = ['うん', 'そう', 'はい', 'そっか', 'なるほど', 'おけ', 'ok', '了解']
    return len(normalized) <= 5 or normalized in short_responses


def extract_location(message: str) -> str:
    for location in LOCATION_CODES.keys():
        if location in message:
            return location
    return "東京"


def detect_db_correction_request(message: str) -> Optional[Dict]:
    pattern = r"(.+?)(?:(?:の|に関する)(?:情報|データ))?(?:で|、|だけど|ですが)、?「(.+?)」は「(.+?)」が正しいよ"
    match = re.search(pattern, message)
    if match:
        member_name_raw, field_raw, value_raw = match.groups()
        member_name = sanitize_user_input(member_name_raw.strip())
        field = sanitize_user_input(field_raw.strip())
        value = sanitize_user_input(value_raw.strip())
        
        field_map = {
            '説明': 'description',
            'デビュー日': 'debut_date',
            '期': 'generation',
            'タグ': 'tags',
            'ステータス': 'status',
            '卒業日': 'graduation_date',
            'もちこの気持ち': 'mochiko_feeling'
        }
        
        if member_name in HOLOMEM_KEYWORDS and field in field_map:
            return {
                'member_name': member_name,
                'field': field,
                'value': value,
                'db_field': field_map[field]
            }
    return None


def is_holomem_name_only_request_safe(message: str) -> Optional[str]:
    msg_stripped = sanitize_user_input(message.strip(), max_length=50)
    if len(msg_stripped) > 20:
        return None
    for name in HOLOMEM_KEYWORDS:
        if name == msg_stripped:
            return name
    return None

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
        user = UserMemory(
            user_uuid=user_uuid,
            user_name=user_name,
            interaction_count=1
        )
        session.add(user)
        logger.info(f"✨ 新規ユーザー作成: {user_name} (UUID: {mask_uuid(user_uuid)})")
    
    return UserData(
        uuid=user.user_uuid,
        name=user.user_name,
        interaction_count=user.interaction_count
    )


def get_conversation_history(session, user_uuid: str, limit: int = 10) -> List[Dict]:
    history_records = (
        session.query(ConversationHistory)
        .filter_by(user_uuid=user_uuid)
        .order_by(ConversationHistory.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [{'role': h.role, 'content': h.content} for h in reversed(history_records)]

# ==============================================================================
# ホロメン情報キャッシュ（修正版: 辞書で返す）
# ==============================================================================
_holomem_cache: Dict[str, Dict] = {}
_holomem_cache_lock = threading.Lock()
_holomem_cache_ttl = timedelta(minutes=30)
_holomem_cache_timestamps: Dict[str, datetime] = {}


def get_holomem_info_cached(member_name: str) -> Optional[Dict]:
    """キャッシュ付きでホロメン情報を取得（辞書で返す）"""
    with _holomem_cache_lock:
        now = datetime.utcnow()
        if member_name in _holomem_cache:
            cached_time = _holomem_cache_timestamps.get(member_name)
            if cached_time and (now - cached_time) < _holomem_cache_ttl:
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
    """キャッシュをクリア"""
    with _holomem_cache_lock:
        if member_name:
            _holomem_cache.pop(member_name, None)
            _holomem_cache_timestamps.pop(member_name, None)
        else:
            _holomem_cache.clear()
            _holomem_cache_timestamps.clear()

# ==============================================================================
# AIモデル呼び出し関数
# ==============================================================================
def _safe_get_gemini_text(response) -> Optional[str]:
    try:
        if hasattr(response, 'candidates') and response.candidates:
            candidate = response.candidates[0]
            if hasattr(candidate, 'content') and candidate.content.parts:
                return candidate.content.parts[0].text
    except (IndexError, AttributeError) as e:
        logger.warning(f"⚠️ Gemini応答解析エラー: {e}")
        if hasattr(response, 'prompt_feedback'):
            logger.warning(f"  フィードバック: {response.prompt_feedback}")
    except Exception as e:
        logger.error(f"❌ Gemini応答処理で予期しないエラー: {e}")
    return None


def call_gemini(system_prompt: str, message: str, history: List[Dict]) -> Optional[str]:
    if not gemini_model:
        return None
    try:
        full_prompt = f"{system_prompt}\n\n【会話履歴】\n"
        for h in history[-5:]:  # 直近5件に制限
            role = 'ユーザー' if h['role'] == 'user' else 'もちこ'
            full_prompt += f"{role}: {h['content']}\n"
        full_prompt += f"\nユーザー: {message}\nもちこ:"

        response = gemini_model.generate_content(
            full_prompt,
            generation_config={"temperature": 0.8, "max_output_tokens": 300}
        )
        text = _safe_get_gemini_text(response)

        if text:
            logger.debug("Gemini応答成功")
            return text.strip()
        return None
    except Exception as e:
        error_str = str(e).lower()
        if 'quota' in error_str or 'rate' in error_str:
            logger.warning(f"⚠️ Gemini レート制限: {e}")
        elif 'api key' in error_str:
            logger.error(f"❌ Gemini APIキーエラー: {e}")
        else:
            logger.warning(f"⚠️ Gemini APIエラー: {e}")
        return None


def call_groq(system_prompt: str, message: str, history: List[Dict], max_tokens: int = 800) -> Optional[str]:
    if not groq_client:
        return None

    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-5:]:  # 直近5件に制限
        messages.append({"role": h['role'], "content": h['content']})
    messages.append({"role": "user", "content": message})

    available_models = groq_model_manager.get_available_models()
    if not available_models:
        logger.error("❌ 利用可能なGroqモデルがありません")
        return None

    last_error = None
    for model_name in available_models:
        try:
            response = groq_client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0.8,
                max_tokens=max_tokens
            )
            logger.info(f"✅ Groq成功 (モデル: {model_name})")
            return response.choices[0].message.content.strip()

        except Exception as e:
            last_error = e
            error_str = str(e)

            if "Rate limit" in error_str or "429" in error_str:
                wait_minutes = 5
                wait_match = re.search(r'try again in (\d+)m?(\d*)s?', error_str)
                if wait_match:
                    mins = int(wait_match.group(1)) if wait_match.group(1) else 0
                    wait_minutes = max(mins + 1, 2)
                groq_model_manager.mark_limited(model_name, wait_minutes, error_str[:100])
                continue

            logger.error(f"❌ Groqエラー ({model_name}): {e}")
            continue

    logger.error(f"❌ 全Groqモデルが失敗: {last_error}")
    return None

# ==============================================================================
# 心理分析（修正版: DB保存実装）
# ==============================================================================
def analyze_user_psychology(user_uuid: str) -> bool:
    """ユーザーの心理分析を実行しDBに保存"""
    try:
        with get_db_session() as session:
            history = (
                session.query(ConversationHistory)
                .filter_by(user_uuid=user_uuid, role='user')
                .order_by(ConversationHistory.timestamp.desc())
                .limit(100)
                .all()
            )

            if len(history) < MIN_MESSAGES_FOR_ANALYSIS:
                logger.info(f"分析スキップ: メッセージ数不足 ({len(history)}/{MIN_MESSAGES_FOR_ANALYSIS})")
                return False

            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            user_name = user.user_name if user else "Unknown"

            messages_text = "\n".join([f"- {h.content}" for h in reversed(history)])[:2000]
            
            analysis_prompt = f"""以下のユーザー「{user_name}」の発言履歴を分析し、性格特性をJSON形式で出力してください。

発言履歴:
{messages_text}

以下のJSON形式で出力してください（数値は0-100）:
{{
    "openness": 50,
    "conscientiousness": 50,
    "extraversion": 50,
    "agreeableness": 50,
    "neuroticism": 50,
    "interests": ["興味1", "興味2"],
    "favorite_topics": ["話題1", "話題2"],
    "conversation_style": "フレンドリー",
    "emotional_tendency": "ポジティブ",
    "analysis_summary": "分析サマリー",
    "confidence": 70
}}"""

            system = "あなたは心理学の専門家です。ユーザーの発言から性格を分析し、指定されたJSON形式のみを出力してください。"
            
            response_text = call_gemini(system, analysis_prompt, [])
            if not response_text:
                response_text = call_groq(system, analysis_prompt, [], max_tokens=1024)

            if not response_text:
                logger.warning("心理分析: AI応答なし")
                return False

            json_match = re.search(r'\{[^{}]*\}', response_text, re.DOTALL)
            if not json_match:
                logger.warning(f"心理分析: JSON抽出失敗")
                return False

            try:
                analysis = json.loads(json_match.group())
            except json.JSONDecodeError as e:
                logger.warning(f"心理分析: JSONパースエラー: {e}")
                return False

            psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            if not psych:
                psych = UserPsychology(user_uuid=user_uuid, user_name=user_name)
                session.add(psych)

            psych.openness = analysis.get('openness', 50)
            psych.conscientiousness = analysis.get('conscientiousness', 50)
            psych.extraversion = analysis.get('extraversion', 50)
            psych.agreeableness = analysis.get('agreeableness', 50)
            psych.neuroticism = analysis.get('neuroticism', 50)
            psych.interests = json.dumps(analysis.get('interests', []), ensure_ascii=False)
            psych.favorite_topics = json.dumps(analysis.get('favorite_topics', []), ensure_ascii=False)
            psych.conversation_style = analysis.get('conversation_style', '')
            psych.emotional_tendency = analysis.get('emotional_tendency', '')
            psych.analysis_summary = analysis.get('analysis_summary', '')
            psych.analysis_confidence = analysis.get('confidence', 50)
            psych.total_messages = len(history)
            psych.avg_message_length = sum(len(h.content) for h in history) // len(history)
            psych.last_analyzed = datetime.utcnow()

            logger.info(f"✅ 心理分析完了・保存: {user_name} (信頼度: {psych.analysis_confidence}%)")
            return True

    except Exception as e:
        logger.error(f"❌ 心理分析エラー: {e}", exc_info=True)
        return False


def get_psychology_insight(session, user_uuid: str) -> str:
    """心理分析からインサイトを取得"""
    psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
    if not psych or (psych.analysis_confidence or 0) < 60:
        return ""

    insights = []
    if (psych.extraversion or 50) > 70:
        insights.append("社交的な")
    elif (psych.extraversion or 50) < 30:
        insights.append("内向的な")
    
    if (psych.openness or 50) > 70:
        insights.append("好奇心旺盛な")
    
    if (psych.agreeableness or 50) > 70:
        insights.append("協調性の高い")

    try:
        if psych.favorite_topics:
            topics = json.loads(psych.favorite_topics)
            if topics:
                insights.append(f"{'、'.join(topics[:2])}が好きな")
    except (json.JSONDecodeError, TypeError):
        pass

    return "".join(insights)

# ==============================================================================
# 天気情報
# ==============================================================================
def get_weather_forecast(location: str) -> str:
    code = LOCATION_CODES.get(location, "130000")
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{code}.json"
    try:
        response = requests.get(url, timeout=SEARCH_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        area = data.get('targetArea', location)
        text = clean_text(data.get('text', ''))
        return f"今の{area}の天気はね、「{text}」って感じだよ！"
    except requests.Timeout:
        logger.warning("天気API: タイムアウト")
        return "ごめん！天気情報の取得がタイムアウトしちゃった…"
    except requests.RequestException as e:
        logger.error(f"❌ 天気APIエラー: {e}")
        return "ごめん！天気情報がうまく取れなかったみたい…"
    except Exception as e:
        logger.error(f"❌ 天気処理エラー: {e}")
        return "天気情報の処理でエラーが起きちゃった…"

# ==============================================================================
# バックグラウンドタスク
# ==============================================================================
def background_db_correction(task_id: str, correction_data: Dict):
    """DBの修正をバックグラウンドで実行"""
    result = f"「{correction_data['member_name']}」の情報修正、失敗しちゃった…。"
    try:
        with get_db_session() as session:
            wiki = session.query(HolomemWiki).filter_by(
                member_name=correction_data['member_name']
            ).first()
            
            if wiki:
                db_field = correction_data.get('db_field')
                if db_field and hasattr(wiki, db_field):
                    setattr(wiki, db_field, correction_data['value'])
                    clear_holomem_cache(correction_data['member_name'])
                    result = f"おっけー！「{correction_data['member_name']}」の{correction_data['field']}を更新しといたよ！"
                    logger.info(f"✅ DB修正完了: {correction_data['member_name']}.{db_field}")
            else:
                result = f"「{correction_data['member_name']}」のデータが見つからなかったや…"

            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.result = result
                task.status = 'completed'
                task.completed_at = datetime.utcnow()

    except Exception as e:
        logger.error(f"❌ DB修正タスクエラー: {e}", exc_info=True)
        try:
            with get_db_session() as session:
                task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
                if task:
                    task.result = result
                    task.status = 'failed'
                    task.completed_at = datetime.utcnow()
        except Exception:
            pass


def scrape_major_search_engines(query: str, num_results: int = 3, site_filter: Optional[str] = None) -> List[Dict]:
    """検索エンジンからスクレイピング"""
    search_query = f"{query} site:{site_filter}" if site_filter else query
    
    engines = [
        {
            'name': 'Google',
            'url': f"https://www.google.com/search?q={quote_plus(search_query)}&hl=ja&num={num_results+2}",
            'selector': 'div.g',
            'title_sel': 'h3',
            'snippet_sel': 'div.VwiC3b'
        },
        {
            'name': 'Bing',
            'url': f"https://www.bing.com/search?q={quote_plus(search_query)}",
            'selector': 'li.b_algo',
            'title_sel': 'h2',
            'snippet_sel': 'p'
        }
    ]

    for engine in engines:
        try:
            headers = {'User-Agent': random.choice(USER_AGENTS)}
            response = requests.get(engine['url'], headers=headers, timeout=SEARCH_TIMEOUT)
            if response.status_code != 200:
                continue

            soup = BeautifulSoup(response.content, 'html.parser')
            results = []

            for elem in soup.select(engine['selector'])[:num_results]:
                title_elem = elem.select_one(engine['title_sel'])
                snippet_elem = elem.select_one(engine['snippet_sel'])
                if title_elem and snippet_elem:
                    results.append({
                        'title': clean_text(title_elem.text),
                        'snippet': clean_text(snippet_elem.text)
                    })

            if results:
                logger.info(f"✅ {engine['name']}から{len(results)}件取得")
                return results

        except requests.Timeout:
            logger.warning(f"{engine['name']}: タイムアウト")
        except Exception as e:
            logger.warning(f"{engine['name']}検索エラー: {e}")
            continue

    return []


def background_deep_search(task_id: str, query_data: Dict):
    """詳細検索をバックグラウンドで実行"""
    query = query_data.get('query', '')
    user_data_dict = query_data.get('user_data', {})
    search_result_text = f"「{query}」について調べたけど、良い情報が見つからなかったや…"

    try:
        results = scrape_major_search_engines(query, 5)
        
        if results:
            formatted_info = "【検索結果】\n" + "\n".join([
                f"・{r['title']}: {r['snippet']}" for r in results
            ])

            user_data = UserData(
                uuid=user_data_dict.get('uuid', ''),
                name=user_data_dict.get('name', 'Guest'),
                interaction_count=user_data_dict.get('interaction_count', 0)
            )

            with get_db_session() as session:
                history = get_conversation_history(session, user_data.uuid)

            search_result_text = generate_ai_response_safe(
                user_data,
                f"{query}について詳しく教えて",
                history,
                reference_info=formatted_info,
                is_detailed=True
            )

    except Exception as e:
        logger.error(f"❌ 検索タスクエラー: {e}", exc_info=True)

    try:
        with get_db_session() as session:
            task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
            if task:
                task.result = search_result_text
                task.status = 'completed'
                task.completed_at = datetime.utcnow()
    except Exception as e:
        logger.error(f"❌ タスク更新エラー: {e}")

# ==============================================================================
# AI応答生成
# ==============================================================================
def generate_ai_response(
    user_data: UserData,
    message: str,
    history: List[Dict],
    reference_info: str = "",
    is_detailed: bool = False
) -> str:
    """AI応答を生成"""
    
    with get_db_session() as session:
        personality_context = get_psychology_insight(session, user_data.uuid)

    system_prompt = f"""あなたは「もちこ」というギャルAIです。ユーザー「{user_data.name}」と会話中。

# キャラクター設定
- 一人称: 「あてぃし」
- 語尾: 「〜じゃん」「〜的な？」「〜だし」
- 性格: 明るく元気、ちょっとおバカだけど愛嬌がある
- 絵文字は控えめに

# ユーザーの印象: {personality_context if personality_context else '初対面または分析中'}

# 参考情報:
{reference_info if reference_info else 'なし'}

# 注意
- 短く親しみやすい返答を心がけて
- 難しい言葉は避けて、ギャルっぽく言い換えて
"""

    response = None

    # 1. Gemini優先
    if gemini_model:
        logger.debug("🚀 Gemini使用")
        response = call_gemini(system_prompt, message, history)

    # 2. Groqフォールバック
    if not response and groq_client:
        logger.debug("🦙 Groqにフォールバック")
        max_tokens = 1200 if is_detailed else 800
        response = call_groq(system_prompt, message, history, max_tokens=max_tokens)

    if not response:
        logger.error("❌ すべてのAIモデルが応答に失敗")
        raise AIModelException("All AI models failed to respond")

    return response


def generate_ai_response_safe(
    user_data: UserData,
    message: str,
    history: List[Dict],
    **kwargs
) -> str:
    """安全なAI応答生成（例外をキャッチ）"""
    try:
        response = generate_ai_response(user_data, message, history, **kwargs)
        if not response or response.strip() == "":
            return "うーん、ちょっと考えがまとまらないや…もう一回言ってみて？"
        return response
    except AIModelException:
        return "ごめん、今あてぃしの頭がうまく働かないみたい…ちょっと待ってからまた話しかけて？"
    except Exception as e:
        logger.critical(f"🔥 予期しないエラー: {e}", exc_info=True)
        return "システムエラーが発生しちゃった…ごめんね！"

# ==============================================================================
# 音声ファイル管理
# ==============================================================================
def find_active_voicevox_url() -> Optional[str]:
    """アクティブなVOICEVOX URLを検索"""
    urls_to_check = []
    if VOICEVOX_URL_FROM_ENV:
        urls_to_check.append(VOICEVOX_URL_FROM_ENV)
    urls_to_check.extend(VOICEVOX_URLS)

    for url in set(urls_to_check):
        if not url:
            continue
        try:
            resp = requests.get(f"{url}/version", timeout=2)
            if resp.status_code == 200:
                logger.info(f"✅ VOICEVOX発見: {url}")
                global_state.active_voicevox_url = url
                return url
        except Exception:
            pass
    return None


def generate_voice_file(text: str, user_uuid: str) -> Optional[str]:
    """音声ファイルを生成"""
    if not global_state.voicevox_enabled or not global_state.active_voicevox_url:
        return None

    try:
        url = global_state.active_voicevox_url
        text_limited = text[:200]

        query_resp = requests.post(
            f"{url}/audio_query",
            params={"text": text_limited, "speaker": VOICEVOX_SPEAKER_ID},
            timeout=10
        )
        query_resp.raise_for_status()
        query = query_resp.json()

        synth_resp = requests.post(
            f"{url}/synthesis",
            params={"speaker": VOICEVOX_SPEAKER_ID},
            json=query,
            timeout=20
        )
        synth_resp.raise_for_status()

        filename = f"voice_{user_uuid[:8]}_{int(time.time())}.wav"
        filepath = os.path.join(VOICE_DIR, filename)
        
        with open(filepath, 'wb') as f:
            f.write(synth_resp.content)

        logger.debug(f"音声ファイル生成: {filename}")
        return filename

    except requests.Timeout:
        logger.warning("VOICEVOX: タイムアウト")
    except Exception as e:
        logger.error(f"❌ 音声生成エラー: {e}")
    return None


def cleanup_old_voice_files():
    """古い音声ファイルを削除"""
    try:
        now = time.time()
        max_age_seconds = VOICE_FILE_MAX_AGE_HOURS * 3600
        deleted_count = 0

        for filepath in glob.glob(os.path.join(VOICE_DIR, "voice_*.wav")):
            try:
                file_age = now - os.path.getmtime(filepath)
                if file_age > max_age_seconds:
                    os.remove(filepath)
                    deleted_count += 1
            except OSError:
                pass

        if deleted_count > 0:
            logger.info(f"🧹 古い音声ファイル{deleted_count}件を削除")

    except Exception as e:
        logger.error(f"❌ 音声ファイルクリーンアップエラー: {e}")

# ==============================================================================
# Flask エンドポイント
# ==============================================================================
@app.route('/health', methods=['GET'])
def health_check():
    """ヘルスチェック"""
    return create_json_response({
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'gemini': gemini_model is not None,
        'groq': groq_client is not None,
        'voicevox': global_state.voicevox_enabled
    })


@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    """メインチャットエンドポイント"""
    try:
        data = request.json
        if not data or 'uuid' not in data or 'message' not in data:
            return Response(
                "必須パラメータ不足|",
                mimetype='text/plain; charset=utf-8',
                status=400
            )

        user_uuid = sanitize_user_input(data['uuid'])
        user_name = sanitize_user_input(data.get('name', 'Guest'))
        message = sanitize_user_input(data['message'])
        generate_voice_flag = data.get('voice', False)

        if not chat_rate_limiter.is_allowed(user_uuid):
            return Response(
                "ちょっと待って！メッセージ送りすぎ～！|",
                mimetype='text/plain; charset=utf-8',
                status=429
            )

        # === 特殊コマンド: 残トークン ===
        if message.strip() == "残トークン":
            status_msg = "【AIエンジン状態】\n"
            status_msg += f"🦁 Gemini: {'稼働中' if gemini_model else '停止中'}\n"
            status_msg += groq_model_manager.get_status_report()
            
            available = groq_model_manager.get_available_models()
            if not available and not gemini_model:
                status_msg += "\n\n⚠️ 全エンジン停止中…ちょっと休憩させて…"
            
            return Response(
                f"{status_msg}|",
                mimetype='text/plain; charset=utf-8',
                status=200
            )

        # === 通常会話処理 ===
        ai_text = ""
        is_task_started = False

        with get_db_session() as session:
            user_data = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)

            # ユーザーメッセージを保存
            session.add(ConversationHistory(
                user_uuid=user_uuid,
                role='user',
                content=message
            ))

            # DB修正リクエストチェック
            correction = detect_db_correction_request(message)
            if correction:
                task_id = f"db_fix_{user_uuid}_{int(time.time())}"
                task = BackgroundTask(
                    task_id=task_id,
                    user_uuid=user_uuid,
                    task_type='db_correction',
                    query=json.dumps(correction, ensure_ascii=False)
                )
                session.add(task)
                background_executor.submit(background_db_correction, task_id, correction)
                ai_text = f"まじ！？「{correction['member_name']}」の情報、直しとくね！"
                is_task_started = True

            # 時刻リクエスト
            if not ai_text and is_time_request(message):
                ai_text = get_japan_time()

            # 天気リクエスト
            if not ai_text and is_weather_request(message):
                ai_text = get_weather_forecast(extract_location(message))

            # 検索リクエスト
            if not ai_text and is_explicit_search_request(message):
                task_id = f"search_{user_uuid}_{int(time.time())}"
                query_data = {
                    'query': message,
                    'user_data': {
                        'uuid': user_data.uuid,
                        'name': user_data.name,
                        'interaction_count': user_data.interaction_count
                    }
                }
                task = BackgroundTask(
                    task_id=task_id,
                    user_uuid=user_uuid,
                    task_type='search',
                    query=json.dumps(query_data, ensure_ascii=False)
                )
                session.add(task)
                background_executor.submit(background_deep_search, task_id, query_data)
                ai_text = "オッケー！ちょっとググってくるから待ってて！"
                is_task_started = True

            # 通常AI応答
            if not ai_text:
                ai_text = generate_ai_response_safe(user_data, message, history)

            # AI応答を保存（タスク開始時以外）
            if not is_task_started:
                session.add(ConversationHistory(
                    user_uuid=user_uuid,
                    role='assistant',
                    content=ai_text
                ))

            # 定期的な心理分析（100回ごと）
            if user_data.interaction_count % 100 == 0:
                background_executor.submit(analyze_user_psychology, user_uuid)

        # 応答準備
        response_text = limit_text_for_sl(ai_text)
        voice_url = ""

        if generate_voice_flag and global_state.voicevox_enabled and not is_task_started:
            voice_filename = generate_voice_file(response_text, user_uuid)
            if voice_filename:
                voice_url = f"{SERVER_URL}/play/{voice_filename}"

        return Response(
            f"{response_text}|{voice_url}",
            mimetype='text/plain; charset=utf-8',
            status=200
        )

    except DatabaseException as e:
        logger.error(f"DBエラー: {e}")
        return Response(
            "データベースエラーが起きちゃった…|",
            mimetype='text/plain; charset=utf-8',
            status=500
        )
    except Exception as e:
        logger.critical(f"🔥 致命的エラー: {e}", exc_info=True)
        return Response(
            "ごめん、システムエラー…|",
            mimetype='text/plain; charset=utf-8',
            status=500
        )


@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
    """バックグラウンドタスクの完了確認"""
    try:
        data = request.json
        if not data or 'uuid' not in data:
            return create_json_response({'status': 'error', 'message': 'uuid required'}, 400)

        user_uuid = sanitize_user_input(data['uuid'])

        with get_db_session() as session:
            task = (
                session.query(BackgroundTask)
                .filter(
                    BackgroundTask.user_uuid == user_uuid,
                    BackgroundTask.status == 'completed'
                )
                .order_by(BackgroundTask.completed_at.desc())
                .first()
            )

            if task:
                result = task.result or ""
                session.delete(task)
                
                # 結果を会話履歴に保存
                session.add(ConversationHistory(
                    user_uuid=user_uuid,
                    role='assistant',
                    content=result
                ))

                return create_json_response({
                    'status': 'completed',
                    'response': f"{limit_text_for_sl(result)}|"
                })

        return create_json_response({'status': 'no_tasks'})

    except DatabaseException as e:
        logger.error(f"タスク確認DBエラー: {e}")
        return create_json_response({'status': 'error', 'message': 'Database error'}, 500)
    except Exception as e:
        logger.error(f"タスク確認エラー: {e}", exc_info=True)
        return create_json_response({'status': 'error'}, 500)


@app.route('/play/<filename>', methods=['GET'])
def play_voice(filename: str):
    """音声ファイル再生"""
    # ファイル名のバリデーション（パストラバーサル対策）
    if not re.match(r'^voice_[a-zA-Z0-9_]+\.wav
            , filename):
        return Response("Invalid filename", status=400)
    
    filepath = os.path.join(VOICE_DIR, filename)
    if not os.path.exists(filepath):
        return Response("File not found", status=404)
    
    return send_from_directory(VOICE_DIR, filename)


@app.route('/holomem/<member_name>', methods=['GET'])
def get_holomem_info(member_name: str):
    """ホロメン情報取得API"""
    try:
        member_name = sanitize_user_input(member_name, max_length=50)
        info = get_holomem_info_cached(member_name)
        
        if info:
            return create_json_response({'status': 'success', 'data': info})
        else:
            return create_json_response({'status': 'not_found'}, 404)
    except Exception as e:
        logger.error(f"ホロメン情報取得エラー: {e}")
        return create_json_response({'status': 'error'}, 500)


@app.route('/stats', methods=['GET'])
def get_stats():
    """統計情報取得"""
    try:
        with get_db_session() as session:
            user_count = session.query(UserMemory).count()
            message_count = session.query(ConversationHistory).count()
            task_pending = session.query(BackgroundTask).filter_by(status='pending').count()

        return create_json_response({
            'status': 'ok',
            'users': user_count,
            'messages': message_count,
            'pending_tasks': task_pending,
            'gemini_active': gemini_model is not None,
            'groq_active': groq_client is not None,
            'groq_available_models': len(groq_model_manager.get_available_models()),
            'voicevox_active': global_state.voicevox_enabled
        })
    except Exception as e:
        logger.error(f"統計取得エラー: {e}")
        return create_json_response({'status': 'error'}, 500)

# ==============================================================================
# 定期タスク
# ==============================================================================
def run_scheduled_tasks():
    """定期タスクを実行するスレッド"""
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)
        except Exception as e:
            logger.error(f"定期タスクエラー: {e}")
            time.sleep(60)


def setup_scheduled_tasks():
    """定期タスクの設定"""
    # 1時間ごとに古い音声ファイルを削除
    schedule.every(1).hours.do(cleanup_old_voice_files)
    
    # 6時間ごとにレート制限クリーンアップ
    schedule.every(6).hours.do(chat_rate_limiter.cleanup_old_entries)
    
    # 定期タスクスレッドを開始
    task_thread = threading.Thread(target=run_scheduled_tasks, daemon=True)
    task_thread.start()
    logger.info("📅 定期タスクスレッド開始")

# ==============================================================================
# 初期化 & クリーンアップ
# ==============================================================================
def initialize_database():
    """データベース初期化"""
    global engine, Session
    
    try:
        if DATABASE_URL.startswith('sqlite'):
            engine = create_engine(
                DATABASE_URL,
                pool_pre_ping=True,
                connect_args={'check_same_thread': False}
            )
        else:
            engine = create_engine(
                DATABASE_URL,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=10,
                pool_recycle=3600
            )
        
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        logger.info("✅ データベース初期化完了")
        return True
    except Exception as e:
        logger.critical(f"🔥 データベース初期化失敗: {e}", exc_info=True)
        return False


def initialize_ai_clients():
    """AIクライアント初期化"""
    global groq_client, gemini_model
    
    # Groq初期化
    if GROQ_API_KEY:
        try:
            groq_client = Groq(api_key=GROQ_API_KEY)
            logger.info("✅ Groqクライアント初期化完了")
        except Exception as e:
            logger.error(f"❌ Groq初期化失敗: {e}")
            groq_client = None
    else:
        logger.warning("⚠️ GROQ_API_KEYが設定されていません")

    # Gemini初期化
    if GEMINI_API_KEY:
        try:
            genai.configure(api_key=GEMINI_API_KEY)
            gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')
            logger.info("✅ Geminiモデル初期化完了")
        except Exception as e:
            logger.error(f"❌ Gemini初期化失敗: {e}")
            gemini_model = None
    else:
        logger.warning("⚠️ GEMINI_API_KEYが設定されていません")


def initialize_voicevox():
    """VOICEVOX初期化"""
    url = find_active_voicevox_url()
    if url:
        global_state.voicevox_enabled = True
        logger.info(f"✅ VOICEVOX有効化: {url}")
    else:
        logger.info("ℹ️ VOICEVOXは利用不可")


def initialize_app():
    """アプリケーション全体の初期化"""
    logger.info("=" * 60)
    logger.info("🔧 もちこAI v31.0 初期化開始")
    logger.info("=" * 60)

    # 1. データベース
    if not initialize_database():
        logger.critical("🔥 データベース初期化失敗 - 起動を中止します")
        return False

    # 2. AIクライアント
    initialize_ai_clients()

    # 3. VOICEVOX
    initialize_voicevox()

    # 4. 定期タスク
    setup_scheduled_tasks()

    # 5. 起動時クリーンアップ
    cleanup_old_voice_files()

    # 状態サマリー
    logger.info("=" * 60)
    logger.info("📊 初期化完了サマリー:")
    logger.info(f"  - Database: ✅")
    logger.info(f"  - Gemini: {'✅' if gemini_model else '❌'}")
    logger.info(f"  - Groq: {'✅' if groq_client else '❌'}")
    logger.info(f"  - VOICEVOX: {'✅' if global_state.voicevox_enabled else '❌'}")
    logger.info("=" * 60)

    return True


def cleanup_on_exit():
    """終了時クリーンアップ"""
    logger.info("🛑 シャットダウン処理開始...")
    
    try:
        background_executor.shutdown(wait=False)
        logger.info("  - バックグラウンドエグゼキュータ停止")
    except Exception as e:
        logger.error(f"エグゼキュータ停止エラー: {e}")

    try:
        if engine:
            engine.dispose()
            logger.info("  - データベース接続クローズ")
    except Exception as e:
        logger.error(f"DB接続クローズエラー: {e}")

    logger.info("✅ シャットダウン完了")


# 終了時ハンドラ登録
atexit.register(cleanup_on_exit)

# アプリケーション初期化実行
initialize_app()

# ==============================================================================
# エントリーポイント
# ==============================================================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('DEBUG', 'false').lower() == 'true'
    
    logger.info(f"🚀 サーバー起動: port={port}, debug={debug_mode}")
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
            
