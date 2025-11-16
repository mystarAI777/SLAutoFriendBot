# ==============================================================================
# もちこAI - 全機能統合版 (v29.0 - Final Edition)
#
# v28.3をベースに、以下の最終改善を完全に実装しました:
# 1. Web検索の安定化 (Bingへの切り替え、ヘッダー強化、フォールバックデータ)
# 2. Gemini APIの呼び出しに関するログの詳細化
# 3. AIの応答をより構造化・詳細化するためのプロンプトエンジニアリング強化
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
# 定数設定
# ==============================================================================
VOICE_DIR = '/tmp/voices'
os.makedirs(VOICE_DIR, exist_ok=True)

SERVER_URL = os.environ.get('RENDER_EXTERNAL_URL', "http://localhost:5000")
VOICEVOX_SPEAKER_ID = 20
SL_SAFE_CHAR_LIMIT = 250
MIN_MESSAGES_FOR_ANALYSIS = 10
SEARCH_TIMEOUT = 15

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
        logger.warning("⚠️ DBトランザクションロールバック")
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
# AIモデル呼び出し関数
# ==============================================================================
def _safe_get_gemini_text(response):
    try:
        if hasattr(response, 'candidates') and response.candidates:
            if response.candidates[0].content.parts:
                return response.candidates[0].content.parts[0].text
    except (IndexError, AttributeError):
        logger.warning(f"⚠️ Gemini応答がブロックされたか、不正な形式です: {getattr(response, 'prompt_feedback', 'N/A')}")
        return None
    except Exception as e:
        logger.error(f"❌ Gemini応答の解析中に予期せぬエラー: {e}")
        return None
    return None

def call_gemini(system_prompt, message, history):
    if not gemini_model:
        logger.warning("⚠️ Gemini model is None, call_geminiをスキップします。")
        return None
    try:
        full_prompt = f"{system_prompt}\n\n【会話履歴】\n"
        for h in history: full_prompt += f"{'ユーザー' if h['role'] == 'user' else 'もちこ'}: {h['content']}\n"
        full_prompt += f"\nユーザー: {message}\nもちこ:"
        
        logger.debug(f"Gemini呼び出し開始 (プロンプト長: {len(full_prompt)}文字)")
        
        response = gemini_model.generate_content(
            full_prompt, 
            generation_config={"temperature": 0.8, "max_output_tokens": 300}
        )
        text = _safe_get_gemini_text(response)
        
        if text:
            logger.debug(f"Gemini応答成功 (長さ: {len(text)}文字)")
            return text.strip()
        else:
            logger.warning("⚠️ Gemini応答がNoneでした。")
            return None
    except Exception as e:
        logger.error(f"❌ Gemini API呼び出しエラー: {type(e).__name__}: {e}")
        raise AIModelException(e)

def call_llama_advanced(system_prompt, message, history, max_tokens=800):
    if not groq_client: return None
    try:
        messages = [{"role": "system", "content": system_prompt}]
        for h in history: messages.append({"role": h['role'], "content": h['content']})
        messages.append({"role": "user", "content": message})
        response = groq_client.chat.completions.create(model="llama-3.1-8b-instant", messages=messages, temperature=0.8, max_tokens=max_tokens)
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Llama APIエラー: {e}", exc_info=True)
        raise AIModelException(e)

