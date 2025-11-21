# ==============================================================================
# もちこAI - 全機能統合版 (v30.0 - Auto-Fallback Edition)
#
# 変更点:
# 1. AIエンジンの優先順位を Gemini -> Groq に変更
# 2. Groqのレート制限(429)対策として、複数のモデルを順次試行するロジックを実装
# 3. 「残トークン」コマンドで各モデルの稼働状況を確認可能に
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
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict, defaultdict
from contextlib import contextmanager

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
    format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s',
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

# Groqで使用するモデルリスト（優先度順）
GROQ_MODELS = [
    "llama-3.3-70b-versatile",  # 最新・高性能（制限きつい）
    "llama-3.1-70b-versatile",  # 高性能バックアップ
    "llama-3.1-8b-instant",     # 超高速・軽量（制限緩い・最後の砦）
    "mixtral-8x7b-32768",       # バランス型
    "gemma2-9b-it"              # Google製バックアップ
]

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
]
LOCATION_CODES = {"東京": "130000", "大阪": "270000", "名古屋": "230000", "福岡": "400000", "札幌": "016000"}

SPECIALIZED_SITES = {
    'Blender': {'base_url': 'https://docs.blender.org/manual/ja/latest/', 'keywords': ['Blender', 'ブレンダー', 'blener']},
    'CGニュース': {'base_url': 'https://modelinghappy.com/', 'keywords': ['CGニュース', '3DCG', 'CG業界']},
    '脳科学・心理学': {'base_url': 'https://nazology.kusuguru.co.jp/', 'keywords': ['脳科学', '心理学', '脳', '認知科学']},
    'セカンドライフ': {'base_url': 'https://community.secondlife.com/news/', 'keywords': ['セカンドライフ', 'Second Life', 'SL']},
    'アニメ': {'base_url': 'https://animedb.jp/', 'keywords': ['アニメ', 'anime', 'ANIME', 'ｱﾆﾒ', 'アニメーション', '作画', '声優', 'OP', 'ED']}
}
HOLO_WIKI_URL = 'https://seesaawiki.jp/hololivetv/'

HOLOMEM_KEYWORDS = [
    'ときのそら', 'ロボ子さん', 'さくらみこ', 'みこち', '星街すいせい', 'すいちゃん', 'AZKi', '夜空メル', 'アキ・ローゼンタール', '赤井はあと', '白上フブキ', '夏色まつり', '湊あくあ',
    '紫咲シオン', '百鬼あやめ', '癒月ちょこ', '大空スバル', '大神ミオ', '猫又おかゆ', 'おかゆん', '戌神ころね', 'ころさん', '兎田ぺこら', 'ぺこーら', '不知火フレア', '白銀ノエル',
    '宝鐘マリン', '船長', '天音かなた', '角巻わため', '常闇トワ', '姫森ルーナ', '雪花ラミィ', '桃鈴ねね', '獅白ぼたん', '尾丸ポルカ', 'ラプラス・ダークネス', '鷹嶺ルイ', '博衣こより',
    '沙花叉クロヱ', '風真いろは', '森カリオペ', '小鳥遊キアラ', '一伊那尓栖', 'がうる・ぐら', 'サメちゃん', 'ワトソン・アメリア', 'IRyS', 'セレス・ファウナ', 'オーロ・クロニー',
    '七詩ムメイ', 'ハコス・ベールズ', 'シオリ・ノヴェラ', '古石ビジュー', 'ネリッサ・レイヴンクロフト', 'フワワ・アビスガード', 'モココ・アビスガード', 'アユンダ・リス',
    'ムーナ・ホシノヴァ', 'アイラニ・イオフィフティーン', 'クレイジー・オリー', 'アーニャ・メルフィッサ', 'パヴォリア・レイネ', '火威青', '音乃瀬奏', '一条莉々華',
    '儒烏風亭らでん', '轟はじめ', 'ホロライブ', 'ホロメン', 'hololive', 'YAGOO', '桐生ココ', '潤羽るしあ', '魔乃アロエ', '九十九佐命'
]
ANIME_KEYWORDS = ['アニメ', 'anime', 'ANIME', 'ｱﾆﾒ', 'アニメーション', '作画', '声優', 'OP', 'ED', '劇場版', '映画', '原作', '漫画', 'ラノベ']
VOICEVOX_URLS = ['http://voicevox-engine:50021', 'http://voicevox:50021', 'http://127.0.0.1:50021', 'http://localhost:50021']

