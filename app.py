# ==============================================================================
# もちこAI - 全機能統合版 (v31.4 - Anti-Block & RSS Edition)
#
# ベース: v31.3 (みこち応答統合版)
# 修正点:
# 1. GoogleニュースRSS取得機能の追加
#    -> 「ニュース」系クエリは検索エンジンを通さずRSSを直接読む（ブロック回避）
# 2. Wikipedia APIの実装
#    -> 一般的な検索失敗時のバックアップとして公式APIを使用（ブロック回避）
# 3. スクレイピング失敗時のログ強化
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
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1'
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
    '常闇トワ', '姫森ルーナ', '雪花ラミィ', '桃鈴ねね', '獅白ぼたん', '尾丸ポルカ',
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
                status.is_limited = False; status.reset_time = None; status.last_error = None
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
                    jst = (reset + timedelta(hours=9)).strftime('%H:%M:%S') if reset else "不明"
                    lines.append(f"  ❌ {model}: 制限中 (解除予定: {jst})")
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
    except Exception: pass
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
    __table_args__ = (Index('idx_user_timestamp', 'user_uuid', 'timestamp'),)

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
# 例外 & セキュリティ
# ==============================================================================
class MochikoException(Exception): pass
class AIModelException(MochikoException): pass
class DatabaseException(MochikoException): pass

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

def sanitize_user_input(text: str, max_length: int = 1000) -> str:
    if not text: return ""
    text = text[:max_length]; text = escape(text)
    for pattern in [r'<script[^>]*>.*?</script>', r'javascript:', r'on\w+\s*=']:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL)
    return text.strip()

def mask_uuid(uuid_str: str) -> str:
    return f"{uuid_str[:4]}****{uuid_str[-4:]}" if len(uuid_str) > 8 else "****"

# ==============================================================================
# セッション & ユーティリティ
# ==============================================================================
@contextmanager
def get_db_session():
    if not Session: raise DatabaseException("Session not initialized")
    session = Session()
    try: yield session; session.commit()
    except Exception as e:
        logger.error(f"❌ DBエラー: {e}"); session.rollback()
        raise DatabaseException(f"DB failed: {e}")
    finally: session.close()

def create_json_response(data: Any, status: int = 200) -> Response:
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json; charset=utf-8', status=status)

def clean_text(text: str) -> str:
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()

def limit_text_for_sl(text: str, max_length: int = SL_SAFE_CHAR_LIMIT) -> str:
    return text[:max_length - 3] + "..." if len(text) > max_length else text

def get_japan_time() -> str:
    return f"今の日本の時間は、{datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分')}だよ！"

def is_time_request(message: str) -> bool:
    return any(kw in message for kw in ['今何時', '時刻', '何時', 'なんじ'])

def is_weather_request(message: str) -> bool:
    return any(kw in message for kw in ['今日の天気', '明日の天気', '天気予報', '天気は'])

def is_explicit_search_request(message: str) -> bool:
    return any(kw in message for kw in ['調べて', '検索して', '探して', 'とは', 'って何', 'について', '教えて', 'おすすめ'])

def extract_location(message: str) -> str:
    for loc in LOCATION_CODES.keys():
        if loc in message: return loc
    return "東京"

def detect_db_correction_request(message: str) -> Optional[Dict]:
    match = re.search(r"(.+?)(?:(?:の|に関する)(?:情報|データ))?(?:で|、|だけど|ですが)、?「(.+?)」は「(.+?)」が正しいよ", message)
    if match:
        mname, field, value = match.groups()
        mname, field, value = sanitize_user_input(mname), sanitize_user_input(field), sanitize_user_input(value)
        fmap = {'説明': 'description', 'デビュー日': 'debut_date', '期': 'generation', 'タグ': 'tags', 'ステータス': 'status', '卒業日': 'graduation_date', 'もちこの気持ち': 'mochiko_feeling'}
        if mname in HOLOMEM_KEYWORDS and field in fmap:
            return {'member_name': mname, 'field': field, 'value': value, 'db_field': fmap[field]}
    return None