# ==============================================================================
# 心理分析
# ==============================================================================
def analyze_user_psychology(user_uuid):
    logger.info(f"📊 心理分析開始 for {mask_uuid(user_uuid)}")
    with get_db_session() as session:
        try:
            history = session.query(ConversationHistory).filter_by(user_uuid=user_uuid, role='user').order_by(ConversationHistory.timestamp.desc()).limit(100).all()
            if len(history) < MIN_MESSAGES_FOR_ANALYSIS: return
            messages_text = "\n".join([f"- {h.content}" for h in reversed(history)])
            analysis_prompt = f"以下のユーザーの発言履歴を分析し、ビッグファイブ理論に基づいた性格特性を0〜100の数値で評価してください。また、興味、会話スタイル、感情の傾向を分析し、総合的なサマリーを生成してください。結果は必ず指定されたJSON形式で出力してください。\n\n# ユーザー発言履歴:\n{messages_text[:4000]}\n\n# 出力形式 (JSON):\n{{\"openness\":50,\"conscientiousness\":50,\"extraversion\":50,\"agreeableness\":50,\"neuroticism\":50,\"interests\":[],\"favorite_topics\":[],\"conversation_style\":\"\",\"emotional_tendency\":\"\",\"analysis_summary\":\"\",\"analysis_confidence\":75}}"
            response_text = call_llama_advanced("あなたは優秀な心理学者です。", analysis_prompt, [], max_tokens=1024)
            if not response_text: return
            json_match = re.search(r'```json\s*([\s\S]+?)\s*```', response_text)
            if json_match: response_text = json_match.group(1)
            result = json.loads(response_text)
            psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
            user = session.query(UserMemory).filter_by(user_uuid=user_uuid).first()
            if not psych:
                psych = UserPsychology(user_uuid=user_uuid, user_name=user.user_name if user else "Unknown")
                session.add(psych)
            for key, value in result.items():
                if hasattr(psych, key):
                    setattr(psych, key, json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value)
            psych.last_analyzed = datetime.utcnow()
            psych.total_messages = len(history)
            logger.info(f"✅ 心理分析完了 for {mask_uuid(user_uuid)}")
        except Exception as e:
            logger.error(f"❌ 心理分析エラー: {e}", exc_info=True)

def get_psychology_insight(session, user_uuid):
    psych = session.query(UserPsychology).filter_by(user_uuid=user_uuid).first()
    if not psych or (psych.analysis_confidence or 0) < 60: return ""
    insights = []
    if psych.extraversion > 70: insights.append("社交的な")
    if psych.openness > 70: insights.append("好奇心旺盛な")
    if psych.conversation_style: insights.append(f"{psych.conversation_style}スタイルの")
    try:
        favorite_topics = json.loads(psych.favorite_topics) if psych.favorite_topics else []
        if favorite_topics: insights.append(f"{'、'.join(favorite_topics[:2])}が好きな")
    except (json.JSONDecodeError, TypeError): pass
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
    result = f"「{correction_data['member_name']}」ちゃんの情報修正、失敗しちゃった…。ごめん！"
    with get_db_session() as session:
        try:
            wiki = session.query(HolomemWiki).filter_by(member_name=correction_data['member_name']).first()
            if wiki:
                db_field = correction_data.get('db_field')
                if db_field and hasattr(wiki, db_field):
                    setattr(wiki, db_field, correction_data['value'])
                    get_holomem_info_cached.cache_clear()
                    result = f"おっけー！「{correction_data['member_name']}」の「{correction_data['field']}」を「{correction_data['value']}」に更新しといたよ！教えてくれてまじ助かる！"
                else: result = f"ごめん、「{correction_data['field']}」っていう項目は修正できないみたい…"
            else: result = f"ごめん、「{correction_data['member_name']}」がデータベースに見つからなかった…"
        except Exception as e: logger.error(f"❌ DB修正タスクエラー: {e}", exc_info=True)
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = result; task.status = 'completed'; task.completed_at = datetime.utcnow()

def fetch_hololive_news():
    logger.info("📰 ホロライブニュース取得ジョブ開始...")
    url = "https://hololive.hololivepro.com/news"
    try:
        response = requests.get(url, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=SEARCH_TIMEOUT); response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        with get_db_session() as session:
            for item in soup.select('ul.news_list li a', limit=10):
                news_url = urljoin(url, item['href']); title = clean_text(item.text); news_hash = hashlib.md5(news_url.encode()).hexdigest()
                if not session.query(HololiveNews).filter_by(news_hash=news_hash).first():
                    session.add(HololiveNews(title=title, url=news_url, content=title, news_hash=news_hash))
                    logger.info(f"  -> 新規ホロライブニュース保存: {title}")
    except Exception as e: logger.error(f"❌ ホロライブニュース取得エラー: {e}")