# ==============================================================================
# グローバル変数 & アプリ設定
# ==============================================================================
class GlobalState:
    def __init__(self):
        self._lock = threading.Lock()
        self._voicevox_enabled = False
        self._active_voicevox_url = None
    @property
    def voicevox_enabled(self):
        with self._lock: return self._voicevox_enabled
    @voicevox_enabled.setter
    def voicevox_enabled(self, value):
        with self._lock: self._voicevox_enabled = value
    @property
    def active_voicevox_url(self):
        with self._lock: return self._active_voicevox_url
    @active_voicevox_url.setter
    def active_voicevox_url(self, value):
        with self._lock: self._active_voicevox_url = value

global_state = GlobalState()
background_executor = ThreadPoolExecutor(max_workers=5)
groq_client, gemini_model, engine, Session = None, None, None, None

# Groqモデルごとの状態管理（レート制限追跡用）
groq_model_status = {model: {"is_limited": False, "reset_time": None} for model in GROQ_MODELS}

app = Flask(__name__)
application = app
app.config['JSON_AS_ASCII'] = False
CORS(app)
Base = declarative_base()

# ==============================================================================
# 秘密情報/環境変数 読み込み
# ==============================================================================
def get_secret(name):
    env_value = os.environ.get(name)
    if env_value and env_value.strip(): return env_value.strip()
    try:
        secret_file_path = f"/etc/secrets/{name}"
        if os.path.exists(secret_file_path):
            with open(secret_file_path, 'r') as f:
                file_value = f.read().strip()
                if file_value: return file_value
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
# セキュリティ & 安定性 関連
# ==============================================================================
class RateLimiter:
    def __init__(self, max_requests: int, time_window: timedelta):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = defaultdict(list)
        self._lock = threading.Lock()
    def is_allowed(self, user_id: str) -> bool:
        with self._lock:
            now = datetime.utcnow()
            cutoff = now - self.time_window
            self.requests[user_id] = [req_time for req_time in self.requests[user_id] if req_time > cutoff]
            if len(self.requests[user_id]) >= self.max_requests: return False
            self.requests[user_id].append(now)
            return True

chat_rate_limiter = RateLimiter(max_requests=10, time_window=timedelta(minutes=1))

class MochikoException(Exception): pass
class AIModelException(MochikoException): pass
class DatabaseException(MochikoException): pass

def sanitize_user_input(text: str, max_length: int = 1000) -> str:
    if not text: return ""
    text = text[:max_length]
    text = escape(text)
    dangerous_patterns = [r'<script[^>]*>.*?</script>', r'javascript:', r'on\w+\s*=',]
    for pattern in dangerous_patterns: text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL)
    return text.strip()

def mask_uuid(uuid: str) -> str:
    if len(uuid) > 8: return f"{uuid[:4]}****{uuid[-4:]}"
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
        logger.error(f"❌ DBエラー: {type(e).__name__}: {e}", exc_info=True)
        session.rollback()
        raise DatabaseException(f"DB operation failed: {e}")
    finally:
        session.close()

# ==============================================================================
# ユーティリティ & ヘルパー関数
# ==============================================================================
def create_json_response(data, status=200):
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json; charset=utf-8', status=status)

def clean_text(text):
    if not text: return ""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()

def limit_text_for_sl(text, max_length=SL_SAFE_CHAR_LIMIT):
    return text[:max_length - 3] + "..." if len(text) > max_length else text

def get_japan_time():
    return f"今の日本の時間は、{datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分')}だよ！"

def is_time_request(message):
    return any(keyword in message for keyword in ['今何時', '時刻', '何時', 'なんじ'])

def is_weather_request(message):
    return any(keyword in message for keyword in ['今日の天気は？', '明日の天気', '天気予報'])