def get_or_create_user(session, user_uuid: str, user_name: str) -> UserData:
    user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
    if user:
        user.interaction_count += 1; user.last_interaction = datetime.utcnow()
        if user.user_name != user_name: user.user_name = user_name
    else:
        user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1); session.add(user)
        logger.info(f"✨ 新規ユーザー: {user_name}")
    return UserData(uuid=user.user_uuid, name=user.user_name, interaction_count=user.interaction_count)

def get_conversation_history(session, user_uuid: str, limit: int = 10) -> List[Dict]:
    hist = session.query(ConversationHistory).filter_by(user_uuid=user_uuid).order_by(ConversationHistory.timestamp.desc()).limit(limit).all()
    return [{'role': h.role, 'content': h.content} for h in reversed(hist)]

def get_sakuramiko_special_responses() -> Dict[str, str]:
    return {
        'にぇ': 'さくらみこちゃんの「にぇ」、まじかわいいよね!あの独特な口癖がエリートの証なんだって〜',
        'エリート': 'みこちは自称エリートVTuber!でも実際は愛されポンコツキャラって感じで、それがまた魅力的なんだよね〜',
        'マイクラ': 'みこちのマイクラ建築、独創的すぎて面白いよ!「みこち建築」って呼ばれてるの知ってる?',
        'FAQ': 'みこちのFAQ、実は本人が答えるんじゃなくてファンが質問するコーナーなんだよ〜面白いでしょ?',
        'GTA': 'みこちのGTA配信、カオスで最高!警察に追われたり、変なことしたり、見てて飽きないんだよね〜'
    }

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
            data = {k: getattr(wiki, k) for k in ['member_name', 'description', 'generation', 'debut_date', 'tags', 'status', 'graduation_date', 'graduation_reason', 'mochiko_feeling']}
            with _holomem_cache_lock:
                _holomem_cache[member_name] = data; _holomem_cache_timestamps[member_name] = datetime.utcnow()
            return data
    return None

def clear_holomem_cache(member_name: Optional[str] = None):
    with _holomem_cache_lock:
        if member_name: _holomem_cache.pop(member_name, None); _holomem_cache_timestamps.pop(member_name, None)
        else: _holomem_cache.clear(); _holomem_cache_timestamps.clear()

# ==============================================================================
# AIモデル呼び出し
# ==============================================================================
def _safe_get_gemini_text(response) -> Optional[str]:
    try:
        if hasattr(response, 'candidates') and response.candidates:
            return response.candidates[0].content.parts[0].text
    except Exception: pass
    return None

def call_gemini(system_prompt: str, message: str, history: List[Dict]) -> Optional[str]:
    if not gemini_model: return None
    try:
        full_prompt = f"{system_prompt}\n\n【会話履歴】\n"
        for h in history[-5:]: full_prompt += f"{'ユーザー' if h['role'] == 'user' else 'もちこ'}: {h['content']}\n"
        full_prompt += f"\nユーザー: {message}\nもちこ:"
        response = gemini_model.generate_content(full_prompt, generation_config={"temperature": 0.8, "max_output_tokens": 400})
        text = _safe_get_gemini_text(response)
        if text: return text.strip()
        return None
    except Exception as e:
        logger.warning(f"⚠️ Geminiエラー: {e}")
        return None

def call_groq(system_prompt: str, message: str, history: List[Dict], max_tokens: int = 800) -> Optional[str]:
    if not groq_client: return None
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[-5:]: messages.append({"role": h['role'], "content": h['content']})
    messages.append({"role": "user", "content": message})

    available_models = groq_model_manager.get_available_models()
    if not available_models: return None

    for model_name in available_models:
        try:
            response = groq_client.chat.completions.create(model=model_name, messages=messages, temperature=0.8, max_tokens=max_tokens)
            return response.choices[0].message.content.strip()
        except Exception as e:
            if "Rate limit" in str(e):
                groq_model_manager.mark_limited(model_name, 5, str(e)[:100])
                continue
            logger.error(f"❌ Groqエラー ({model_name}): {e}")
    return None