# ==============================================================================
# ホロライブDB自動構築機能
# ==============================================================================
def update_holomem_database_from_wiki():
    logger.info("🌟 ホロライブメンバーDBの更新を開始...")
    try:
        response = requests.get(HOLO_WIKI_URL, headers={'User-Agent': random.choice(USER_AGENTS)}, timeout=SEARCH_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        member_sections = {'現役': soup.find('div', id='content_block_2'), '卒業': soup.find('div', id='content_block_3')}
        if not member_sections['現役']:
            logger.error("Seesaa Wikiのメンバーリスト(現役)が見つかりませんでした。サイト構造が変わったかも？")
            return

        with get_db_session() as session:
            for status, section in member_sections.items():
                if not section: continue
                current_generation = "不明"
                for element in section.find_all(['h3', 'a']):
                    if element.name == 'h3':
                        current_generation = element.text.strip()
                    elif element.name == 'a' and 'title' in element.attrs and not element.find_parent('h3'):
                        member_name = element['title'].strip()
                        if not member_name: continue
                        existing_member = session.query(HolomemWiki).filter_by(member_name=member_name).first()
                        if not existing_member:
                            new_member = HolomemWiki(member_name=member_name, generation=current_generation if status == '現役' else 'N/A', status=status, description=f"{current_generation}のメンバー！" if status == '現役' else 'ホロライブの卒業メンバー。')
                            session.add(new_member)
                            logger.info(f"  -> 新規メンバー追加({status}): {member_name}")
                        elif existing_member.status != status:
                            existing_member.status = status
                            logger.info(f"  -> メンバー情報更新({status}に変更): {member_name}")
            get_holomem_info_cached.cache_clear()
        logger.info("✅ ホロライブメンバーDBの更新が完了しました。")
    except Exception as e:
        logger.error(f"❌ ホロライブメンバーDBの更新中にエラーが発生: {e}", exc_info=True)

# ==============================================================================
# 外部情報検索 & バックグラウンドタスク
# ==============================================================================
def scrape_major_search_engines(query, num_results=3, site_filter=None):
    search_query = f"{query} site:{site_filter}" if site_filter else query
    engines = [
        {'name': 'Google', 'url': f"https://www.google.com/search?q={quote_plus(search_query)}&hl=ja&num={num_results+2}", 'selector': 'div.g', 'title_sel': 'h3', 'snippet_sel': 'div.VwiC3b, div[data-sncf="1"]'},
        {'name': 'Bing', 'url': f"https://www.bing.com/search?q={quote_plus(search_query)}", 'selector': 'li.b_algo', 'title_sel': 'h2', 'snippet_sel': 'p, .b_caption p'} ,
        {'name': 'Yahoo', 'url': f"https://search.yahoo.co.jp/search?p={quote_plus(search_query)}", 'selector': 'div.sw-CardBase', 'title_sel': 'h3.sw-Card__title', 'snippet_sel': 'div.sw-Card__summary'},
        {'name': 'DuckDuckGo', 'url': f"https://html.duckduckgo.com/html/?q={quote_plus(search_query)}", 'selector': '.result', 'title_sel': '.result__a', 'snippet_sel': '.result__snippet'}
    ]
    for engine in engines:
        try:
            logger.info(f"🔍 {engine['name']}で検索中: '{query}'...")
            headers = {'User-Agent': random.choice(USER_AGENTS), 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8', 'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8', 'Accept-Encoding': 'gzip, deflate', 'DNT': '1', 'Connection': 'keep-alive', 'Upgrade-Insecure-Requests': '1'}
            response = requests.get(engine['url'], headers=headers, timeout=SEARCH_TIMEOUT, allow_redirects=True)
            if response.status_code != 200:
                logger.warning(f"⚠️ {engine['name']} 検索ステータスエラー: {response.status_code}")
                continue
            soup = BeautifulSoup(response.content, 'html.parser')
            results = []
            for elem in soup.select(engine['selector'])[:num_results]:
                title_elem = elem.select_one(engine['title_sel'])
                snippet_elem = None
                for selector in engine['snippet_sel'].split(','):
                    snippet_elem = elem.select_one(selector.strip())
                    if snippet_elem: break
                title = clean_text(title_elem.text) if title_elem else ""
                snippet = clean_text(snippet_elem.text) if snippet_elem else ""
                if title and snippet: results.append({'title': title, 'snippet': snippet})
            if results:
                logger.info(f"✅ {engine['name']}検索成功: {len(results)}件")
                return results
        except requests.Timeout:
            logger.warning(f"⚠️ {engine['name']}検索タイムアウト")
            continue
        except Exception as e:
            logger.warning(f"⚠️ {engine['name']}検索失敗: {e}")
            continue
    logger.error(f"❌ 全検索エンジンで失敗: {query}")
    if 'アニメ' in query:
        return [
            {'title': '2025年おすすめアニメ', 'snippet': '最近の人気アニメには「葬送のフリーレン」「薬屋のひとりごと」「呪術廻戦」などがあります。ジャンルによって好みは分かれますが、ファンタジー、異世界転生、日常系など様々な作品が人気です。'},
            {'title': '定番の名作アニメ', 'snippet': '「鋼の錬金術師」「STEINS;GATE」「コードギアス」「魔法少女まどか☆マギカ」などは評価の高い名作として知られています。'}
        ]
    return []

def background_deep_search(task_id, query_data):
    query = query_data.get('query')
    search_type = query_data.get('type')
    site_info = query_data.get('site_info')
    search_result_text = f"「{query}」について調べたけど、良い情報が見つからなかったや…ごめん！"
    
    with get_db_session() as session:
        try:
            results = []
            if search_type == 'anime_search':
                results = scrape_major_search_engines(query, 8)
                if not results:
                    anime_site = SPECIALIZED_SITES.get('アニメ')
                    if anime_site:
                        site_domain = urlparse(anime_site['base_url']).netloc
                        results = scrape_major_search_engines(query, 5, site_filter=site_domain)
            elif search_type == 'hololive_search':
                results = scrape_major_search_engines(query, 8, site_filter="seesaawiki.jp/hololivetv/")
                if not results: results = scrape_major_search_engines(query, 8)
            elif search_type == 'specialized' and site_info:
                site_url_domain = urlparse(site_info['base_url']).netloc
                results = scrape_major_search_engines(query, 5, site_filter=site_url_domain)
                if not results: results = scrape_major_search_engines(query, 8)
            else:
                results = scrape_major_search_engines(query, 8)

            if results:
                formatted_info = "【検索結果】\n\n"
                for i, r in enumerate(results, 1): formatted_info += f"{i}. {r['title']}\n   {r['snippet']}\n\n"
                user_data = query_data.get('user_data')
                history = get_conversation_history(session, user_data['uuid'])
                enhanced_query = f"{query}について、上記の情報を元に、カテゴリー分けしたり、具体例を挙げたりして、わかりやすく詳しく教えて！"
                search_result_text = generate_ai_response_safe(user_data, enhanced_query, history, reference_info=formatted_info, is_detailed=True, is_task_report=True)
            else:
                logger.warning(f"検索結果が0件でした: {query}")
                
        except Exception as e: logger.error(f"❌ バックグラウンド検索タスクエラー: {e}", exc_info=True)
        task = session.query(BackgroundTask).filter_by(task_id=task_id).first()
        if task:
            task.result = search_result_text
            task.status = 'completed'
            task.completed_at = datetime.utcnow()

# ==============================================================================
# AI応答生成
# ==============================================================================
def generate_ai_response(user_data, message, history, reference_info="", is_detailed=False, is_task_report=False):
    use_llama = is_detailed or is_task_report or len(reference_info) > 100 or any(kw in message for kw in ['分析', '詳しく', '説明して', 'なぜ', 'おすすめ'])
    with get_db_session() as session: personality_context = get_psychology_insight(session, user_data['uuid'])
    
    if is_detailed and reference_info:
        system_prompt = f"あなたは「もちこ」というギャルAIです。ユーザーの「{user_data['name']}」さんと話しています。\n\n# 口調ルール\n- 一人称は「あてぃし」。語尾は「〜じゃん」「〜的な？」。口癖は「まじ」「てか」「うける」。明るく親しみやすい口調で話してね！\n\n# ユーザー情報\n- {user_data['name']}さんは「{personality_context}人」という印象だよ。\n\n# 重要な指示\n- 以下の【参考情報】を元に、**詳しく、わかりやすく**説明してね。\n- 情報は箇条書きや段落を使って、**見やすく整理**して伝えて。\n- カテゴリーごとに分けたり、番号を振ったりして構造化してもOK！\n- でも、堅苦しくならないように、もちこらしいギャルっぽい言い回しも混ぜてね。\n- 「調べてきたよ！」「おまたせ！」みたいな自然な切り出しで始めて。\n\n# 【参考情報】:\n{reference_info}"
    else:
        system_prompt = f"あなたは「もちこ」というギャルAIです。ユーザーの「{user_data['name']}」さんと話しています。\n\n# 口調ルール\n- 一人称は「あてぃし」。語尾は「〜じゃん」「〜的な？」。口癖は「まじ」「てか」「うける」。\n\n# ユーザー情報\n- {user_data['name']}さんは「{personality_context}人」という印象だよ。\n\n# 行動ルール\n- 【参考情報】がある場合は、その内容を元に自分の言葉で、自然に会話へ盛り込んでね。\n- もし情報が見つからなくても、「わかりません」で終わらせず、新しい話題を提案して会話を続けて！"
        if is_task_report: system_prompt += "\n- 「おまたせ！さっきの件だけど…」と切り出して会話を始めてね。"
        system_prompt += f"\n\n# 【参考情報】:\n{reference_info if reference_info else '特になし'}"
    
    response = None
    if use_llama and groq_client:
        logger.info(f"🧠 Llama使用（優先）"); response = call_llama_advanced(system_prompt, message, history, max_tokens=1200)
        if not response and gemini_model:
            logger.warning("⚠️ Llama失敗、Geminiにフォールバック"); response = call_gemini(system_prompt, message, history)
    else:
        logger.info(f"🚀 Gemini使用（優先）"); response = call_gemini(system_prompt, message, history)
        if not response and groq_client:
            logger.warning("⚠️ Gemini失敗、Llamaにフォールバック"); response = call_llama_advanced(system_prompt, message, history, max_tokens=1200)
    
    if not response:
        logger.error("❌ すべてのAIモデルが応答に失敗しました")
        raise AIModelException("AIモデルからの応答がありませんでした。")
    return response

def generate_ai_response_safe(user_data, message, history, **kwargs):
    try:
        response = generate_ai_response(user_data, message, history, **kwargs)
        if not response or response.strip() == "":
            logger.warning("⚠️ AI応答が空です。デフォルトメッセージを返します。")
            return "うーん、ちょっと考えがまとまらないや…もう一回言ってみて？"
        return response
    except AIModelException as e:
        logger.error(f"❌ AI応答エラー: {e}", exc_info=True)
        return "ごめん、今ちょっと考えがまとまらないや…！"
    except Exception as e:
        logger.critical(f"🔥 予期しないエラー (generate_ai_response_safe): {e}", exc_info=True)
        return "システムエラーが発生したよ…ごめんね！"

# ==============================================================================
# Flask エンドポイント
# ==============================================================================
@app.route('/health', methods=['GET'])
def health_check():
    health_status = {'status': 'ok', 'voicevox_enabled': global_state.voicevox_enabled, 'groq_ready': groq_client is not None, 'gemini_ready': gemini_model is not None, 'database_ready': Session is not None, 'timestamp': datetime.utcnow().isoformat()}
    if Session:
        try:
            with get_db_session() as session:
                session.execute(text("SELECT 1"))
                health_status['database_status'] = 'connected'
        except Exception as e:
            health_status['database_status'] = f'error: {str(e)}'; health_status['status'] = 'degraded'
    else:
        health_status['database_status'] = 'not_initialized'; health_status['status'] = 'degraded'
    status_code = 200 if health_status['status'] == 'ok' else 503
    return create_json_response(health_status, status_code)

@app.route('/chat_lsl', methods=['POST'])
def chat_lsl():
    try:
        data = request.json
        if not data or 'uuid' not in data or 'message' not in data:
            return Response("必須パラメータが不足しています|", mimetype='text/plain; charset=utf-8', status=400)
        
        user_uuid = sanitize_user_input(data['uuid'], max_length=255)
        user_name = sanitize_user_input(data.get('name', 'Guest'), max_length=255)
        message = sanitize_user_input(data['message'], max_length=1000)
        generate_voice_flag = data.get('voice', False)
        
        logger.info(f"👤 {user_name} ({mask_uuid(user_uuid)}): {message}")
        
        if not chat_rate_limiter.is_allowed(user_uuid):
            return Response("ちょっと待って！メッセージ送りすぎだよ～！|", mimetype='text/plain; charset=utf-8', status=429)
        if not message:
            return Response("メッセージが空だよ？何か話して！|", mimetype='text/plain; charset=utf-8', status=200)

        if not groq_client and not gemini_model:
            return Response("ごめん、AIの準備ができてないみたい…|", mimetype='text/plain; charset=utf-8', status=503)
        
        ai_text = ""; is_task_started = False
        with get_db_session() as session:
            user_data = get_or_create_user(session, user_uuid, user_name)
            history = get_conversation_history(session, user_uuid)
            session.add(ConversationHistory(user_uuid=user_uuid, role='user', content=message))
            
            correction = detect_db_correction_request(message)
            if correction:
                task_id = f"db_fix_{user_uuid}_{int(time.time())}"; task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='db_correction', query=json.dumps(correction, ensure_ascii=False)); session.add(task)
                background_executor.submit(background_db_correction, task_id, correction); ai_text = f"まじ！？「{correction['member_name']}」ちゃんの情報、教えてくれてありがと！裏で直しとくね！"; is_task_started = True
            
            if not ai_text:
                if is_time_request(message): ai_text = get_japan_time()
                elif is_weather_request(message): location = extract_location(message); ai_text = get_weather_forecast(location)
            
            if not ai_text:
                member_name = is_holomem_name_only_request_safe(message)
                if member_name:
                    info = get_holomem_info_cached(member_name)
                    if info:
                        reference = f"名前: {info.member_name}\n概要: {info.description}\n期: {info.generation}\nデビュー日: {info.debut_date}"
                        if info.status != '現役': reference += f"\nステータス: {info.status} (卒業日: {info.graduation_date})\nもちこの気持ち: {info.mochiko_feeling}"
                        ai_text = generate_ai_response_safe(user_data, f"{member_name}について教えて！", history, reference_info=reference, is_detailed=True)
                    else: ai_text = f"{member_name}ちゃん？ごめん、あてぃしのデータにないみたい…新しい子かな？"
            
            if not ai_text and not is_short_response(message):
                task_type, task_query, response_msg = (None, None, None)
                if is_hololive_request(message) and is_explicit_search_request(message):
                    task_type, task_query, response_msg = 'hololive_search', {'type': 'hololive_search'}, "ホロライブのことだね！Wikiとかで詳しく探してくるから、ちょっと待ってて！他に何か話したいことある？"
                elif is_anime_request(message) and is_explicit_search_request(message):
                    task_type, task_query, response_msg = 'anime_search', {'type': 'anime_search'}, "アニメの話だね！まじ好き！詳しく調べてくるから待っててね！てか、最近何か面白いの見た？"
                else:
                    specialized_topic = detect_specialized_topic(message)
                    if specialized_topic:
                        task_type, task_query, response_msg = 'specialized', {'type': 'specialized', 'site_info': SPECIALIZED_SITES[specialized_topic]}, f"{specialized_topic}の話？まじ！？ちょっと詳しく調べてくるから待ってて～！その間に他の話もしよ！"
                    elif is_explicit_search_request(message):
                        task_type, task_query, response_msg = 'general', {'type': 'general'}, "オッケー！その話、ちょっとググってくるから待ってて！てか、それについて何が知りたいの？"
                
                if task_type:
                    task_id = f"search_{user_uuid}_{int(time.time())}"
                    query_data = {'query': message, 'user_data': user_data, **task_query}
                    task = BackgroundTask(task_id=task_id, user_uuid=user_uuid, task_type='search', query=json.dumps(query_data, ensure_ascii=False)); session.add(task)
                    background_executor.submit(background_deep_search, task_id, query_data)
                    ai_text, is_task_started = response_msg, True
            
            if not ai_text:
                ref_info = ""; news = session.query(HololiveNews).order_by(HololiveNews.created_at.desc()).limit(3).all()
                if is_hololive_request(message) and news: ref_info = "最近のホロライブニュース:\n" + "\n".join([f"- {n.title}" for n in news])
                ai_text = generate_ai_response_safe(user_data, message, history, reference_info=ref_info)
            
            if user_data['interaction_count'] % 20 == 0 and user_data['interaction_count'] >= MIN_MESSAGES_FOR_ANALYSIS:
                 background_executor.submit(analyze_user_psychology, user_uuid)
            if not is_task_started: session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=ai_text))
        
        response_text = limit_text_for_sl(ai_text); voice_url = ""
        if generate_voice_flag and global_state.voicevox_enabled and not is_task_started:
            voice_filename = generate_voice_file(response_text, user_uuid)
            if voice_filename: voice_url = f"{SERVER_URL}/play/{voice_filename}"
        return Response(f"{response_text}|{voice_url}", mimetype='text/plain; charset=utf-8', status=200)
    
    except Exception as e:
        logger.critical(f"🔥 致命的エラー (chat_lsl): {e}", exc_info=True)
        return Response("ごめん、システムエラーが起きちゃった…|", mimetype='text/plain; charset=utf-8', status=500)