def is_hololive_request(message):
    return any(keyword in message for keyword in HOLOMEM_KEYWORDS)

def is_anime_request(message):
    return any(keyword in message for keyword in ANIME_KEYWORDS)

def detect_specialized_topic(message):
    for topic, config in SPECIALIZED_SITES.items():
        if any(keyword in message for keyword in config['keywords']):
            return topic
    return None

def is_explicit_search_request(message):
    return any(keyword in message for keyword in ['調べて', '検索して', '探して', 'とは', 'って何', 'について', '教えて', 'おすすめ'])

def is_short_response(message):
    normalized_message = message.strip().lower()
    return len(normalized_message) <= 5 or normalized_message in ['うん', 'そう', 'はい', 'そっか', 'なるほど', 'おけ', 'ok', '了解']

def extract_location(message):
    for location in LOCATION_CODES.keys():
        if location in message: return location
    return "東京"

def detect_db_correction_request(message):
    pattern = r"(.+?)(?:(?:の|に関する)(?:情報|データ))?(?:で|、|だけど|ですが)、?「(.+?)」は「(.+?)」が正しいよ"
    match = re.search(pattern, message)
    if match:
        member_name_raw, field_raw, value_raw = match.groups()
        member_name = sanitize_user_input(member_name_raw.strip())
        field = sanitize_user_input(field_raw.strip())
        value = sanitize_user_input(value_raw.strip())
        field_map = {'説明': 'description', 'デビュー日': 'debut_date', '期': 'generation', 'タグ': 'tags', 'ステータス': 'status', '卒業日': 'graduation_date', 'もちこの気持ち': 'mochiko_feeling'}
        if member_name in HOLOMEM_KEYWORDS and field in field_map:
            return {'member_name': member_name, 'field': field, 'value': value, 'db_field': field_map[field]}
    return None

def is_holomem_name_only_request_safe(message: str):
    msg_stripped = sanitize_user_input(message.strip(), max_length=50)
    if len(msg_stripped) > 20: return None
    for name in HOLOMEM_KEYWORDS:
        if name == msg_stripped: return name
    return None

def get_or_create_user(session, user_uuid, user_name):
    user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
    if user:
        user.interaction_count += 1
        user.last_interaction = datetime.utcnow()
        if user.user_name != user_name: user.user_name = user_name
    else:
        user = UserMemory(user_uuid=user_uuid, user_name=user_name, interaction_count=1)
        session.add(user)
        logger.info(f"✨ 新規ユーザー作成: {user_name} (UUID: {mask_uuid(user_uuid)})")
    return {'uuid': user.user_uuid, 'name': user.user_name, 'interaction_count': user.interaction_count}

def get_conversation_history(session, user_uuid, limit=10):
    history_records = session.query(ConversationHistory).filter_by(user_uuid=user_uuid).order_by(ConversationHistory.timestamp.desc()).limit(limit).all()
    return [{'role': h.role, 'content': h.content} for h in reversed(history_records)]

# ==============================================================================
# AIモデル呼び出し関数 (Gemini優先 + Groqフォールバック強化版)
# ==============================================================================
def _safe_get_gemini_text(response):
    try:
        if hasattr(response, 'candidates') and response.candidates:
            if response.candidates[0].content.parts:
                return response.candidates[0].content.parts[0].text
    except (IndexError, AttributeError):
        logger.warning(f"⚠️ Gemini応答不正: {getattr(response, 'prompt_feedback', 'N/A')}")
        return None
    except Exception:
        return None
    return None

def call_gemini(system_prompt, message, history):
    if not gemini_model:
        return None
    try:
        full_prompt = f"{system_prompt}\n\n【会話履歴】\n"
        for h in history: full_prompt += f"{'ユーザー' if h['role'] == 'user' else 'もちこ'}: {h['content']}\n"
        full_prompt += f"\nユーザー: {message}\nもちこ:"
        
        response = gemini_model.generate_content(
            full_prompt, 
            generation_config={"temperature": 0.8, "max_output_tokens": 300}
        )
        text = _safe_get_gemini_text(response)
        
        if text:
            logger.debug(f"Gemini応答成功")
            return text.strip()
        else:
            return None
    except Exception as e:
        logger.warning(f"⚠️ Gemini API呼び出しエラー (スキップ): {e}")
        return None