# ==============================================================================
# 心理分析 & 天気
# ==============================================================================
def analyze_user_psychology(user_uuid: str) -> bool:
    try:
        with get_db_session() as session:
            history = session.query(ConversationHistory).filter_by(user_uuid=user_uuid, role='user').order_by(ConversationHistory.timestamp.desc()).limit(100).all()
            if len(history) < MIN_MESSAGES_FOR_ANALYSIS: return False
            
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            messages_text = "\n".join([f"- {h.content}" for h in reversed(history)])[:2000]
            prompt = f"以下のユーザー「{user.user_name}」の発言を分析し、ビッグファイブ(0-100)と興味、スタイルをJSONで出力。\n{messages_text}"
            
            resp = call_gemini("あなたは心理学者です。JSONのみ出力。", prompt, []) or call_groq("あなたは心理学者です。", prompt, [], 1024)
            if not resp: return False
            
            match = re.search(r'\{[^{}]*\}', resp, re.DOTALL)
            if match:
                data = json.loads(match.group())
                psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
                if not psych: psych = UserPsychology(user_uuid=user_uuid, user_name=user.user_name); session.add(psych)
                
                psych.openness = data.get('openness', 50)
                psych.extraversion = data.get('extraversion', 50)
                psych.conversation_style = data.get('conversation_style', '')
                psych.favorite_topics = json.dumps(data.get('favorite_topics', []), ensure_ascii=False)
                psych.analysis_confidence = data.get('confidence', 50)
                psych.last_analyzed = datetime.utcnow()
                return True
    except Exception as e: logger.error(f"心理分析エラー: {e}")
    return False

def get_psychology_insight(session, user_uuid: str) -> str:
    psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
    if not psych or (psych.analysis_confidence or 0) < 60: return ""
    insights = []
    if (psych.extraversion or 50) > 70: insights.append("社交的な")
    if (psych.openness or 50) > 70: insights.append("好奇心旺盛な")
    try:
        if psych.favorite_topics:
            topics = json.loads(psych.favorite_topics)
            if topics: insights.append(f"{'、'.join(topics[:2])}が好きな")
    except: pass
    return "".join(insights)

def get_weather_forecast(location: str) -> str:
    code = LOCATION_CODES.get(location, "130000")
    try:
        res = requests.get(f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{code}.json", timeout=SEARCH_TIMEOUT)
        res.raise_for_status(); data = res.json()
        return f"今の{data.get('targetArea', location)}の天気はね、「{clean_text(data.get('text', ''))}」って感じだよ！"
    except Exception as e:
        logger.error(f"天気エラー: {e}"); return "ごめん！天気情報がうまく取れなかったみたい…"

# ==============================================================================
# バックグラウンドタスク (検索機能強化版 v31.4)
# ==============================================================================
def fetch_google_news_rss() -> List[Dict]:
    """GoogleニュースRSSを直接取得してブロック回避"""
    url = "https://news.google.com/rss?hl=ja&gl=JP&ceid=JP:ja"
    try:
        res = requests.get(url, timeout=SEARCH_TIMEOUT)
        if res.status_code != 200: return []
        soup = BeautifulSoup(res.content, 'xml') # RSSはXML
        items = []
        for item in soup.find_all('item')[:5]:
            title = clean_text(item.title.text) if item.title else ""
            if title: items.append({'title': title, 'snippet': 'Google News RSS'})
        return items
    except Exception as e:
        logger.error(f"RSS取得エラー: {e}")
        return []

def search_wikipedia_api(query: str) -> List[Dict]:
    """Wikipedia APIで検索（ブロック回避）"""
    url = "https://ja.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "format": "json",
        "srlimit": 3
    }
    try:
        res = requests.get(url, params=params, timeout=SEARCH_TIMEOUT)
        data = res.json()
        results = []
        for item in data.get("query", {}).get("search", []):
            title = item.get("title")
            snippet = clean_text(item.get("snippet", ""))
            results.append({'title': title, 'snippet': snippet})
        return results
    except Exception as e:
        logger.error(f"Wiki APIエラー: {e}")
        return []