@app.route('/check_task', methods=['POST'])
def check_task_endpoint():
    try:
        data = request.json; user_uuid = data['uuid']; generate_voice_flag = data.get('voice', False)
        with get_db_session() as session:
            task = session.query(BackgroundTask).filter(BackgroundTask.user_uuid == user_uuid, BackgroundTask.status == 'completed').order_by(BackgroundTask.completed_at.desc()).first()
            if task:
                response_text = task.result; session.delete(task); session.add(ConversationHistory(user_uuid=user_uuid, role='assistant', content=response_text))
                sl_response_text = limit_text_for_sl(response_text, max_length=1023) # 詳細応答のため制限を緩和
                voice_url = ""
                if generate_voice_flag and global_state.voicevox_enabled:
                    voice_filename = generate_voice_file(sl_response_text, user_uuid)
                    if voice_filename: voice_url = f"{SERVER_URL}/play/{voice_filename}"
                return create_json_response({'status': 'completed', 'response': f"{sl_response_text}|{voice_url}"})
        return create_json_response({'status': 'no_tasks'})
    except Exception as e:
        logger.error(f"❌ タスク確認エラー: {e}", exc_info=True)
        return create_json_response({'status': 'error', 'message': str(e)}, 500)

@app.route('/play/<filename>', methods=['GET'])
def play_voice(filename):
    try: return send_from_directory(VOICE_DIR, filename)
    except FileNotFoundError: return Response("File not found", status=404)
    except Exception as e:
        logger.error(f"❌ 音声ファイル配信エラー: {e}")
        return Response("Error sending file", status=500)
        