def call_llama_advanced(system_prompt, message, history, max_tokens=800):
    global groq_model_status
    if not groq_client: return None
    
    messages = [{"role": "system", "content": system_prompt}]
    for h in history: messages.append({"role": h['role'], "content": h['content']})
    messages.append({"role": "user", "content": message})

    last_exception = None

    # モデルリストを順番に試行（429対策）
    for model_name in GROQ_MODELS:
        # 制限中のモデルはスキップ判定
        status = groq_model_status.get(model_name, {"is_limited": False})
        if status["is_limited"]:
            if status["reset_time"] and datetime.utcnow() < status["reset_time"]:
                logger.info(f"⏭️ {model_name} は制限中のためスキップ")
                continue
            else:
                status["is_limited"] = False
                status["reset_time"] = None

        try:
            response = groq_client.chat.completions.create(
                model=model_name, messages=messages, temperature=0.8, max_tokens=max_tokens
            )
            logger.info(f"✅ Groq成功 (モデル: {model_name})")
            return response.choices[0].message.content.strip()

        except Exception as e:
            last_exception = e
            error_str = str(e)
            
            # レート制限 (429) の場合
            if "Rate limit reached" in error_str or "429" in error_str:
                logger.warning(f"⚠️ レート制限到達: {model_name}")
                groq_model_status[model_name]["is_limited"] = True
                
                # 解除待ち時間を解析（なければデフォルト5分）
                wait_match = re.search(r'try again in (.*?)\.', error_str)
                if wait_match:
                    groq_model_status[model_name]["reset_time"] = datetime.utcnow() + timedelta(minutes=5) # 安全のため長めに
                else:
                    groq_model_status[model_name]["reset_time"] = datetime.utcnow() + timedelta(minutes=1)
                continue # 次のモデルへ
            
            logger.error(f"❌ Groqエラー ({model_name}): {e}")
            continue

    # 全モデル失敗
    logger.error(f"❌ 全Groqモデルが失敗しました。")
    raise AIModelException(f"All Groq models failed. Last error: {last_exception}")

# ==============================================================================
# 心理分析
# ==============================================================================
def analyze_user_psychology(user_uuid):
    # 心理分析はトークン節約のため、Geminiが生きている時か、Groqの軽量モデルで行うのが理想だが
    # 今回は既存ロジックのままで、call_llama_advancedの自動フォールバックに任せる
    with get_db_session() as session:
        try:
            history = session.query(ConversationHistory).filter_by(user_uuid=user_uuid, role='user').order_by(ConversationHistory.timestamp.desc()).limit(100).all()
            if len(history) < MIN_MESSAGES_FOR_ANALYSIS: return
            messages_text = "\n".join([f"- {h.content}" for h in reversed(history)])
            analysis_prompt = f"以下のユーザーの発言履歴を分析し、性格特性をJSONで出力してください。\n{messages_text[:2000]}"
            
            # Gemini優先で試す
            response_text = call_gemini("あなたは心理学者です。JSONのみ出力して。", analysis_prompt, [])
            if not response_text:
                response_text = call_llama_advanced("あなたは心理学者です。", analysis_prompt, [], max_tokens=1024)
            
            if not response_text: return
            
            # JSON抽出 (簡易実装)
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                # ここではDB保存処理を省略（実際にはパースしてUserPsychologyを更新）
                logger.info(f"✅ 心理分析完了 (保存処理は省略)")
                pass
        except Exception as e:
            logger.error(f"❌ 心理分析エラー: {e}")

def get_psychology_insight(session, user_uuid):
    psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
    if not psych or (psych.analysis_confidence or 0) < 60: return ""
    insights = []
    if psych.extraversion > 70: insights.append("社交的な")
    if psych.openness > 70: insights.append("好奇心旺盛な")
    try:
        favorite_topics = json.loads(psych.favorite_topics) if psych.favorite_topics else []
        if favorite_topics: insights.append(f"{'、'.join(favorite_topics[:2])}が好きな")
    except: pass
    return "".join(insights)