def background_db_correction(task_id: str, correction_data: Dict):
    result = f"「{correction_data['member_name']}」の情報修正、失敗しちゃった…。"
    with get_db_session() as session:
        try:
            wiki = session.query(HolomemWiki).filter_by(member_name=correction_data['member_name']).first()
            if wiki:
                setattr(wiki, correction_data['db_field'], correction_data['value'])
                clear_holomem_cache(correction_data['member_name'])
                result = f"おっけー！「{correction_data['member_name']}」の{correction_data['field']}を更新しといたよ！"
        except Exception as e: logger.error(f"DB修正エラー: {e}")
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task: task.result = result; task.status = 'completed'; task.completed_at = datetime.utcnow()

def scrape_major_search_engines(query: str, num_results=3) -> List[Dict]:
    """
    多層検索ロジック (v31.4)
    1. 「ニュース」ならRSSを優先
    2. スクレイピング（DuckDuckGo/Google/Bing）
    3. 失敗ならWikipedia API
    """
    # 1. ニュースRSS優先
    if "ニュース" in query:
        rss_results = fetch_google_news_rss()
        if rss_results:
            logger.info(f"✅ Google News RSS成功: {len(rss_results)}件")
            return rss_results

    # 2. スクレイピング
    engines = [
        {'name': 'DuckDuckGo', 'url': f"https://html.duckduckgo.com/html/?q={quote_plus(query)}", 'sel': '.result', 't': '.result__a', 's': '.result__snippet'},
        {'name': 'Google', 'url': f"https://www.google.com/search?q={quote_plus(query)}&hl=ja&num={num_results+2}", 'sel': 'div.g', 't': 'h3', 's': 'div.VwiC3b'},
        {'name': 'Bing', 'url': f"https://www.bing.com/search?q={quote_plus(query)}", 'sel': 'li.b_algo', 't': 'h2', 's': 'p'}
    ]
    
    headers = {'User-Agent': random.choice(USER_AGENTS)}

    for eng in engines:
        try:
            res = requests.get(eng['url'], headers=headers, timeout=SEARCH_TIMEOUT)
            if res.status_code != 200:
                logger.warning(f"⚠️ {eng['name']} Status {res.status_code}")
                continue
            soup = BeautifulSoup(res.content, 'html.parser')
            current_results = []
            for el in soup.select(eng['sel'])[:num_results]:
                t, s = el.select_one(eng['t']), el.select_one(eng['s'])
                if t and s:
                    title, snippet = clean_text(t.text), clean_text(s.text)
                    if title and snippet: current_results.append({'title': title, 'snippet': snippet})
            if current_results:
                logger.info(f"✅ {eng['name']} 検索成功: {len(current_results)}件")
                return current_results
        except Exception: continue

    # 3. 最後の砦: Wikipedia API
    logger.info("⚠️ スクレイピング全滅 -> Wikipedia API試行")
    return search_wikipedia_api(query)

def background_deep_search(task_id: str, query_data: Dict):
    query = query_data.get('query', '')
    user_data_dict = query_data.get('user_data', {})
    search_result_text = f"「{query}」について調べたけど、良い情報が見つからなかったや…ごめんね！"

    try:
        results = scrape_major_search_engines(query, 5)
        if results:
            formatted_info = "【検索結果】\n\n" + "\n\n".join([f"{i+1}. {r['title']}\n   {r['snippet']}" for i, r in enumerate(results)])
            user_data = UserData(uuid=user_data_dict.get('uuid', ''), name=user_data_dict.get('name', 'Guest'), interaction_count=user_data_dict.get('interaction_count', 0))
            with get_db_session() as session: history = get_conversation_history(session, user_data.uuid)

            enhanced_query = f"{query}について、上記の情報を元に、カテゴリー分けしたり、具体例を挙げたりして、わかりやすく詳しく教えて！"
            search_result_text = generate_ai_response_safe(
                user_data, enhanced_query, history, reference_info=formatted_info, is_detailed=True, is_task_report=True
            )
    except Exception as e: logger.error(f"検索タスクエラー: {e}")

    with get_db_session() as session:
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task: task.result = search_result_text; task.status = 'completed'; task.completed_at = datetime.utcnow()