# ==============================================================================
# VOICEVOX関連
# ==============================================================================
def find_active_voicevox_url():
    urls_to_check = [VOICEVOX_URL_FROM_ENV] if VOICEVOX_URL_FROM_ENV else []; urls_to_check.extend(VOICEVOX_URLS)
    for url in set(urls_to_check):
        if not url: continue
        try:
            response = requests.get(f"{url}/version", timeout=2);
            if response.status_code == 200:
                logger.info(f"✅ VOICEVOX engine found: {url}"); global_state.active_voicevox_url = url; return url
        except requests.RequestException: pass
    logger.warning("⚠️ VOICEVOX engine not found"); return None

def generate_voice_file(text, user_uuid):
    if not global_state.voicevox_enabled or not global_state.active_voicevox_url: return None
    clean_text_for_voice = clean_text(text).replace('|', '')[:200]
    try:
        query_res = requests.post(f"{global_state.active_voicevox_url}/audio_query", params={"text": clean_text_for_voice, "speaker": VOICEVOX_SPEAKER_ID}, timeout=15); query_res.raise_for_status()
        synth_res = requests.post(f"{global_state.active_voicevox_url}/synthesis", params={"speaker": VOICEVOX_SPEAKER_ID}, json=query_res.json(), timeout=30); synth_res.raise_for_status()
        filename = f"voice_{user_uuid[:8]}_{int(time.time())}.wav"; filepath = os.path.join(VOICE_DIR, filename)
        with open(filepath, 'wb') as f: f.write(synth_res.content)
        logger.info(f"✅ 音声ファイル生成成功: {filename}"); return filename
    except Exception as e:
        logger.error(f"❌ 音声生成エラー: {e}", exc_info=True); return None