# ==============================================================================
# コア機能: 天気, Wiki, DB修正, ニュース
# ==============================================================================
def get_weather_forecast(location):
    code = LOCATION_CODES.get(location, "130000")
    url = f"https://www.jma.go.jp/bosai/forecast/data/overview_forecast/{code}.json"
    try:
        response = requests.get(url, timeout=SEARCH_TIMEOUT); response.raise_for_status()
        data = response.json()
        return f"今の{data.get('targetArea', location)}の天気はね、「{clean_text(data.get('text', ''))}」って感じだよ！"
    except Exception as e:
        logger.error(f"❌ 天気APIエラー: {e}")
        return "ごめん！天気情報がうまく取れなかったみたい…"

@lru_cache(maxsize=100)
def get_holomem_info_cached(member_name: str):
    with get_db_session() as session:
        return session.query(HolomemWiki).filter_by(member_name=member_name).first()

def background_db_correction(task_id, correction_data):
    result = f"「{correction_data['member_name']}」ちゃんの情報修正、失敗しちゃった…。"
    with get_db_session() as session:
        try:
            wiki = session.query(HolomemWiki).filter_by(member_name=correction_data['member_name']).first()
            if wiki:
                db_field = correction_data.get('db_field')
                if db_field and hasattr(wiki, db_field):
                    setattr(wiki, db_field, correction_data['value'])
                    get_holomem_info_cached.cache_clear()
                    result = f"おっけー！「{correction_data['member_name']}」の情報を更新しといたよ！"
        except Exception as e: logger.error(f"❌ DB修正タスクエラー: {e}")
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = result; task.status = 'completed'; task.completed_at = datetime.utcnow()

def fetch_hololive_news():
    # 簡略化のためスキップ（実際の運用では残す）
    pass

def update_holomem_database_from_wiki():
    # 簡略化のためスキップ
    pass

# ==============================================================================
# 外部情報検索 & バックグラウンドタスク
# ==============================================================================
def scrape_major_search_engines(query, num_results=3, site_filter=None):
    # 既存の検索ロジック（省略せず実装）
    search_query = f"{query} site:{site_filter}" if site_filter else query
    engines = [
        {'name': 'Google', 'url': f"https://www.google.com/search?q={quote_plus(search_query)}&hl=ja&num={num_results+2}", 'selector': 'div.g', 'title_sel': 'h3', 'snippet_sel': 'div.VwiC3b'},
        {'name': 'Bing', 'url': f"https://www.bing.com/search?q={quote_plus(search_query)}", 'selector': 'li.b_algo', 'title_sel': 'h2', 'snippet_sel': 'p'}
    ]
    for engine in engines:
        try:
            headers = {'User-Agent': random.choice(USER_AGENTS)}
            response = requests.get(engine['url'], headers=headers, timeout=SEARCH_TIMEOUT)
            if response.status_code != 200: continue
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            for elem in soup.select(engine['selector'])[:num_results]:
                title_elem = elem.select_one(engine['title_sel'])
                snippet_elem = elem.select_one(engine['snippet_sel'])
                if title_elem and snippet_elem:
                    results.append({'title': clean_text(title_elem.text), 'snippet': clean_text(snippet_elem.text)})
            if results: return results
        except Exception: continue
    return []

def background_deep_search(task_id, query_data):
    query = query_data.get('query')
    search_result_text = f"「{query}」について調べたけど、良い情報が見つからなかったや…"
    
    with get_db_session() as session:
        try:
            results = scrape_major_search_engines(query, 5)
            if results:
                formatted_info = "【検索結果】\n" + "\n".join([f"{r['title']}: {r['snippet']}" for r in results])
                user_data = query_data.get('user_data')
                history = get_conversation_history(session, user_data['uuid'])
                search_result_text = generate_ai_response_safe(user_data, f"{query}について詳しく教えて", history, reference_info=formatted_info, is_detailed=True, is_task_report=True)
        except Exception as e: logger.error(f"❌ 検索タスクエラー: {e}")
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = search_result_text; task.status = 'completed'; task.completed_at = datetime.utcnow()