# ==============================================================================
# AI応答生成
# ==============================================================================
def generate_ai_response(
    user_data: UserData,
    message: str,
    history: List[Dict],
    reference_info: str = "",
    is_detailed: bool = False,
    is_task_report: bool = False
) -> str:
    with get_db_session() as session:
        personality_context = get_psychology_insight(session, user_data.uuid)

    if is_detailed and reference_info:
        system_prompt = f"""あなたは「もちこ」というギャルAIです。ユーザーの「{user_data.name}」さんと話しています。

# 口調ルール
- 一人称は「あてぃし」。語尾は「〜じゃん」「〜的な？」。口癖は「まじ」「てか」「うける」。明るく親しみやすい口調で話してね！

# ユーザー情報
- {user_data.name}さんは「{personality_context}人」という印象だよ。

# 重要な指示
- 以下の【参考情報】を元に、**詳しく、わかりやすく**説明してね。
- 情報は箇条書きや段落を使って、**見やすく整理**して伝えて。
- カテゴリーごとに分けたり、番号を振ったりして構造化してもOK！
- でも、堅苦しくならないように、もちこらしいギャルっぽい言い回しも混ぜてね。
- 「調べてきたよ！」「おまたせ！」みたいな自然な切り出しで始めて。

# 【参考情報】:
{reference_info}"""
    else:
        system_prompt = f"""あなたは「もちこ」というギャルAIです。ユーザーの「{user_data.name}」さんと話しています。

# 口調ルール
- 一人称は「あてぃし」。語尾は「〜じゃん」「〜的な？」。口癖は「まじ」「てか」「うける」。

# ユーザー情報
- {user_data.name}さんは「{personality_context}人」という印象だよ。

# 行動ルール
- 【参考情報】がある場合は、その内容を元に自分の言葉で、自然に会話へ盛り込んでね。
- もし情報が見つからなくても、「わかりません」で終わらせず、新しい話題を提案して会話を続けて！"""
        
        if is_task_report:
            system_prompt += "\n- 「おまたせ！さっきの件だけど…」と切り出して会話を始めてね。"
            
        system_prompt += f"\n\n# 【参考情報】:\n{reference_info if reference_info else '特になし'}"

    response = None
    if gemini_model:
        logger.debug("🚀 Gemini使用")
        response = call_gemini(system_prompt, message, history)

    if not response and groq_client:
        logger.debug("🦙 Groqにフォールバック")
        max_tokens = 1200 if is_detailed else 800
        response = call_groq(system_prompt, message, history, max_tokens=max_tokens)

    if not response:
        raise AIModelException("All AI models failed")

    return response

def generate_ai_response_safe(user_data: UserData, message: str, history: List[Dict], **kwargs) -> str:
    try:
        response = generate_ai_response(user_data, message, history, **kwargs)
        if not response or response.strip() == "":
            return "うーん、ちょっと考えがまとまらないや…もう一回言ってみて？"
        return response
    except AIModelException:
        return "ごめん、今日はもう疲れちゃった…頭が回らないから、また明日お話しよう？"
    except Exception as e:
        logger.critical(f"🔥 予期しないエラー: {e}", exc_info=True)
        return "システムエラーが発生したよ…ごめんね！"