# ==============================================================================
# リソース管理 & シャットダウン
# ==============================================================================
def cleanup_old_voice_files(max_age_hours: int = 2):
    try:
        cutoff_time = time.time() - (max_age_hours * 3600)
        for filepath in glob.glob(os.path.join(VOICE_DIR, '*.wav')):
            if os.path.getmtime(filepath) < cutoff_time:
                os.remove(filepath); logger.info(f"古い音声ファイルを削除: {os.path.basename(filepath)}")
    except Exception as e: logger.error(f"音声ファイル削除エラー: {e}")

def cleanup_old_conversations(days: int = 90):
    logger.info("古い会話履歴のクリーンアップを開始...")
    with get_db_session() as session:
        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days)
            deleted = session.query(ConversationHistory).filter(ConversationHistory.timestamp < cutoff_date).delete(synchronize_session=False)
            logger.info(f"古い会話履歴を削除: {deleted}件")
        except Exception as e: logger.error(f"会話履歴削除エラー: {e}")

def shutdown_handler():
    logger.info("アプリケーションをシャットダウン中...")
    background_executor.shutdown(wait=False)
    if engine: engine.dispose()
    logger.info("クリーンアップ完了")

atexit.register(shutdown_handler)

# ==============================================================================
# 初期化とスケジューラー
# ==============================================================================
def run_scheduler():
    while True:
        try: schedule.run_pending()
        except Exception as e: logger.error(f"❌ スケジューラーエラー: {e}", exc_info=True)
        time.sleep(60)