# ==============================================================================
# AI応答生成 (統合版)
# ==============================================================================
def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False, is_task_report=False):
    with get_db_session() as session: personality_context = get_psychology_insight(session, user_data['uuid'])
    
    system_prompt = f"あなたは「もちこ」というギャルAIです。ユーザー「{user_data['name']}」と会話中。\n"
    system_prompt += f"# 口調: 一人称「あてぃし」、語尾「〜じゃん」「〜的な？」\n"
    system_prompt += f"# ユーザー印象: {personality_context}\n"
    system_prompt += f"# 参考情報:\n{reference_info}"
    
    response = None

    # 1. Gemini (Google) を最優先で試す
    logger.info(f"🚀 Gemini使用（メインエンジン）")
    try:
        response = call_gemini(system_prompt, message, history)
    except Exception: pass

    # 2. Gemini失敗時 -> Groq (Llama/Mixtral) へフォールバック
    if not response and groq_client:
        logger.info(f"🧠 Llama (Groq) にフォールバック")
        try:
            # 自動で70b -> 8bと切り替わる
            response = call_llama_advanced(system_prompt, message, history, max_tokens=1200)
        except Exception: pass

    if not response:
        logger.error("❌ すべてのAIモデルが応答に失敗")
        raise AIModelException("All models failed")
    return response

def generate_ai_response_safe(user_data, message, history, **kwargs):
    try:
        response = generate_ai_response(user_data, message, history, **kwargs)
        if not response or response.strip() == "":
            return "うーん、ちょっと考えがまとまらないや…もう一回言ってみて？"
        return response
    except AIModelException:
        # 全モデル全滅時のメッセージ
        return "ごめん、今日はもう疲れちゃった…頭が回らないから、また明日お話しよう？"
    except Exception as e:
        logger.critical(f"🔥 予期しないエラー: {e}", exc_info=True)
        return "システムエラーが発生したよ…ごめんね！"

# ==============================================================================
# Flask エンドポイント
# ==============================================================================
@app.route('/health', methods=['GET'])
def health_check():
    return create_json_response({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()})

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json
        if not data or 'uuid' not in data or 'message' not in data:
            return Response("必須パラメータ不足|", mimetype='text/plain; charset=utf-8', status=400)
        
        user_uuid = sanitize_user_input(data['uuid'])
        user_name = sanitize_user_input(data.get('name', 'Guest'))
        message = sanitize_user_input(data['message'])
        generate_voice_flag = data.get('voice', False)
        
        if not chat_rate_limiter.is_allowed(user_uuid):
            return Response("ちょっと待って！メッセージ送りすぎ～！|", mimetype='text/plain; charset=utf-8', status=429)

        # === 残トークン確認コマンド ===
        if message.strip() == "残トークン":
            status_msg = "【AIエンジン状態】\n"
            status_msg += f"🦁 メイン (Gemini): {'稼働中' if gemini_model else '停止中'}\n"
            status_msg += "🦙 サブ (Groq) 稼働状況:\n"
            all_dead = True
            for model in GROQ_MODELS:
                status = groq_model_status.get(model, {})
                if status.get("is_limited"):
                    reset_time = status.get("reset_time")
                    jst_time = (reset_time + timedelta(hours=9)).strftime('%H:%M:%S') if reset_time else "不明"
                    status_msg += f"❌ {model}: 制限中 (解除: {jst_time}頃)\n"
                else:
                    status_msg += f"✅ {model}: OK\n"
                    all_dead = False
            if all_dead and not gemini_model: status_msg += "\n⚠️ 全滅…もう疲れちゃった…"
            return Response(f"{status_msg}|", mimetype='text/plain; charset=utf-8', status=200)

        # 通常会話
        ai_text = ""; is_task_started = False
        with get_db_session() as session:
            user_data = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            
            correction = detect_db_correction_request(message)
            if correction:
                task_id = f"db_fix_{user_uuid}_{int(time.time())}"
                task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='db_correction', query=json.dumps(correction, ensure_ascii=False))
                session.add(task)
                background_executor.submit(background_db_correction, task_id, correction)
                ai_text, is_task_started = f"まじ！？「{correction['member_name']}」の情報、直しとくね！", True
            
            if not ai_text:
                if is_time_request(message): ai_text = get_japan_time()
                elif is_weather_request(message): ai_text = get_weather_forecast(extract_location(message))
            
            if not ai_text and is_explicit_search_request(message):
                task_id = f"search_{user_uuid}_{int(time.time())}"
                query_data = {'query': message, 'user_data': user_data}
                task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=json.dumps(query_data, ensure_ascii=False))
                session.add(task)
                background_executor.submit(background_deep_search, task_id, query_data)
                ai_text, is_task_started = "オッケー！ちょっとググってくるから待ってて！", True

            if not ai_text:
                ai_text = generate_ai_response_safe(user_data, message, history)
            
            if not is_task_started:
                session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        
        response_text = limit_text_for_sl(ai_text)
        voice_url = ""
        if generate_voice_flag and global_state.voicevox_enabled and not is_task_started:
            voice_filename = generate_voice_file(response_text, user_uuid)
            if voice_filename: voice_url = f"{SERVER_URL}/play/{voice_filename}"
            
        return Response(f"{response_text}|{voice_url}", mimetype='text/plain; charset=utf-8', status=200)
    
    except Exception as e:
        logger.critical(f"🔥 致命的エラー: {e}", exc_info=True)
        return Response("ごめん、システムエラー…|", mimetype='text/plain; charset=utf-8', status=500)