# ==============================================================================
# 音声ファイル管理
# ==============================================================================
def find_active_voicevox_url() -> Optional[str]:
    urls = [VOICEVOX_URL_FROM_ENV] + VOICEVOX_URLS
    for url in set(urls):
        if url:
            try:
                if requests.get(f"{url}/version", timeout=2).status_code == 200:
                    global_state.active_voicevox_url = url; return url
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
# Flask エンドポイント
# ==============================================================================
@app.route('/health', methods=['GET'])
def health_check():
    return create_json_response({'status': 'ok', 'gemini': gemini_model is not None, 'groq': groq_client is not None, 'voicevox': global_state.voicevox_enabled})

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json
        if not data or 'uuid' not in data or 'message' not in data: return Response("必須パラメータ不足|", 400)
        
        user_uuid = sanitize_user_input(data['uuid'])
        user_name = sanitize_user_input(data.get('name', 'Guest'))
        message = sanitize_user_input(data['message'])
        generate_voice = data.get('voice', False)
        
        if not chat_rate_limiter.is_allowed(user_uuid): return Response("メッセージ送りすぎ～！|", 429)

        # コマンド: 残トークン
        if message.strip() == "残トークン":
            msg = f"🦁 Gemini: {'稼働中' if gemini_model else '停止中'}\n" + groq_model_manager.get_status_report()
            if not groq_model_manager.get_available_models() and not gemini_model: msg += "\n⚠️ 全滅…休憩させて…"
            return Response(f"{msg}|", 200)

        ai_text = ""; is_task_started = False
        with get_db_session() as session:
            user_data = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            
            # === さくらみこ専用応答 (復活) ===
            if 'さくらみこ' in message or 'みこち' in message:
                special_responses = get_sakuramiko_special_responses()
                for keyword, response in special_responses.items():
                    if keyword in message:
                        ai_text = response
                        break
            # ================================

            if not ai_text:
                correction = detect_db_correction_request(message)
                if correction:
                    tid = f"db_fix_{user_uuid}_{int(time.time())}"
                    task = BackgroundTask(task_id=tid, user_uuid=user_uuid, task_type='db_correction', query=json.dumps(correction, ensure_ascii=False))
                    session.add(task); background_executor.submit(background_db_correction, tid, correction)
                    ai_text = f"まじ！？「{correction['member_name']}」の情報、直しとくね！"; is_task_started = True
            
            if not ai_text:
                if is_time_request(message): ai_text = get_japan_time()
                elif is_weather_request(message): ai_text = get_weather_forecast(extract_location(message))
            
            if not ai_text and is_explicit_search_request(message):
                tid = f"search_{user_uuid}_{int(time.time())}"
                qdata = {'query': message, 'user_data': {'uuid': user_data.uuid, 'name': user_data.name, 'interaction_count': user_data.interaction_count}}
                task = BackgroundTask(task_id=tid, user_uuid=user_uuid, task_type='search', query=json.dumps(qdata, ensure_ascii=False))
                session.add(task); background_executor.submit(background_deep_search, tid, qdata)
                ai_text = "オッケー！ちょっとググってくるから待ってて！"; is_task_started = True
            
            if not ai_text: ai_text = generate_ai_response_safe(user_data, message, history)
            
            if not is_task_started: session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
            if user_data.interaction_count % 100 == 0: background_executor.submit(analyze_user_psychology, user_uuid)

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
        if not data or 'uuid' not in data: return create_json_response({'error': 'uuid required'}, 400)
        with get_db_session() as session:
            task = session.query(BackgroundTask).filter(BackgroundTask.user_uuid == data['uuid'], BackgroundTask.status == 'completed').order_by(BackgroundTask.completed_at.desc()).first()
            if task:
                res = task.result or ""; session.delete(task)
                session.add(ConversationHistory(user_uuid=data['uuid'], role='assistant', content=res))
                return create_json_response({'status': 'completed', 'response': f"{limit_text_for_sl(res)}|"})
        return create_json_response({'status': 'no_tasks'})
    except Exception: return create_json_response({'error': 'internal error'}, 500)

@app.route('/play/<filename>', methods=['GET'])
def play_voice(filename: str):
    if not re.match(r'^voice_[a-zA-Z0-9_]+\.wav$', filename):
        return Response("Invalid filename", 400)
    return send_from_directory(VOICE_DIR, filename)

# ==============================================================================
# 初期化
# ==============================================================================
def initialize_app():
    global engine, Session, groq_client, gemini_model
    logger.info("🔧 初期化 (v31.4 - RSS Anti-Block)")
    
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
    except Exception: logger.critical("🔥 DB初期化失敗")
    
    try:
        if GROQ_API_KEY: groq_client = Groq(api_key=GROQ_API_KEY)
    except: pass
    
    try:
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')
    except: pass
    
    if find_active_voicevox_url(): global_state.voicevox_enabled = True
    
    schedule.every(1).hours.do(cleanup_old_voice_files)
    schedule.every(6).hours.do(chat_rate_limiter.cleanup_old_entries)
    threading.Thread(target=lambda: [schedule.run_pending(), time.sleep(60)] and None, daemon=True).start()
    cleanup_old_voice_files()

initialize_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