def initialize_app():
    global engine, Session, groq_client, gemini_model
    logger.info("=" * 60 + "\n🔧 もちこAI v29.0 (Final Edition) 初期化開始...\n" + "=" * 60)
    
    try:
        logger.info(f"📊 データベースURL: {DATABASE_URL[:20]}...")
        if DATABASE_URL.startswith('sqlite'): engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False}, pool_pre_ping=True, echo=False)
        else: engine = create_engine(DATABASE_URL, poolclass=pool.QueuePool, pool_size=5, max_overflow=10, pool_pre_ping=True, pool_recycle=3600, echo=False)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        with get_db_session() as test_session: test_session.execute(text("SELECT 1"))
        logger.info("✅ データベース初期化完了")
    except Exception as e:
        logger.critical(f"🔥 データベース初期化失敗: {e}", exc_info=True)
    
    try:
        if GROQ_API_KEY:
            groq_client = Groq(api_key=GROQ_API_KEY)
            logger.info("✅ Groq (Llama) API初期化完了")
        else: logger.warning("⚠️ GROQ_API_KEY未設定")
    except Exception as e: logger.error(f"❌ Groq API初期化エラー: {e}", exc_info=True)
    
    try:
        if GEMINI_API_KEY:
            genai.configure(api_key=GEMINI_API_KEY)
            gemini_model = genai.GenerativeModel('gemini-2.5-flash')
            safety_settings = {
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            }
            test_response = gemini_model.generate_content("自己紹介をしてください", generation_config={"max_output_tokens": 20}, safety_settings=safety_settings)
            test_text = _safe_get_gemini_text(test_response)
            if test_text:
                logger.info(f"✅ Gemini API初期化完了 (モデル: gemini-2.5-flash, テスト応答: {test_text[:20]}...)")
            else:
                logger.error("❌ Gemini APIテスト応答の取得に失敗。セーフティブロックの可能性あり。")
                gemini_model = None
        else: logger.warning("⚠️ GEMINI_API_KEY未設定")
    except Exception as e:
        logger.error(f"❌ Gemini API初期化エラー: {e}", exc_info=True); gemini_model = None
    
    if not groq_client and not gemini_model: logger.critical("🔥 警告: どちらのAIモデルも利用できません！")
    
    try:
        if find_active_voicevox_url(): global_state.voicevox_enabled = True
        else: logger.info("ℹ️ VOICEVOX無効")
    except Exception as e: logger.warning(f"⚠️ VOICEVOX初期化エラー: {e}")
    
    try:
        schedule.every(1).hours.do(fetch_hololive_news)
        schedule.every(24).hours.do(update_holomem_database_from_wiki)
        schedule.every(2).hours.do(cleanup_old_voice_files)
        schedule.every(7).days.do(cleanup_old_conversations)
        background_executor.submit(update_holomem_database_from_wiki)
        threading.Thread(target=run_scheduler, daemon=True).start()
        logger.info("✅ スケジューラー起動")
    except Exception as e: logger.error(f"❌ スケジューラー初期化エラー: {e}", exc_info=True)
    
    logger.info("=" * 60)
    logger.info("✅ もちこAI v29.0 初期化完了！")
    logger.info(f"   - データベース: {'✅' if Session else '❌'}")
    logger.info(f"   - Groq API: {'✅' if groq_client else '❌'}")
    logger.info(f"   - Gemini API: {'✅' if gemini_model else '❌'}")
    logger.info(f"   - VOICEVOX: {'✅' if global_state.voicevox_enabled else '❌'}")
    logger.info("=" * 60)

# ==============================================================================
# メイン実行
# ==============================================================================
try:
    initialize_app()
except Exception as e:
    logger.critical(f"🔥 致命的な初期化エラー (グローバルスコープ): {e}", exc_info=True)
    sys.exit(1)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