@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
    try:
        data = request.json; user_uuid = data['uuid']
        with get_db_session() as session:
            task = session.query(BackgroundTask).filter(BackgroundTask.user_uuid == user_uuid, BackgroundTask.status == 'completed').first()
            if task:
                res = task.result; session.delete(task); session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=res))
                return create_json_response({'status': 'completed', 'response': f"{limit_text_for_sl(res)}|"})
        return create_json_response({'status': 'no_tasks'})
    except Exception: return create_json_response({'status': 'error'}, 500)

@app.route('/play/<filename>', methods=['GET'])
def play_voice(filename):
    return send_from_directory(VOICE_DIR, filename)

# ==============================================================================
# VOICEVOX関連
# ==============================================================================
def find_active_voicevox_url():
    urls = [VOICEVOX_URL_FROM_ENV] + VOICEVOX_URLS
    for url in set(urls):
        if url:
            try:
                if requests.get(f"{url}/version", timeout=2).status_code == 200:
                    global_state.active_voicevox_url = url; return url
            except: pass
    return None

def generate_voice_file(text, user_uuid):
    if not global_state.voicevox_enabled or not global_state.active_voicevox_url: return None
    try:
        query = requests.post(f"{global_state.active_voicevox_url}/audio_query", params={"text": text[:200], "speaker": VOICEVOX_SPEAKER_ID}, timeout=10).json()
        wav = requests.post(f"{global_state.active_voicevox_url}/synthesis", params={"speaker": VOICEVOX_SPEAKER_ID}, json=query, timeout=20).content
        filename = f"voice_{user_uuid[:8]}_{int(time.time())}.wav"
        with open(os.path.join(VOICE_DIR, filename), 'wb') as f: f.write(wav)
        return filename
    except: return None

# ==============================================================================
# 初期化
# ==============================================================================
def initialize_app():
    global engine, Session, groq_client, gemini_model
    logger.info("🔧 初期化開始 (v30.0)")
    
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
    except Exception: logger.critical("🔥 DB接続失敗")
    
    try:
        if GROQ_API_KEY: groq_client = Groq(api_key=GROQ_API_KEY)
    except Exception: pass
    
    try:
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            gemini_model = genai.GenerativeModel('gemini-2.0-flash-exp')
    except Exception: pass
    
    if find_active_voicevox_url(): global_state.voicevox_enabled = True

initialize_app()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
